"""
Regression tests: a user-supplied (pasted) file must be applicable.

THE PROVEN BUG (session d021ff07, 2026-07-26)
---------------------------------------------
`UserManagementModal.jsx` was not among the session's 5 uploaded files
(`file_manifest_classified` lists exactly 5, and the file is genuinely absent
from the GitHub repo — `gh_session_register_error` x8, HTTP 404). The pipeline
paused and the user pasted it (`agent_filereq_pause_provided`,
content_len=51183). The run then succeeded on every axis that matters:
`resolution_summary` resolved 2 symbols, `qa_gate_passed`, and
`smart_result_emitted` named the file.

The edits were nevertheless impossible to apply. The pause handler registered
the pasted content as a synthetic dict::

    {"filename": fn, "content": content}       # <- no "id"

so `changes_by_file[...]["file_id"]` was `""`. The frontend then requested
`/api/chat/{sid}/files/` + "" — and the Railway request log shows exactly
that, three times::

    GET /api/chat/d021ff07-.../files/  307 Temporary Redirect

`redirect_slashes` bounced it to the *list* endpoint, which returns a JSON
array. No PUT was ever issued, and no error was ever shown. Only 5 files were
in the session, so the 120-req/60s rate limiter played no part.

The fix has three independent layers, each covered below:
  1. persist pasted content as a real `session_files` row (root cause);
  2. recover `file_id` by filename when a producer still fails to persist;
  3. answer an empty file id with 400 instead of silently redirecting.
"""
import os
import shutil
import sys
import tempfile

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

from auth_test_utils import fake_request  # noqa: E402

_PIPELINE_PATH = os.path.join(_BACKEND_DIR, "services", "pipeline.py")


@pytest.fixture()
def isolated_db(monkeypatch):
    """Point database.py at a fresh temp sqlite DB for this test only.

    `database` caches Path.home() into module-level constants at import time,
    so the whole import chain that binds to it must be dropped and re-imported
    after HOME is patched (same approach as test_file_version_history.py).
    """
    tmp_home = tempfile.mkdtemp(prefix="surgicalai_test_home_")
    monkeypatch.setenv("HOME", tmp_home)
    for mod in list(sys.modules):
        if (mod in ("database", "routers", "services")
                or mod.startswith("routers.")
                or mod.startswith("services.session_file_store")
                or mod == "services.session_auth"):
            del sys.modules[mod]
    import database as _database
    _database.init_db()
    from routers import session_files as _session_files
    from services import session_file_store as _store
    yield _database, _session_files, _store
    shutil.rmtree(tmp_home, ignore_errors=True)


def _row_count(database, session_id, filename):
    with database.get_db_ctx() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM session_files "
            "WHERE session_id = ? AND filename = ?",
            (session_id, filename),
        ).fetchone()
    return int(row[0])


# ── Layer 1: pasted content becomes a real row ────────────────────────────────

def test_pasted_file_gets_a_real_id_and_is_fetchable(isolated_db):
    """This is the exact failure from d021ff07: without a real id the apply
    path cannot address the file at all."""
    database, session_files, store = isolated_db
    content = "function isReferralProgram(u) {\n  return true\n}\n"

    entry = store.register_session_file(
        "d021ff07", "UserManagementModal.jsx", content)

    assert entry is not None, "pasted file must be persisted"
    assert entry["id"], "file_id must be non-empty — an empty id is unroutable"
    assert entry["content"] == content

    # And the id must actually resolve through the real GET route.
    with database.get_db_ctx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            ("d021ff07", "t", None),
        )
        conn.commit()
    fetched = session_files.get_session_file("d021ff07", entry["id"], request=fake_request())
    assert fetched["filename"] == "UserManagementModal.jsx"
    assert fetched["content"] == content


def test_pasted_file_is_upserted_not_duplicated(isolated_db):
    """Re-pasting the same filename must update one row, never create a second
    — two rows make "the" file_id for a filename ambiguous."""
    database, _session_files, store = isolated_db
    first = store.register_session_file("s1", "a.js", "v1")
    second = store.register_session_file("s1", "a.js", "v2")

    assert first["id"] == second["id"]
    assert _row_count(database, "s1", "a.js") == 1
    with database.get_db_ctx() as conn:
        row = conn.execute(
            "SELECT content FROM session_files WHERE id = ?", (first["id"],)
        ).fetchone()
    assert row["content"] == "v2"


def test_pasted_file_still_obeys_the_per_file_size_limit(isolated_db):
    """Pasting must not be a way around the upload limits (1 MB for text)."""
    _database, _session_files, store = isolated_db
    too_big = "x" * (2 * 1024 * 1024)
    assert store.register_session_file("s1", "huge.js", too_big) is None


def test_pasted_file_obeys_the_session_cap(isolated_db, monkeypatch):
    """A new row is refused once the 30 MB session budget is spent."""
    _database, _session_files, store = isolated_db
    import middleware.file_validator as fv
    monkeypatch.setattr(fv, "MAX_SESSION_BYTES", 1000)

    assert store.register_session_file("s1", "a.js", "a" * 600) is not None
    assert store.register_session_file("s1", "b.js", "b" * 600) is None, \
        "second file exceeds the session cap and must be refused"
    # Updating an EXISTING row is not new growth and must still work.
    assert store.register_session_file("s1", "a.js", "a" * 700) is not None


