"""Edit mode builds the saved plan from storage, not from trimmed chat history."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services.plan_artifact import (  # noqa: E402
    message_requests_plan_build,
    persist_plan_from_assistant_text,
)


def _long_plan() -> str:
    body = "A" * 5000
    return (
        f"{body}\n\n"
        "```implementation_plan\n"
        '{"steps": ['
        '{"filename": "a.py", "symbol": "alpha", "description": "one"},'
        '{"filename": "b.py", "symbol": "beta", "description": "two"}'
        "]}\n"
        "```\n"
    )


@pytest.fixture()
def plan_db(monkeypatch, tmp_path):
    import database as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "plan_build.db"))
    db.init_db()
    sid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (sid, "t", "user-a"),
        )
        conn.commit()
    return {"sid": sid}


def _stream(monkeypatch, sid, message):
    from routers import chat as chat_router

    calls = {}

    async def _fake_pipeline(*args, **kwargs):
        calls["kwargs"] = kwargs
        yield 'data: {"type": "done", "content": ""}\n\n'

    monkeypatch.setattr(chat_router, "_resolve_chat_key", lambda uid, provider: "k")
    monkeypatch.setattr("services.pipeline.run_natural_pipeline_stream", _fake_pipeline)
    monkeypatch.setattr("services.pipeline.run_smart_pipeline_stream", _fake_pipeline)

    class _Req:
        def __init__(self):
            self.state = type("S", (), {"user_id": "user-a"})()

    async def _drain(resp):
        parts = []
        async for p in resp.body_iterator:
            parts.append(p.decode() if isinstance(p, (bytes, bytearray)) else p)
        return "".join(parts)

    resp = asyncio.run(chat_router.smart_stream(
        {"session_id": sid, "message": message, "mode": "edit"},
        _Req(),
    ))
    body = asyncio.run(_drain(resp))
    return calls, body


def test_edit_build_the_plan_passes_full_plan(plan_db, monkeypatch):
    sid = plan_db["sid"]
    text = _long_plan()
    ev = persist_plan_from_assistant_text(sid, text)
    calls, body = _stream(monkeypatch, sid, "build the plan")
    kwargs = calls["kwargs"]
    steps = kwargs["forced_edit_plan"]
    assert [(s["filename"], s["symbol"]) for s in steps] == [
        ("a.py", "alpha"),
        ("b.py", "beta"),
    ]
    assert kwargs["plan_run_id"] == ev["run_id"]
    assert len(kwargs["user_request"]) > 4000
    assert "A" * 4000 in kwargs["user_request"]
    assert "Implement the attached implementation_plan steps exactly." in kwargs["user_request"]
    assert kwargs["user_request"] != "build the plan"
    assert '"type": "plan_updated"' in body
    assert message_requests_plan_build("implement this plan") is True
    assert message_requests_plan_build("build off the plan") is True
    assert message_requests_plan_build("execute the plan") is True


def test_unrelated_edit_does_not_force_plan(plan_db, monkeypatch):
    sid = plan_db["sid"]
    persist_plan_from_assistant_text(sid, _long_plan())
    calls, _body = _stream(monkeypatch, sid, "fix the typo")
    assert not calls["kwargs"].get("forced_edit_plan")
    assert message_requests_plan_build("don't build the plan") is False
