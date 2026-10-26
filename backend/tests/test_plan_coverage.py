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
    revert_plan_after_implement_error,
    note_plan_step,
    coverage_after_crash,
    release_unshipped_plan_steps,
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


def test_parse_create_step_uses_filename_as_symbol():
    steps = parse_implementation_plan(
        "```implementation_plan\n"
        "{\"steps\": [{\"filename\": \"test_openai_models.py\", \"action\": \"create\", "
        "\"description\": \"Add the model test\"}]}\n```"
    )
    assert steps == [{
        "filename": "test_openai_models.py",
        "symbol": "test_openai_models.py",
        "description": "Add the model test",
        "action": "create",
    }]


def test_create_step_round_trips_and_new_file_counts_as_done(plan_db):
    sid = plan_db["sid"]
    text = (
        "```implementation_plan\n"
        "{\"steps\": ["
        "{\"filename\": \"test_openai_models.py\", \"action\": \"create\", \"description\": \"add tests\"},"
        "{\"filename\": \"app.py\", \"symbol\": \"main\", \"description\": \"wire it\"}"
        "]}\n```"
    )
    ev = persist_plan_from_assistant_text(sid, text)
    planned = forced_edit_plan_from_run(sid, ev["run_id"])
    assert planned[0]["action"] == "create"
    assert planned[0]["symbol"] == "test_openai_models.py"
    cov = compute_plan_coverage(planned, {
        "changes_by_file": {
            "app.py": {"filename": "app.py", "changes": [{"symbol": "main"}]},
        },
        "new_files": [{"filename": "test_openai_models.py", "content": "def test_ok():\n    pass\n"}],
        "skipped_changes": [],
    })
    assert cov["ok"] is True
    assert cov["missing"] == []


def test_assignment_window_and_missing_symbol_skips_the_model():
    import asyncio
    from services.pipeline import _PlanSymbolNotFound, _execute_single_edit
    from services.plan_artifact import module_assignment_window, plain_skip_reason

    src = "HEADER = 1\n" + ("x = 1\n" * 80) + "MODEL = \"gpt-4.1\"\n" + ("y = 2\n" * 80)
    window = module_assignment_window(src, "MODEL")
    assert window is not None
    assert "MODEL" in window
    assert "HEADER" not in window
    assert plain_skip_reason({
        "filename": "a.py", "symbol": "MODEL", "reason": "symbol not found",
    }) == "Could not find `MODEL` in `a.py`."
    assert "timed out" in plain_skip_reason({
        "filename": "a.py", "symbol": "MODEL", "reason": "timed out editing MODEL in a.py",
    })

    class _Boom:
        class messages:
            @staticmethod
            def stream(**kwargs):
                raise AssertionError("model must not be called")

    async def _run():
        await _execute_single_edit(
            _Boom(), "claude-sonnet-5", "a.py", "MISSING", "set it",
            "VALUE = 1\n", None, "change it",
        )

    try:
        asyncio.run(_run())
    except _PlanSymbolNotFound:
        pass
    else:
        raise AssertionError("missing symbol should fail before the model call")


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


def test_missing_file_and_time_budget_stay_pending():
    planned = [
        {"filename": "a.py", "symbol": "foo"},
        {"filename": "b.py", "symbol": "bar"},
        {"filename": "c.py", "symbol": "baz"},
    ]
    result = {
        "changes_by_file": {},
        "skipped_changes": [
            {"filename": "a.py", "symbol": "foo", "reason": "File not found in session: a.py"},
            {"filename": "b.py", "symbol": "bar", "reason": "Skipped — pipeline time budget reached before this edit could run"},
            {"filename": "c.py", "symbol": "baz", "reason": "already matches"},
        ],
    }
    cov = compute_plan_coverage(planned, result)
    assert cov["ok"] is False
    assert {"filename": "c.py", "symbol": "baz"} in cov["covered"]
    pending = {(p["filename"], p["symbol"]) for p in cov["pending"]}
    assert pending == {("a.py", "foo"), ("b.py", "bar")}
    assert cov["missing"] == []


