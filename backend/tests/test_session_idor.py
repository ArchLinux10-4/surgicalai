"""Session resource IDOR — ownership gates on chat/session file routes.

Policy under test matches list_sessions visibility in routers/chat.py:
  WHERE s.user_id = ? OR s.user_id IS NULL

Evidence for the gap (pre-fix): get_messages / delete_session / session_files
/ credit-pause GET selected by session_id only with no owner check.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)


@pytest.fixture()
def idor_env(monkeypatch, tmp_path):
    """Fresh SQLite + minimal app mounting the routers under test.

    Monkeypatches database.DB_PATH (restored automatically) so sibling tests
    that already imported `database` are not left pointing at a deleted temp DB.
    """
    import database as db
    from pathlib import Path

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "idor.db"))
    db.init_db()

    from routers import chat as chat_router
    from routers import session_files as files_router
    from routers import tasks as tasks_router
    from routers import surgical as surgical_router

    app = FastAPI()

    # Stamp user_id the same way main.auth_middleware does — tests control it
    # via the X-Test-User header so owner vs attacker can share one client.
    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.user_id = request.headers.get("X-Test-User", "user-a")
        request.state.username = request.state.user_id
        request.state.is_admin = request.headers.get("X-Test-Admin", "") == "1"
        return await call_next(request)

    app.include_router(chat_router.router, prefix="/api/chat")
    app.include_router(files_router.router, prefix="/api/chat")
    app.include_router(tasks_router.router, prefix="/api/tasks")
    app.include_router(surgical_router.router, prefix="/api/surgical")

    client = TestClient(app)
    return {
        "client": client,
        "db": db,
        "tmp": str(tmp_path),
    }


def _create_session(db, user_id: str | None, title: str = "t") -> str:
    sid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (sid, title, user_id),
        )
        conn.commit()
    return sid


def _add_message(db, session_id: str, content: str = "secret"):
    mid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (mid, session_id, "user", content),
        )
        conn.commit()
    return mid


def _add_file(db, session_id: str, filename: str = "secret.py", content: str = "print(1)"):
    fid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, lines, symbol_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fid, session_id, filename, content, 1, 0),
        )
        conn.commit()
    return fid


def test_owner_can_read_messages(idor_env):
    db, client = idor_env["db"], idor_env["client"]
    sid = _create_session(db, "user-a")
    _add_message(db, sid, "hello owner")
    r = client.get(f"/api/chat/sessions/{sid}/messages", headers={"X-Test-User": "user-a"})
    assert r.status_code == 200
    assert any(m["content"] == "hello owner" for m in r.json())


def test_attacker_cannot_read_messages(idor_env):
    db, client = idor_env["db"], idor_env["client"]
    sid = _create_session(db, "user-a")
    _add_message(db, sid, "private")
    r = client.get(f"/api/chat/sessions/{sid}/messages", headers={"X-Test-User": "user-b"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Not your session"


def test_attacker_cannot_list_or_get_session_files(idor_env):
    db, client = idor_env["db"], idor_env["client"]
    sid = _create_session(db, "user-a")
    fid = _add_file(db, sid, content="SECRET_TOKEN=1")

    listed = client.get(f"/api/chat/{sid}/files", headers={"X-Test-User": "user-b"})
    assert listed.status_code == 403

    got = client.get(f"/api/chat/{sid}/files/{fid}", headers={"X-Test-User": "user-b"})
    assert got.status_code == 403


def test_attacker_cannot_delete_or_rename_session(idor_env):
    db, client = idor_env["db"], idor_env["client"]
    sid = _create_session(db, "user-a", title="keep-me")

    r = client.patch(
        f"/api/chat/sessions/{sid}",
        json={"title": "hijacked"},
        headers={"X-Test-User": "user-b"},
    )
    assert r.status_code == 403

    r = client.delete(f"/api/chat/sessions/{sid}", headers={"X-Test-User": "user-b"})
    assert r.status_code == 403

    # Owner row still intact
    with db.get_db_ctx() as conn:
        row = conn.execute("SELECT title FROM chat_sessions WHERE id = ?", (sid,)).fetchone()
    assert row["title"] == "keep-me"


def test_legacy_null_owner_session_readable_by_authed_user(idor_env):
    """Matches list_sessions: user_id IS NULL rows are visible to any authed user."""
    db, client = idor_env["db"], idor_env["client"]
    sid = _create_session(db, None)
    _add_message(db, sid, "legacy")
    r = client.get(f"/api/chat/sessions/{sid}/messages", headers={"X-Test-User": "user-b"})
    assert r.status_code == 200
    assert any(m["content"] == "legacy" for m in r.json())


def test_missing_session_is_404(idor_env):
    client = idor_env["client"]
    r = client.get(
        f"/api/chat/sessions/{uuid.uuid4()}/messages",
        headers={"X-Test-User": "user-a"},
    )
    assert r.status_code == 404


def test_credit_pause_get_requires_session_owner(idor_env):
    db, client = idor_env["db"], idor_env["client"]
    from services.anthropic_billing import save_credit_pause

    sid = _create_session(db, "user-a")
    pause_id = save_credit_pause(
        session_id=sid,
        user_id="user-a",
        user_request="retry",
        remaining_plan=[{"filename": "a.py", "symbol": "f"}],
        completed_edit_blocks=[],
        held_grok_writes=[],
        file_content_snapshot={},
        error_message="credit balance is too low",
    )
    assert pause_id

    denied = client.get(f"/api/chat/credit-pause/{sid}", headers={"X-Test-User": "user-b"})
    assert denied.status_code == 403

    allowed = client.get(f"/api/chat/credit-pause/{sid}", headers={"X-Test-User": "user-a"})
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["active"] is True
    assert body["pause"]["pause_id"] == pause_id


def test_credit_pause_dismiss_blocks_other_user(idor_env):
    db, client = idor_env["db"], idor_env["client"]
    from services.anthropic_billing import save_credit_pause, get_credit_pause

    sid = _create_session(db, "user-a")
    pause_id = save_credit_pause(
        session_id=sid,
        user_id="user-a",
        user_request="retry",
        remaining_plan=[],
        error_message="low",
    )
    r = client.post(
        f"/api/chat/credit-pause/{pause_id}/dismiss",
        headers={"X-Test-User": "user-b"},
    )
    assert r.status_code == 403
    assert get_credit_pause(pause_id)["status"] == "paused"


def test_delete_session_clears_credit_pauses(idor_env):
    db, client = idor_env["db"], idor_env["client"]
    from services.anthropic_billing import save_credit_pause, get_active_credit_pause

    sid = _create_session(db, "user-a")
    save_credit_pause(
        session_id=sid,
        user_id="user-a",
        user_request="retry",
        remaining_plan=[{"filename": "x"}],
        error_message="low",
    )
    assert get_active_credit_pause(sid) is not None

    r = client.delete(f"/api/chat/sessions/{sid}", headers={"X-Test-User": "user-a"})
    assert r.status_code == 200
    assert get_active_credit_pause(sid) is None


def test_tasks_list_blocks_other_user(idor_env):
    db, client = idor_env["db"], idor_env["client"]
    from services import task_planner

    sid = _create_session(db, "user-a")
    task_planner.create_tasks(sid, "run-1", [{"title": "t1", "detail": "d", "kind": "edit"}])
    r = client.get(f"/api/tasks?session_id={sid}", headers={"X-Test-User": "user-b"})
    assert r.status_code == 403


def test_applied_changes_block_other_user(idor_env):
    db, client = idor_env["db"], idor_env["client"]
    sid = _create_session(db, "user-a")
    r = client.get(f"/api/surgical/applied/{sid}", headers={"X-Test-User": "user-b"})
    assert r.status_code == 403

    ok = client.get(f"/api/surgical/applied/{sid}", headers={"X-Test-User": "user-a"})
    assert ok.status_code == 200
    assert ok.json()["applied_ids"] == []


def test_unscoped_qa_log_requires_admin(idor_env):
    client = idor_env["client"]
    denied = client.get("/api/surgical/qa-log", headers={"X-Test-User": "user-a"})
    assert denied.status_code == 403

    allowed = client.get(
        "/api/surgical/qa-log",
        headers={"X-Test-User": "admin", "X-Test-Admin": "1"},
    )
    assert allowed.status_code == 200


def test_require_session_access_helper_unit():
    """Direct unit coverage of the shared helper without HTTP."""
    from fastapi import HTTPException
    from services.session_auth import require_session_access

    with pytest.raises(HTTPException) as ei:
        require_session_access("", "user-a")
    assert ei.value.status_code == 400

    with pytest.raises(HTTPException) as ei:
        require_session_access("any", "")
    assert ei.value.status_code == 401
