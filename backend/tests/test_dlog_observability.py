"""
Tests for the observability gap-closure work (see the dlog-observability spec).

Covers:
  • the debug_events.emit reentrancy guard (no recursion, single DB row);
  • database._dlog now REACHING the exportable debug_events table;
  • rate_limit_rejected emitted with the correct tier/retry_after on a forced
    limit breach (and NOTHING logged on the allow path);
  • session_file_upload_ok / _rejected on BOTH upload routes;
  • the client-event endpoint: auth required, size cap (413), and that user_id
    is stamped from request.state — never from the client body.

Isolated temp-HOME SQLite DB, mirroring the pattern used by the other tests.
"""
import os
import sys
import json
import tempfile
import importlib
from pathlib import Path

import pytest

# Isolated SQLite DB before importing the app.
_TMP_HOME = tempfile.mkdtemp(prefix="sai_test_dlog_")
os.environ["HOME"] = _TMP_HOME
os.environ.pop("DATABASE_URL", None)  # force SQLite path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import database  # noqa: E402
importlib.reload(database)
database.init_db()

import debug_events  # noqa: E402


# ─────────────────────────── helpers ──────────────────────────────────────────

def _events(event=None, like=None):
    """Return debug_events rows (as parsed dicts) optionally filtered by exact
    event name or a LIKE prefix on the event column."""
    with database.get_db_ctx() as conn:
        if event is not None:
            rows = conn.execute(
                "SELECT event, session_id, user_id, data FROM debug_events WHERE event = ?",
                (event,),
            ).fetchall()
        elif like is not None:
            rows = conn.execute(
                "SELECT event, session_id, user_id, data FROM debug_events WHERE event LIKE ?",
                (like,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT event, session_id, user_id, data FROM debug_events"
            ).fetchall()
    out = []
    for r in rows:
        rec = {"event": r["event"], "session_id": r["session_id"], "user_id": r["user_id"]}
        try:
            rec["data"] = json.loads(r["data"])
        except Exception:
            rec["data"] = {}
        out.append(rec)
    return out


def _count(event):
    with database.get_db_ctx() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM debug_events WHERE event = ?", (event,)
        ).fetchone()
    return int(row["c"])


# ─────────────────────────── reentrancy guard ─────────────────────────────────

def test_emit_reentrancy_guard_skips_db_when_already_emitting():
    """If a thread is already inside emit(), a nested emit() must NOT write a
    second DB row (it degrades to the /tmp line). This is what stops
    database._dlog (fired by get_db_ctx open/close DURING emit's own write)
    from recursing into an infinite loop / extra rows."""
    before = _count("nested_probe")
    debug_events._state.in_progress = True
    try:
        debug_events.emit("nested_probe", session_id="x")
    finally:
        debug_events._state.in_progress = False
    after = _count("nested_probe")
    assert after == before, "reentrant emit must not write a DB row"


def test_emit_writes_exactly_one_row_despite_lifecycle_dlogs():
    """A single emit() opens get_db_ctx, whose open/close fire database._dlog →
    emit (reentrant). The guard must ensure exactly ONE row for our event and
    that the process terminates (no recursion)."""
    ev = "single_row_probe_" + os.urandom(4).hex()
    debug_events.emit(ev, session_id="s1")
    assert _count(ev) == 1


# ────────────────── database._dlog reaches debug_events ────────────────────────

def test_database_dlog_now_reaches_exportable_debug_events():
    """The core gap #1 fix: database._dlog (imported by session_files/debug/
    database itself) previously NEVER reached debug_events. It must now."""
    ev = "db_dlog_probe_" + os.urandom(4).hex()
    database._dlog(ev, session_id="abc123", user_id="u1", detail="hello")
    rows = _events(ev)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "abc123"
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["data"]["detail"] == "hello"
    assert rows[0]["data"]["event"] == ev  # data carries the full record


# ─────────────────────────── rate limiter ─────────────────────────────────────

def test_rate_limit_rejected_emitted_with_tier_and_retry_after():
    """Force a pipeline-tier breach (cap 10) and assert a rate_limit_rejected
    row with the REAL tier/cap/window/retry_after — not a re-guess."""
    from middleware.rate_limiter import check_rate_limit, _PIPELINE_LIMIT

    cap, window = _PIPELINE_LIMIT
    user = "ratelimit_user_" + os.urandom(4).hex()
    path = "/api/chat/send"  # a pipeline path

    before_reject = _count("rate_limit_rejected")
    resp = None
    for _ in range(cap + 5):
        resp = check_rate_limit(user, path, None)
        if resp is not None:
            break
    assert resp is not None and resp.status_code == 429, "expected a 429 after breaching the cap"

    rows = [r for r in _events("rate_limit_rejected") if r["data"].get("user_id") == user]
    assert rows, "a rate_limit_rejected event must be recorded on rejection"
    d = rows[0]["data"]
    assert d["tier"] == "pipeline"
    assert d["path"] == path
    assert d["cap"] == cap and d["window"] == window
    assert isinstance(d["retry_after"], int) and d["retry_after"] > 0