def test_apply_coverage_keeps_failed_skips_pending(plan_db):
    sid = plan_db["sid"]
    ev = persist_plan_from_assistant_text(sid, GROK_STYLE)
    run_id = ev["run_id"]
    planned = forced_edit_plan_from_run(sid, run_id)
    cov = compute_plan_coverage(planned, {
        "changes_by_file": {},
        "skipped_changes": [
            {"filename": "Header.tsx", "symbol": "Header", "reason": "File not found in session: Header.tsx"},
        ],
    })
    updated = apply_coverage_to_run(sid, run_id, cov)
    statuses = {t["symbol"]: t["status"] for t in updated}
    assert statuses["Header"] == "pending"
    header = next(t for t in updated if t["symbol"] == "Header")
    assert "not in this chat" in header["result_summary"]
    assert latest_plan_run(sid)["phase"] == "ready"


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


def test_staged_file_flags_unknown_symbol_before_build(plan_db):
    sid = plan_db["sid"]
    with plan_db["db"].get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, language) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), sid, "app.py", "def main():\n    return 1\n", "python"),
        )
        conn.commit()
    text = (
        "```implementation_plan\n"
        "{\"steps\": [{\"filename\": \"app.py\", \"symbol\": \"missing_name\", "
        "\"description\": \"no such function\"}]}\n```"
    )
    ev = persist_plan_from_assistant_text(sid, text)
    task = latest_plan_run(sid)["tasks"][0]
    assert task["status"] == "pending"
    assert task["verdict"] == "unresolved"
    assert "missing_name" in (task.get("result_summary") or "")
    assert ev["tasks"][0]["verdict"] == "unresolved"


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
    assert "surgical_edit" in plan
    assert "code examples" in plan or "Best practices" in plan


def test_persist_stores_markdown_and_user_id(plan_db):
    from services.plan_artifact import load_session_plan
    sid = plan_db["sid"]
    ev = persist_plan_from_assistant_text(sid, CLAUDE_STYLE, user_id="user-a")
    assert ev["type"] == "plan_ready"
    assert "markdown" in ev and "Add a banner" in ev["markdown"]
    assert ev["version"] == 1
    doc = load_session_plan(sid)
    assert doc is not None
    assert doc["user_id"] == "user-a"
    assert doc["markdown"] == CLAUDE_STYLE.strip()
    assert "## Overview" in doc["markdown"]


def test_persist_revise_bumps_version_and_keeps_revision(plan_db):
    from services.plan_artifact import load_session_plan
    from database import get_db
    sid = plan_db["sid"]
    first = persist_plan_from_assistant_text(sid, CLAUDE_STYLE, user_id="user-a")
    assert first["version"] == 1
    second = persist_plan_from_assistant_text(sid, GROK_STYLE, user_id="user-a")
    assert second["type"] == "plan_updated"
    assert second["version"] == 2
    assert "Header" in second["markdown"]
    doc = load_session_plan(sid)
    assert doc["version"] == 2
    conn = get_db()
    try:
        revs = conn.execute(
            "SELECT version, markdown FROM session_plan_revisions "
            "WHERE session_id = ? ORDER BY version",
            (sid,),
        ).fetchall()
    finally:
        conn.close()
    assert len(revs) == 1
    assert revs[0]["version"] == 1
    assert "Add a banner" in revs[0]["markdown"]


def test_plan_markdown_injected_outside_history_cap(plan_db):
    from services.plan_artifact import current_plan_markdown_for_prompt
    sid = plan_db["sid"]
    persist_plan_from_assistant_text(sid, CLAUDE_STYLE, user_id="user-a")
    injected = current_plan_markdown_for_prompt(sid)
    assert "Current session plan" in injected
    assert "Add a banner" in injected
    assert "implementation_plan" in injected


def test_implement_request_includes_saved_markdown():
    import inspect
    from services.plan_artifact import plan_build_user_request
    from routers import chat as chat_router
    src = inspect.getsource(plan_build_user_request)
    assert "current_plan_markdown_for_prompt" in src
    assert "Implement the attached implementation_plan steps exactly." in src
    assert "plan_build_user_request" in inspect.getsource(chat_router.implement_plan)


