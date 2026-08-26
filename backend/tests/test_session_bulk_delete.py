"""Bulk session delete + list_sessions file_count (sidebar multi-select).

Evidence for the gap: Sidebar promptBulkDelete awaited api.sessionFiles.list
sequentially for every selected id, then deleted with N round-trips — felt
broken/slow. list_sessions now returns file_count; POST /sessions/bulk-delete
deletes many ids in one request with per-id ownership checks.
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
def env(monkeypatch, tmp_path):
    import database as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "bulk.db"))
    db.init_db()

    from routers import chat as chat_router

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.user_id = request.headers.get("X-Test-User", "user-a")
        request.state.username = request.state.user_id
        request.state.is_admin = False
        return await call_next(request)

    app.include_router(chat_router.router, prefix="/api/chat")
    return {"client": TestClient(app), "db": db}


def _session(db, user_id: str | None, title: str = "t") -> str:
    sid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (sid, title, user_id),
        )
        conn.commit()
    return sid


def _message(db, session_id: str, content: str = "hi"):
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), session_id, "user", content),
        )
        conn.commit()


def _file(db, session_id: str, filename: str = "a.py"):
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, lines, symbol_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), session_id, filename, "x=1", 1, 0),
        )
        conn.commit()


def test_list_sessions_includes_file_count(env):
    db, client = env["db"], env["client"]
    sid = _session(db, "user-a")
    _message(db, sid)
    _file(db, sid, "one.py")
    _file(db, sid, "two.py")

    r = client.get("/api/chat/sessions", headers={"X-Test-User": "user-a"})
    assert r.status_code == 200
    row = next(s for s in r.json() if s["id"] == sid)
    assert int(row["message_count"]) == 1
    assert int(row["file_count"]) == 2


def test_bulk_delete_removes_owned_sessions(env):
    db, client = env["db"], env["client"]
    a = _session(db, "user-a", "a")
    b = _session(db, "user-a", "b")
    _message(db, a)
    _file(db, b)

    r = client.post(
        "/api/chat/sessions/bulk-delete",
        json={"ids": [a, b]},
        headers={"X-Test-User": "user-a"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["deleted"]) == {a, b}
    assert body["errors"] == []

    with db.get_db_ctx() as conn:
        left = conn.execute(
            "SELECT COUNT(*) AS c FROM chat_sessions WHERE id IN (?, ?)", (a, b)
        ).fetchone()["c"]
        msgs = conn.execute(
            "SELECT COUNT(*) AS c FROM chat_messages WHERE session_id IN (?, ?)", (a, b)
        ).fetchone()["c"]
        files = conn.execute(
            "SELECT COUNT(*) AS c FROM session_files WHERE session_id IN (?, ?)", (a, b)
        ).fetchone()["c"]
    assert left == 0
    assert msgs == 0
    assert files == 0


def test_bulk_delete_skips_other_users_session(env):
    db, client = env["db"], env["client"]
    mine = _session(db, "user-a", "mine")
    theirs = _session(db, "user-b", "theirs")
    _message(db, theirs, "secret")

    r = client.post(
        "/api/chat/sessions/bulk-delete",
        json={"ids": [mine, theirs]},
        headers={"X-Test-User": "user-a"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] == [mine]
    assert len(body["errors"]) == 1
    assert body["errors"][0]["id"] == theirs
    assert body["errors"][0]["status"] == 403
    assert body["ok"] is False

    with db.get_db_ctx() as conn:
        assert conn.execute(
            "SELECT title FROM chat_sessions WHERE id = ?", (theirs,)
        ).fetchone()["title"] == "theirs"


def test_bulk_delete_requires_ids(env):
    client = env["client"]
    r = client.post(
        "/api/chat/sessions/bulk-delete",
        json={},
        headers={"X-Test-User": "user-a"},
    )
    assert r.status_code == 400
