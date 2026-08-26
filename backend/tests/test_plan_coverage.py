"""Chat Plan artifact: parse, persist, coverage, model-agnostic, no Agent SSE.

Regression: Ask/Plan must not hit the edit pipeline (see test_mode_selector).
These tests cover the new plan_ready path and the coverage gate.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services.plan_artifact import (  # noqa: E402
    parse_implementation_plan,
    compute_plan_coverage,
    persist_plan_from_assistant_text,
    apply_coverage_to_run,
    latest_plan_run,
    mark_plan_implementing,
    forced_edit_plan_from_run,
)
from services.task_planner import create_tasks  # noqa: E402


CLAUDE_STYLE = """## Overview
Add a banner.

## Steps
1. **File: app.py** — update `main`.

```implementation_plan
{"steps": [
  {"filename": "app.py", "symbol": "main", "description": "Add banner"}
]}
```
"""

GROK_STYLE = """Here is the plan.

```implementation_plan
[
  {"filename": "Header.tsx", "symbol": "Header", "description": "Add nav link"},
  {"filename": "App.tsx", "symbol_path": "App", "description": "Wire Header"}
]
```
"""

GPT_STYLE = """## Overview
Refactor auth.

```plan-json
{"steps": [
  {"filename": "auth.ts", "symbol": "login", "description": "Validate token"}
]}
```
"""


def test_parse_claude_grok_gpt_fences_same_shape():
    c = parse_implementation_plan(CLAUDE_STYLE)
    g = parse_implementation_plan(GROK_STYLE)
    p = parse_implementation_plan(GPT_STYLE)
    assert c == [{"filename": "app.py", "symbol": "main", "description": "Add banner"}]
    assert [s["filename"] for s in g] == ["Header.tsx", "App.tsx"]
    assert g[1]["symbol"] == "App"  # symbol_path alias
    assert p[0]["symbol"] == "login"


def test_parse_missing_or_invalid_returns_none():
    assert parse_implementation_plan("just markdown") is None
    assert parse_implementation_plan("```implementation_plan\nnot-json\n```") is None
    assert parse_implementation_plan("```implementation_plan\n[]\n```") is None
    assert parse_implementation_plan(
        "```implementation_plan\n{\"steps\": [{\"filename\": \"a.py\"}]}\n```"
    ) is None


def test_coverage_missing_and_skipped():
    planned = [
        {"filename": "a.py", "symbol": "foo"},
        {"filename": "b.py", "symbol": "bar"},
        {"filename": "c.py", "symbol": "baz"},
    ]
    result = {
        "changes_by_file": {
            "a.py": {"filename": "a.py", "changes": [{"symbol": "foo"}]},
        },
        "skipped_changes": [
            {"filename": "b.py", "symbol": "bar", "reason": "already matches"},
        ],
    }
    cov = compute_plan_coverage(planned, result)
    assert cov["ok"] is False
    assert cov["missing"] == [{"filename": "c.py", "symbol": "baz"}]
    assert {"filename": "a.py", "symbol": "foo"} in cov["covered"]
    assert {"filename": "b.py", "symbol": "bar"} in cov["skipped"]


def test_coverage_complete_with_extra_allowed():
    planned = [{"filename": "a.py", "symbol": "foo"}]
    result = {
        "changes_by_file": {
            "a.py": {"filename": "a.py", "changes": [
                {"symbol": {"full_path": "foo"}},
                {"symbol": "bonus"},
            ]},
        },
        "skipped_changes": [],
    }
    cov = compute_plan_coverage(planned, result)
    assert cov["ok"] is True
    assert cov["missing"] == []
    assert any(e["symbol"] == "bonus" for e in cov["extra"])


@pytest.fixture()
def plan_db(monkeypatch, tmp_path):
    import database as db
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "plan.db"))
    db.init_db()
    sid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (sid, "t", "user-a"),
        )
        conn.commit()
    return {"db": db, "sid": sid}


def test_create_tasks_without_new_fields_still_inserts(plan_db):
    sid = plan_db["sid"]
    rows = create_tasks(sid, "run-agent", [{"title": "t1", "detail": "d", "kind": "code"}])
    assert len(rows) == 1
    assert rows[0]["source"] == "agent"
    assert rows[0]["filename"] == ""


def test_persist_missing_json_creates_no_rows(plan_db):
    sid = plan_db["sid"]
    ev = persist_plan_from_assistant_text(sid, "no fence here")
    assert ev is None
    assert latest_plan_run(sid) is None


def test_persist_and_revise_replace(plan_db):
    sid = plan_db["sid"]
    first = persist_plan_from_assistant_text(sid, CLAUDE_STYLE)
    assert first["type"] == "plan_ready"
    assert first["tasks"][0]["symbol"] == "main"
    first_run = first["run_id"]

    second = persist_plan_from_assistant_text(sid, GROK_STYLE)
    assert second["type"] == "plan_updated"
    assert second["run_id"] != first_run
    assert [t["symbol"] for t in second["tasks"]] == ["Header", "App"]
    latest = latest_plan_run(sid)
    assert latest["run_id"] == second["run_id"]
    # Implement uses the new set only
    keys = {(s["filename"], s["symbol"]) for s in forced_edit_plan_from_run(sid, latest["run_id"])}
    assert keys == {("Header.tsx", "Header"), ("App.tsx", "App")}


def test_invalid_revise_keeps_previous(plan_db):
    sid = plan_db["sid"]
    first = persist_plan_from_assistant_text(sid, CLAUDE_STYLE)
    ev = persist_plan_from_assistant_text(sid, "forget the fence")
    assert ev["type"] == "plan_unchanged"
    assert ev["run_id"] == first["run_id"]
    assert latest_plan_run(sid)["run_id"] == first["run_id"]


def test_lock_during_implementing(plan_db):
    sid = plan_db["sid"]
    first = persist_plan_from_assistant_text(sid, CLAUDE_STYLE)
    mark_plan_implementing(sid, first["run_id"])
    ev = persist_plan_from_assistant_text(sid, GROK_STYLE)
    assert ev["type"] == "plan_locked"
    assert ev["run_id"] == first["run_id"]
    latest = latest_plan_run(sid)
    assert latest["phase"] == "implementing"
    assert latest["tasks"][0]["symbol"] == "main"


def test_apply_coverage_blocks_missing(plan_db):
    sid = plan_db["sid"]
    ev = persist_plan_from_assistant_text(sid, GROK_STYLE)
    run_id = ev["run_id"]
    planned = forced_edit_plan_from_run(sid, run_id)
    cov = compute_plan_coverage(planned, {
        "changes_by_file": {
            "Header.tsx": {"filename": "Header.tsx", "changes": [{"symbol": "Header"}]},
        },
        "skipped_changes": [],
    })
    updated = apply_coverage_to_run(sid, run_id, cov)
    statuses = {t["symbol"]: t["status"] for t in updated}
    assert statuses["Header"] == "done"
    assert statuses["App"] == "blocked"
    assert latest_plan_run(sid)["phase"] == "blocked"


def test_grok_reinforcement_requires_json():
    from services.grok_ask_plan_native import build_grok_ask_plan_reinforcement
    plan = build_grok_ask_plan_reinforcement("plan")
    ask = build_grok_ask_plan_reinforcement("ask")
    assert "implementation_plan" in plan
    assert "implementation_plan" not in ask
    assert "PLAN MODE" in plan


# ── smart_stream integration: plan_ready, never task_plan / pipeline ─────────

@pytest.fixture()
def stream_env(monkeypatch, tmp_path):
    import database as db
    import importlib
    from fastapi import Request

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "plan_stream.db"))
    db.init_db()

    from routers import chat as chat_router

    calls = {"pipeline": 0, "chat_stream": 0, "plan_tasks": 0}

    async def _fake_chat_stream(messages, **kwargs):
        calls["chat_stream"] += 1
        yield 'data: {"type": "token", "content": ' + json.dumps(CLAUDE_STYLE) + "}\n\n"
        yield 'data: {"type": "done", "content": "", "model": "grok-4.6"}\n\n'

    async def _fake_pipeline(*args, **kwargs):
        calls["pipeline"] += 1
        yield 'data: {"type": "smart_result", "content": "{}"}\n\n'
        yield 'data: {"type": "done", "content": ""}\n\n'

    async def _plan_tasks(*a, **k):
        calls["plan_tasks"] += 1
        return {"tasks": []}

    monkeypatch.setattr(chat_router, "_resolve_chat_key",
                        lambda uid, provider: "sk-test" if provider == "anthropic" else "k")
    monkeypatch.setattr(chat_router, "run_chat_stream", _fake_chat_stream)
    monkeypatch.setattr("services.pipeline.run_natural_pipeline_stream", _fake_pipeline)
    monkeypatch.setattr("services.pipeline.run_smart_pipeline_stream", _fake_pipeline)
    monkeypatch.setattr("database.get_setting",
                        lambda k, d=None: "grok-4.6" if k == "architect_model" else (d if d is not None else ""))
    monkeypatch.setattr("services.task_planner.plan_tasks", _plan_tasks)

    class _Req:
        def __init__(self):
            self.state = type("S", (), {"user_id": "user-a"})()

    sid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (sid, "t", "user-a"),
        )
        conn.commit()

    return {"chat": chat_router, "sid": sid, "req": _Req(), "calls": calls, "db": db}


def test_plan_stream_emits_plan_ready_not_task_plan(stream_env):
    import asyncio
    chat_router = stream_env["chat"]

    async def _drain(resp):
        parts = []
        async for p in resp.body_iterator:
            parts.append(p.decode() if isinstance(p, (bytes, bytearray)) else p)
        return "".join(parts)

    resp = asyncio.run(chat_router.smart_stream(
        {"session_id": stream_env["sid"], "message": "plan a banner", "mode": "plan"},
        stream_env["req"],
    ))
    body = asyncio.run(_drain(resp))
    assert '"type": "plan_ready"' in body
    assert '"type": "task_plan"' not in body
    assert stream_env["calls"]["pipeline"] == 0
    assert stream_env["calls"]["chat_stream"] == 1
    assert stream_env["calls"]["plan_tasks"] == 0
    latest = latest_plan_run(stream_env["sid"])
    assert latest and latest["tasks"][0]["symbol"] == "main"


def test_directive_still_has_required_mode_selector_strings():
    from routers import chat as chat_router
    plan = chat_router._mode_directive("plan")
    assert "PLAN mode" in plan
    assert "surgical_edit" in plan
    assert "implementation_plan" in plan