def test_get_active_plan_foreign_user_forbidden(plan_db):
    from fastapi import HTTPException
    from routers import chat as chat_router

    sid = plan_db["sid"]
    persist_plan_from_assistant_text(sid, CLAUDE_STYLE, user_id="user-a")

    class _Req:
        def __init__(self, uid):
            self.state = type("S", (), {"user_id": uid})()

    # Owner can read
    ok = chat_router.get_active_plan(sid, _Req("user-a"))
    assert ok["markdown"]
    assert ok["version"] == 1
    assert ok["tasks"]

    with pytest.raises(HTTPException) as ei:
        chat_router.get_active_plan(sid, _Req("user-other"))
    assert ei.value.status_code == 403


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
    assert "PLAN mode" not in plan
    assert "implementation plan" in plan.lower()
    assert "surgical_edit" in plan
    assert "implementation_plan" in plan


def test_plan_and_agent_runs_stay_independent(plan_db):
    sid = plan_db["sid"]
    agent = create_tasks(sid, "run-agent-1", [{"title": "agent step", "detail": "d"}])
    plan = persist_plan_from_assistant_text(sid, CLAUDE_STYLE)
    assert plan["run_id"] != "run-agent-1"
    from services.task_planner import list_tasks
    agent_rows = list_tasks(sid, "run-agent-1")
    plan_rows = list_tasks(sid, plan["run_id"])
    assert [r["source"] for r in agent_rows] == ["agent"]
    assert all(r["source"] == "plan" for r in plan_rows)
    assert latest_plan_run(sid)["run_id"] == plan["run_id"]
    assert agent[0]["id"] == agent_rows[0]["id"]


def test_new_plan_after_complete(plan_db):
    sid = plan_db["sid"]
    first = persist_plan_from_assistant_text(sid, CLAUDE_STYLE)
    planned = forced_edit_plan_from_run(sid, first["run_id"])
    cov = compute_plan_coverage(planned, {
        "changes_by_file": {
            "app.py": {"filename": "app.py", "changes": [{"symbol": "main"}]},
        },
        "skipped_changes": [],
    })
    apply_coverage_to_run(sid, first["run_id"], cov)
    assert latest_plan_run(sid)["phase"] == "complete"
    second = persist_plan_from_assistant_text(sid, GROK_STYLE)
    assert second["type"] == "plan_ready"
    assert second["run_id"] != first["run_id"]
    assert latest_plan_run(sid)["run_id"] == second["run_id"]


def test_all_skipped_with_reason_marks_done(plan_db):
    sid = plan_db["sid"]
    ev = persist_plan_from_assistant_text(sid, GROK_STYLE)
    planned = forced_edit_plan_from_run(sid, ev["run_id"])
    cov = compute_plan_coverage(planned, {
        "changes_by_file": {},
        "skipped_changes": [
            {"filename": "Header.tsx", "symbol": "Header", "reason": "already matches"},
            {"filename": "App.tsx", "symbol": "App", "reason": "no-op"},
        ],
    })
    assert cov["ok"] is True
    updated = apply_coverage_to_run(sid, ev["run_id"], cov)
    assert {t["status"] for t in updated} == {"done"}
    assert latest_plan_run(sid)["phase"] == "complete"


def test_implement_and_persist_not_claude_gated():
    import inspect
    from routers import chat as chat_router
    from services import plan_artifact
    impl = inspect.getsource(chat_router.implement_plan)
    persist = inspect.getsource(plan_artifact.persist_plan_from_assistant_text)
    assert "_is_claude" not in impl
    assert "plan_tasks" not in impl
    assert "_is_claude" not in persist
    assert "plan_tasks" not in persist


def test_coverage_aliases_path_and_dotted_symbol():
    planned = [{"filename": "src/Header.tsx", "symbol": "Header"}]
    result = {
        "changes_by_file": {
            "Header.tsx": {
                "filename": "Header.tsx",
                "changes": [{"symbol": {"full_path": "App.Header"}}],
            },
        },
        "skipped_changes": [],
    }
    cov = compute_plan_coverage(planned, result)
    assert cov["ok"] is True
    assert cov["missing"] == []


def test_coverage_rejects_unrelated_qualified_symbols():
    planned = [{"filename": "a.py", "symbol": "Widget.render"}]
    result = {
        "changes_by_file": {
            "a.py": {"filename": "a.py", "changes": [{"symbol": "Header.render"}]},
        },
        "skipped_changes": [],
    }
    cov = compute_plan_coverage(planned, result)
    assert cov["ok"] is False
    assert cov["missing"] == []
    assert cov["covered"] == []
    assert cov["pending"] == [{
        "filename": "a.py",
        "symbol": "Widget.render",
        "reason": "Edited `Header.render` instead of `Widget.render`.",
    }]