def test_rate_limit_allow_path_emits_nothing():
    """The allow path is per-request hot code — it must add NO log line."""
    from middleware.rate_limiter import check_rate_limit

    user = "allow_user_" + os.urandom(4).hex()
    before = _count("rate_limit_rejected")
    resp = check_rate_limit(user, "/api/settings", None)  # first call always allowed
    assert resp is None
    assert _count("rate_limit_rejected") == before


# ─────────────────────────── upload outcomes ──────────────────────────────────

@pytest.fixture()
def upload_client():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from routers import session_files
    import database

    # Prior tests may have rebound DB_PATH; re-init on the active path.
    database.init_db()

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.user_id = "server_user"
        request.state.username = "server"
        request.state.is_admin = False
        return await call_next(request)

    app.include_router(session_files.router, prefix="/chat")
    client = TestClient(app)

    _orig_post = client.post

    def _post(url, *args, **kwargs):
        parts = url.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "chat":
            sid = parts[1]
            with database.get_db_ctx() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
                    (sid, "t", "server_user"),
                )
                conn.commit()
        return _orig_post(url, *args, **kwargs)

    client.post = _post
    return client


def test_json_upload_ok_and_rejected_events(upload_client):
    sid = "sess_json_" + os.urandom(4).hex()

    # Success path
    r = upload_client.post(f"/chat/{sid}/files",
                           json={"filename": "a.py", "content": "print('hi')\n"})
    assert r.status_code == 200
    oks = [e for e in _events("session_file_upload_ok")
           if e["data"].get("filename") == "a.py" and e["data"].get("route") == "json"]
    assert oks, "expected a session_file_upload_ok for the JSON route"
    assert oks[0]["data"]["replaced"] is False

    # Rejection path: oversize text file (> 1 MB) → 413 from validate_file_size
    big = "x" * (1024 * 1024 + 10)
    r2 = upload_client.post(f"/chat/{sid}/files",
                            json={"filename": "big.txt", "content": big})
    assert r2.status_code == 413
    rej = [e for e in _events("session_file_upload_rejected")
           if e["data"].get("filename") == "big.txt" and e["data"].get("route") == "json"]
    assert rej, "expected a session_file_upload_rejected for the oversize JSON upload"
    assert rej[0]["data"]["status"] == 413
    assert "too large" in rej[0]["data"]["reason"].lower()


def test_multipart_upload_ok_and_rejected_events(upload_client):
    sid = "sess_mp_" + os.urandom(4).hex()

    r = upload_client.post(f"/chat/{sid}/files/upload",
                           files={"file": ("b.py", b"print('hi')\n", "text/x-python")})
    assert r.status_code == 200
    oks = [e for e in _events("session_file_upload_ok")
           if e["data"].get("filename") == "b.py" and e["data"].get("route") == "multipart"]
    assert oks, "expected a session_file_upload_ok for the multipart route"

    big = b"x" * (1024 * 1024 + 10)
    r2 = upload_client.post(f"/chat/{sid}/files/upload",
                            files={"file": ("big.txt", big, "text/plain")})
    assert r2.status_code == 413
    rej = [e for e in _events("session_file_upload_rejected")
           if e["data"].get("filename") == "big.txt" and e["data"].get("route") == "multipart"]
    assert rej, "expected a session_file_upload_rejected for the oversize multipart upload"
    assert rej[0]["data"]["status"] == 413


# ─────────────────────────── client-event endpoint ────────────────────────────

@pytest.fixture()
def client_event_app():
    """Mount the debug router behind a middleware that stamps request.state
    exactly like main.py's auth_middleware does, so we can exercise the
    endpoint's OWN logic (size cap, user-id stamping) deterministically."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from routers import debug as debug_router

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.user_id = "server_user"
        request.state.username = "server"
        request.state.is_admin = False
        return await call_next(request)

    app.include_router(debug_router.router)
    return TestClient(app)


def test_client_event_stamps_server_user_id_not_client_supplied(client_event_app):
    """user_id must come from request.state, NEVER from the client body."""
    r = client_event_app.post("/api/debug/client-event", json={
        "event": "toast_error",
        "user_id": "ATTACKER",             # must be ignored
        "data": {"user_id": "ATTACKER2", "filename": "x.py"},
    })
    assert r.status_code == 200
    rows = _events(like="client_%")
    mine = [e for e in rows if e["event"] == "client_toast_error"]
    assert mine, "client event must be recorded with the client_ prefix"
    # Stamped from state, not from the body/data
    assert mine[-1]["user_id"] == "server_user"
    assert mine[-1]["data"]["user_id"] == "server_user"


def test_client_event_size_cap_returns_413_and_logs(client_event_app):
    huge = {"event": "big", "data": {"blob": "z" * (8 * 1024)}}
    r = client_event_app.post("/api/debug/client-event", json=huge)
    assert r.status_code == 413
    assert _count("client_event_rejected_too_large") >= 1


def test_client_event_requires_auth_via_full_app():
    """Through the real app + auth middleware, an unauthenticated POST is 401.
    Proves the endpoint is NOT open (normal users authenticated, admin only
    for read/clear)."""
    from fastapi.testclient import TestClient
    import main  # imported lazily — heavy; sets up all routers
    client = TestClient(main.app)
    r = client.post("/api/debug/client-event", json={"event": "x", "data": {}})
    assert r.status_code == 401