def test_degraded_inputs_never_raise(isolated_db):
    """register_session_file is called from inside the streaming pipeline; it
    must degrade to None rather than kill a live run."""
    _database, _session_files, store = isolated_db
    assert store.register_session_file("", "a.js", "x") is None
    assert store.register_session_file("s1", "", "x") is None
    assert store.register_session_file("s1", "a.js", None) is None


# ── Layer 2: file_id recovery by filename ────────────────────────────────────

def test_resolve_session_file_id_recovers_by_filename(isolated_db):
    _database, _session_files, store = isolated_db
    entry = store.register_session_file("s1", "UserManagementModal.jsx", "x")
    assert store.resolve_session_file_id(
        "s1", "UserManagementModal.jsx") == entry["id"]


def test_resolve_session_file_id_returns_none_when_absent(isolated_db):
    _database, _session_files, store = isolated_db
    assert store.resolve_session_file_id("s1", "nope.js") is None
    assert store.resolve_session_file_id("", "") is None


# ── Layer 3: an empty file id must fail loudly, not redirect ─────────────────

def test_empty_file_id_returns_400_not_a_redirect_to_the_list(isolated_db):
    """`GET /chat/{sid}/files/` used to 307 to the list route, so a broken
    apply received a JSON array and looked like it had worked."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    database, session_files, _store = isolated_db

    with database.get_db_ctx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            ("d021ff07", "t", "server_user"),
        )
        conn.commit()

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.user_id = "server_user"
        return await call_next(request)

    app.include_router(session_files.router, prefix="/chat")
    client = TestClient(app)

    for verb in ("get", "put", "delete"):
        resp = getattr(client, verb)(
            "/chat/d021ff07/files/", follow_redirects=False)
        assert resp.status_code == 400, (
            f"{verb.upper()} /files/ returned {resp.status_code}, expected 400 "
            f"— a redirect here silently reroutes an apply to the list route")
        assert "file id" in resp.json()["detail"].lower()


def test_valid_file_id_still_routes_normally(isolated_db):
    """The 400 guard must not shadow real file requests."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    database, session_files, store = isolated_db
    entry = store.register_session_file("s1", "a.js", "hello")

    with database.get_db_ctx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            ("s1", "t", "server_user"),
        )
        conn.commit()

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.user_id = "server_user"
        return await call_next(request)

    app.include_router(session_files.router, prefix="/chat")
    client = TestClient(app)

    ok = client.get(f"/chat/s1/files/{entry['id']}")
    assert ok.status_code == 200 and ok.json()["content"] == "hello"
    # Listing (no trailing slash) is untouched.
    listed = client.get("/chat/s1/files")
    assert listed.status_code == 200 and len(listed.json()) == 1


# ── Schema: index + uniqueness ───────────────────────────────────────────────

def test_session_files_indexes_exist(isolated_db):
    """`session_id` is filtered on every single upload (the 30 MB cap query);
    it must be indexed, and (session_id, filename) must be unique."""
    database, _session_files, _store = isolated_db
    with database.get_db_ctx() as conn:
        names = {
            (r["name"] if hasattr(r, "__getitem__") else r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert "idx_session_files_session" in names
    assert "idx_session_files_session_filename" in names


def test_duplicate_filename_in_one_session_is_rejected_by_the_db(isolated_db):
    """The read-then-insert upsert in every write path is racy; the DB must be
    the backstop so two concurrent uploads cannot both insert."""
    import sqlite3
    database, _session_files, _store = isolated_db
    with database.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content) "
            "VALUES ('id1', 's1', 'dup.js', 'a')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO session_files (id, session_id, filename, content) "
                "VALUES ('id2', 's1', 'dup.js', 'b')")
            conn.commit()


# ── Source guards: keep the defect from growing back ────────────────────────

def _pipeline_source():
    with open(_PIPELINE_PATH, encoding="utf-8") as fh:
        return fh.read()


def test_pause_handler_no_longer_builds_an_idless_stub_entry():
    """The literal defect: registering pasted content as a dict with no `id`
    inside the symbol map, which becomes file_id "" in the smart result."""
    src = _pipeline_source()
    assert 'symbol_maps_by_name[fn] = (\n' \
           '                                        _fr_smap,\n' \
           '                                        {"filename": fn, "content": content})' not in src, \
        "pause-provided file is being registered as an id-less stub again"
    assert "register_session_file" in src, \
        "pause path must persist pasted content via register_session_file"


def test_changes_by_file_recovers_a_missing_file_id():
    """Second layer: even if some other producer yields an entry with no id,
    changes_by_file must resolve it rather than emit ""."""
    src = _pipeline_source()
    assert "resolve_session_file_id" in src
    assert '"file_id": sf_entry.get("id", "") if sf_entry else ""' not in src, \
        "changes_by_file is emitting an unrecovered, possibly-empty file_id"