def test_revert_implement_error_unlocks_pending(plan_db):
    sid = plan_db["sid"]
    ev = persist_plan_from_assistant_text(sid, CLAUDE_STYLE)
    mark_plan_implementing(sid, ev["run_id"])
    updated = revert_plan_after_implement_error(sid, ev["run_id"], "pipeline exploded")
    assert all(t["status"] == "pending" for t in updated)
    assert latest_plan_run(sid)["phase"] == "ready"
    assert "pipeline exploded" in (updated[0].get("result_summary") or "")


def test_omitted_fence_retries_then_plan_ready(stream_env, monkeypatch):
    import asyncio
    chat_router = stream_env["chat"]
    calls = stream_env["calls"]

    async def _fake(messages, **kwargs):
        calls["chat_stream"] += 1
        if calls["chat_stream"] == 1:
            yield 'data: {"type": "token", "content": "## Overview\\nNo fence."}\n\n'
        else:
            yield 'data: {"type": "token", "content": ' + json.dumps(CLAUDE_STYLE) + "}\n\n"
        yield 'data: {"type": "done", "content": "", "model": "grok-4.6"}\n\n'

    monkeypatch.setattr(chat_router, "run_chat_stream", _fake)
    calls["chat_stream"] = 0

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
    assert '"type": "plan_missing"' not in body
    assert '"type": "task_plan"' not in body
    assert calls["chat_stream"] == 2
    assert calls["pipeline"] == 0
    assert calls["plan_tasks"] == 0
    assert latest_plan_run(stream_env["sid"])["tasks"][0]["symbol"] == "main"


def test_omitted_fence_retry_empty_emits_plan_missing(stream_env, monkeypatch):
    import asyncio
    chat_router = stream_env["chat"]
    calls = stream_env["calls"]

    async def _fake(messages, **kwargs):
        calls["chat_stream"] += 1
        yield 'data: {"type": "token", "content": "just markdown, no fence"}\n\n'
        yield 'data: {"type": "done", "content": "", "model": "gpt-4.1"}\n\n'

    monkeypatch.setattr(chat_router, "run_chat_stream", _fake)
    calls["chat_stream"] = 0

    async def _drain(resp):
        parts = []
        async for p in resp.body_iterator:
            parts.append(p.decode() if isinstance(p, (bytes, bytearray)) else p)
        return "".join(parts)

    resp = asyncio.run(chat_router.smart_stream(
        {"session_id": stream_env["sid"], "message": "plan auth", "mode": "plan"},
        stream_env["req"],
    ))
    body = asyncio.run(_drain(resp))
    assert '"type": "plan_missing"' in body
    assert '"type": "plan_ready"' not in body
    assert '"type": "task_plan"' not in body
    assert calls["chat_stream"] == 2
    assert calls["pipeline"] == 0
    assert latest_plan_run(stream_env["sid"]) is None


def test_implement_exception_emits_plan_failed_not_coverage(stream_env, monkeypatch):
    import asyncio
    sid = stream_env["sid"]
    ev = persist_plan_from_assistant_text(sid, CLAUDE_STYLE)
    with stream_env["db"].get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, language) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), sid, "app.py", "def main():\n    pass\n", "python"),
        )
        conn.commit()

    async def _boom(*a, **k):
        raise RuntimeError("pipeline exploded")
        yield  # pragma: no cover

    monkeypatch.setattr("services.pipeline.run_natural_pipeline_stream", _boom)

    async def _drain(resp):
        parts = []
        async for p in resp.body_iterator:
            parts.append(p.decode() if isinstance(p, (bytes, bytearray)) else p)
        return "".join(parts)

    resp = asyncio.run(stream_env["chat"].implement_plan(
        {"session_id": sid, "run_id": ev["run_id"]},
        stream_env["req"],
    ))
    body = asyncio.run(_drain(resp))
    assert '"type": "plan_failed"' in body
    assert '"type": "plan_coverage"' not in body
    latest = latest_plan_run(sid)
    assert latest["phase"] == "ready"
    assert all(t["status"] == "pending" for t in latest["tasks"])


def _drain_implement(stream_env, sid, run_id):
    import asyncio

    async def _drain(resp):
        parts = []
        async for p in resp.body_iterator:
            parts.append(p.decode() if isinstance(p, (bytes, bytearray)) else p)
        return "".join(parts)

    resp = asyncio.run(stream_env["chat"].implement_plan(
        {"session_id": sid, "run_id": run_id},
        stream_env["req"],
    ))
    return asyncio.run(_drain(resp))


def _insert_impl_file(stream_env, sid):
    with stream_env["db"].get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, language) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), sid, "Header.tsx", "export function Header(){return null}\n", "tsx"),
        )
        conn.commit()


def test_error_without_result_retries_steps_marked_done_in_memory(stream_env, monkeypatch):
    sid = stream_env["sid"]
    ev = persist_plan_from_assistant_text(sid, GROK_STYLE)
    run_id = ev["run_id"]
    _insert_impl_file(stream_env, sid)

    async def _crash(*a, **k):
        note_plan_step(sid, run_id, "Header.tsx", "Header", "done")
        note_plan_step(sid, run_id, "App.tsx", "App", "done")
        yield 'data: {"type": "error", "content": "UnboundLocalError: MAX_QA_RETRIES"}\n\n'
        yield 'data: {"type": "done", "content": ""}\n\n'

    monkeypatch.setattr("services.pipeline.run_natural_pipeline_stream", _crash)
    body = _drain_implement(stream_env, sid, run_id)
    assert '"type": "plan_failed"' in body
    assert '"type": "plan_coverage"' not in body
    assert "not produced" not in body
    latest = latest_plan_run(sid)
    assert latest["phase"] == "ready"
    assert {t["symbol"]: t["status"] for t in latest["tasks"]} == {
        "Header": "pending",
        "App": "pending",
    }
    again = {(s["filename"], s["symbol"]) for s in forced_edit_plan_from_run(sid, run_id)}
    assert again == {("Header.tsx", "Header"), ("App.tsx", "App")}
    assert all("MAX_QA_RETRIES" in (t.get("result_summary") or "") for t in latest["tasks"])


def test_error_with_checkpoint_keeps_recovered_steps(stream_env, monkeypatch):
    sid = stream_env["sid"]
    ev = persist_plan_from_assistant_text(sid, GROK_STYLE)
    run_id = ev["run_id"]
    _insert_impl_file(stream_env, sid)
    checkpoint = json.dumps({
        "phase": "plan_execute",
        "changes_by_file": {
            "Header.tsx": {
                "filename": "Header.tsx",
                "changes": [{"symbol": "Header", "original_code": "a", "new_code": "b"}],
            },
        },
    })

    async def _crash(*a, **k):
        note_plan_step(sid, run_id, "Header.tsx", "Header", "done")
        note_plan_step(sid, run_id, "App.tsx", "App", "done")
        yield "data: " + json.dumps({"type": "checkpoint", "content": checkpoint}) + "\n\n"
        yield 'data: {"type": "error", "content": "UnboundLocalError: MAX_QA_RETRIES"}\n\n'
        yield 'data: {"type": "done", "content": ""}\n\n'

    monkeypatch.setattr("services.pipeline.run_natural_pipeline_stream", _crash)
    body = _drain_implement(stream_env, sid, run_id)
    assert '"type": "smart_result"' in body
    assert '"type": "plan_coverage"' in body
    assert '"type": "plan_failed"' not in body
    assert "not produced" not in body
    latest = latest_plan_run(sid)
    statuses = {t["symbol"]: t["status"] for t in latest["tasks"]}
    assert statuses == {"Header": "done", "App": "pending"}
    app = next(t for t in latest["tasks"] if t["symbol"] == "App")
    assert "Build crashed" in (app.get("result_summary") or "")
    again = {(s["filename"], s["symbol"]) for s in forced_edit_plan_from_run(sid, run_id)}
    assert again == {("App.tsx", "App")}


def test_release_unshipped_leaves_covered_steps(plan_db):
    sid = plan_db["sid"]
    ev = persist_plan_from_assistant_text(sid, GROK_STYLE)
    run_id = ev["run_id"]
    tasks = latest_plan_run(sid)["tasks"]
    header = next(t for t in tasks if t["symbol"] == "Header")
    app = next(t for t in tasks if t["symbol"] == "App")
    from services.task_planner import update_task
    update_task(header["id"], status="done", verdict="covered")
    update_task(app["id"], status="done", verdict="edited")
    release_unshipped_plan_steps(sid, run_id, "Build crashed")
    again = {(s["filename"], s["symbol"]) for s in forced_edit_plan_from_run(sid, run_id)}
    assert again == {("App.tsx", "App")}
    cov = coverage_after_crash(
        [{"filename": "Header.tsx", "symbol": "Header"}, {"filename": "App.tsx", "symbol": "App"}],
        {"changes_by_file": {"Header.tsx": {"changes": [{"symbol": "Header"}]}}, "skipped_changes": []},
        "Build crashed: boom",
    )
    assert cov["missing"] == []
    assert {p["symbol"] for p in cov["pending"]} == {"App"}


def test_new_file_does_not_cover_an_edit_and_a_child_symbol_covers_the_parent():
    planned = [
        {"filename": "config.py", "symbol": "MODEL_CATALOG", "description": "edit the catalog"},
        {"filename": "test_openai_models.py", "symbol": "test_openai_models.py", "description": "add tests", "action": "create"},
        {"filename": "title_matcher.py", "symbol": "TitleMatcher", "description": "update the matcher"},
        {"filename": "api_governor.py", "symbol": "governed_chat_completion", "description": "govern the call"},
    ]
    cov = compute_plan_coverage(planned, {
        "changes_by_file": {
            "title_matcher.py": {"changes": [{"symbol": "TitleMatcher.__init__"}]},
            "api_governor.py": {"changes": [{"symbol": "_text_region_L274_364"}]},
        },
        "new_files": [{"filename": "config.py", "content": "{\"filename\": \"test_openai_models.py\"}"}],
        "skipped_changes": [],
    })
    covered = {row["symbol"] for row in cov["covered"]}
    assert "MODEL_CATALOG" not in covered
    assert "test_openai_models.py" not in covered
    assert "TitleMatcher" in covered
    pending = {row["symbol"]: row["reason"] for row in cov["pending"]}
    assert pending["governed_chat_completion"] == (
        "Edited `_text_region_L274_364` instead of `governed_chat_completion`."
    )
    matched = compute_plan_coverage(
        [planned[1]],
        {
            "changes_by_file": {},
            "new_files": [{"filename": "test_openai_models.py", "content": "def test_ok():\n    pass\n"}],
            "skipped_changes": [],
        },
    )
    assert matched["ok"] is True


def test_done_clears_the_old_summary_and_a_blocked_step_is_not_rerun(plan_db):
    from services.plan_artifact import mark_next_plan_step_running

    sid = plan_db["sid"]
    text = (
        "```implementation_plan\n"
        "{\"steps\": ["
        "{\"filename\": \"config.py\", \"symbol\": \"MODEL_CATALOG\", \"description\": \"edit\"},"
        "{\"filename\": \"app.py\", \"symbol\": \"main\", \"description\": \"edit\"}"
        "]}\n```"
    )
    ev = persist_plan_from_assistant_text(sid, text)
    run_id = ev["run_id"]
    note_plan_step(sid, run_id, "config.py", "MODEL_CATALOG", "blocked", "symbol not found")
    nxt = mark_next_plan_step_running(sid, run_id)
    assert nxt["symbol"] == "main"
    first = next(t for t in latest_plan_run(sid)["tasks"] if t["symbol"] == "MODEL_CATALOG")
    assert first["status"] == "blocked"
    assert first["result_summary"] == "symbol not found"
    note_plan_step(sid, run_id, "config.py", "MODEL_CATALOG", "done")
    done = next(t for t in latest_plan_run(sid)["tasks"] if t["symbol"] == "MODEL_CATALOG")
    assert done["status"] == "done"
    assert not (done.get("result_summary") or "").strip()


def test_plan_create_keeps_the_step_filename_and_unwraps_nested_json():
    from services.pipeline import normalize_plan_new_file

    raw = json.dumps({
        "filename": "config.py",
        "content": json.dumps({
            "filename": "test_openai_models.py",
            "content": "def test_models():\n    assert True\n",
            "language": "python",
        }),
    })
    saved = json.loads(normalize_plan_new_file(raw, "test_openai_models.py"))
    assert saved["filename"] == "test_openai_models.py"
    assert saved["content"] == "def test_models():\n    assert True\n"
    assert "config.py" not in saved["content"]
