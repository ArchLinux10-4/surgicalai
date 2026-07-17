"""
Tests for the Ask / Plan / Edit / Agent mode selector.

Design goal (the whole point of the feature): Ask and Plan modes must be
*structurally* incapable of reaching the edit pipeline. This mirrors the #1
lesson from Cursor/Copilot plan-mode failures — enforce "no edits" by control
flow, not by trusting the model to obey a prompt.

Two layers of coverage:

1. Pure helpers (`_normalize_mode`, `_mode_directive`) — fast, no I/O.
2. Integration: drive `smart_stream()` end-to-end against a temp SQLite DB with
   the LLM + pipeline mocked, and assert:
     - ask/plan  -> run_chat_stream called, edit pipeline NEVER called,
                    tokens streamed, assistant reply persisted.
     - edit      -> edit pipeline IS reached (regression guard).
"""
import os
import sys
import asyncio
import tempfile
import uuid
import importlib
from pathlib import Path

import pytest

# Point the SQLite DB at an isolated temp HOME before importing the app.
_TMP_HOME = tempfile.mkdtemp(prefix="sai_test_home_")
os.environ["HOME"] = _TMP_HOME
os.environ.pop("DATABASE_URL", None)  # force SQLite path

# backend/ on the path so `import database`, `routers.chat` resolve.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import database  # noqa: E402
importlib.reload(database)  # re-evaluate DB_PATH against the temp HOME
database.init_db()

from routers import chat as chat_router  # noqa: E402


# ─────────────────────────── Pure helper tests ────────────────────────────────

def test_normalize_mode_defaults_to_edit():
    for raw in (None, "", "   ", "nonsense", "EDITT", 123):
        assert chat_router._normalize_mode(raw) == "edit"


def test_normalize_mode_accepts_known_modes_case_insensitive():
    assert chat_router._normalize_mode("ask") == "ask"
    assert chat_router._normalize_mode("PLAN") == "plan"
    assert chat_router._normalize_mode("  Agent ") == "agent"
    assert chat_router._normalize_mode("edit") == "edit"


def test_mode_directive_ask_and_plan_forbid_edits():
    ask = chat_router._mode_directive("ask")
    plan = chat_router._mode_directive("plan")
    assert "ASK mode" in ask
    assert "PLAN mode" in plan
    for d in (ask, plan):
        assert "surgical_edit" in d  # explicit "do NOT produce <surgical_edit>"
    assert chat_router._mode_directive("edit") == ""
    assert chat_router._mode_directive("agent") == ""


# ─────────────────────────── Integration harness ──────────────────────────────

class _FakeRequest:
    def __init__(self, user_id="u_test"):
        self.state = type("S", (), {"user_id": user_id})()


def _seed_session():
    sid = str(uuid.uuid4())
    with database.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title) VALUES (?, ?)",
            (sid, "test"),
        )
        conn.commit()
    return sid


async def _drain(response):
    """Collect the full SSE body from a StreamingResponse."""
    chunks = []
    async for part in response.body_iterator:
        chunks.append(part.decode() if isinstance(part, (bytes, bytearray)) else part)
    return "".join(chunks)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _mock_backends(monkeypatch):
    # Pretend an Anthropic key is configured so the 401 guard passes.
    monkeypatch.setattr(chat_router, "_resolve_chat_key",
                        lambda uid, provider: "sk-test" if provider == "anthropic" else "")

    # Track whether the edit pipeline was ever entered.
    calls = {"pipeline": 0, "chat_stream": 0}

    async def _fake_chat_stream(messages, **kwargs):
        calls["chat_stream"] += 1
        # Echo back the directive so we can assert it was injected.
        yield 'data: {"type": "token", "content": "ANSWER"}\n\n'
        # Real run_chat_stream tags its terminal done with the resolved model;
        # mirror that so the model-capture trace path is exercised.
        yield 'data: {"type": "done", "content": "", "model": "claude-sonnet-4-20250514"}\n\n'

    async def _fake_pipeline(*args, **kwargs):
        calls["pipeline"] += 1
        yield 'data: {"type": "smart_result", "content": "{}"}\n\n'
        yield 'data: {"type": "done", "content": ""}\n\n'

    # Ask/Plan call run_chat_stream via chat.py's module-level import binding.
    monkeypatch.setattr(chat_router, "run_chat_stream", _fake_chat_stream)
    # Edit/Agent import the pipeline fresh inside smart_stream() at call time,
    # so patching the source module reaches those bindings.
    monkeypatch.setattr("services.pipeline.run_natural_pipeline_stream", _fake_pipeline)
    monkeypatch.setattr("services.pipeline.run_smart_pipeline_stream", _fake_pipeline)
    # Force the natural (Claude) pipeline branch deterministically.
    monkeypatch.setattr("database.get_setting",
                        lambda k, d=None: "claude-sonnet-4-20250514" if k == "architect_model" else (d if d is not None else ""))
    return calls


@pytest.mark.parametrize("mode", ["ask", "plan"])
def test_ask_plan_never_touch_edit_pipeline(_mock_backends, mode):
    sid = _seed_session()
    resp = _run(chat_router.smart_stream(
        {"session_id": sid, "message": "How does auth work?", "mode": mode},
        _FakeRequest(),
    ))
    body = _run(_drain(resp))

    # Streamed a plain answer...
    assert "ANSWER" in body
    assert _mock_backends["chat_stream"] == 1
    # ...and the edit pipeline was NEVER entered. This is the core guarantee.
    assert _mock_backends["pipeline"] == 0

    # Assistant reply persisted.
    with database.get_db_ctx() as conn:
        rows = conn.execute(
            "SELECT content FROM chat_messages WHERE session_id = ? AND role = 'assistant'",
            (sid,),
        ).fetchall()
    assert any("ANSWER" in (r["content"] or "") for r in rows)


def test_ask_mode_error_path_is_graceful(monkeypatch, _mock_backends):
    """If the LLM stream raises, Ask mode must emit an error+done, not crash.

    Regression guard: the except branch previously referenced a helper defined
    later in the function (NameError). This exercises that path directly.
    """
    async def _boom(messages, **kwargs):
        raise RuntimeError("upstream down")
        yield  # pragma: no cover  (makes this an async generator)

    monkeypatch.setattr(chat_router, "run_chat_stream", _boom)

    sid = _seed_session()
    resp = _run(chat_router.smart_stream(
        {"session_id": sid, "message": "explain this", "mode": "ask"},
        _FakeRequest(),
    ))
    body = _run(_drain(resp))

    assert '"type": "error"' in body
    assert '"type": "done"' in body
    assert _mock_backends["pipeline"] == 0  # still never touches the edit path


def test_ask_mode_logs_resolved_model(monkeypatch, _mock_backends):
    """Ghost-guard: sse_mode_done must record which backend produced the answer.

    run_chat_stream never logs its resolved model, so if an Ask/Plan reply is
    empty we'd otherwise have no idea which backend was used. The mode branch
    captures `model` from the terminal `done` event — assert it reaches _dlog.
    """
    events = []
    monkeypatch.setattr(chat_router, "_dlog",
                        lambda tag, **kw: events.append((tag, kw)))

    sid = _seed_session()
    resp = _run(chat_router.smart_stream(
        {"session_id": sid, "message": "how does this work?", "mode": "ask"},
        _FakeRequest(),
    ))
    _run(_drain(resp))

    done = [kw for tag, kw in events if tag == "sse_mode_done"]
    assert done, "sse_mode_done was never logged"
    assert done[0]["model"] == "claude-sonnet-4-20250514"
    assert done[0]["mode"] == "ask"
    assert done[0]["chars"] > 0
    # mode must also appear on the very first log line of the request.
    start = [kw for tag, kw in events if tag == "sse_stream_start"]
    assert start and start[0]["mode"] == "ask"


def test_edit_mode_reaches_pipeline(_mock_backends):
    """Regression guard: the default edit path must still hit the pipeline."""
    sid = _seed_session()
    resp = _run(chat_router.smart_stream(
        {"session_id": sid, "message": "add a null check", "mode": "edit"},
        _FakeRequest(),
    ))
    _run(_drain(resp))
    assert _mock_backends["pipeline"] == 1
    assert _mock_backends["chat_stream"] == 0


def test_missing_mode_defaults_to_edit(_mock_backends):
    """No mode field -> edit path (pipeline reached), never ask/plan."""
    sid = _seed_session()
    resp = _run(chat_router.smart_stream(
        {"session_id": sid, "message": "fix the bug"},
        _FakeRequest(),
    ))
    _run(_drain(resp))
    assert _mock_backends["pipeline"] == 1
    assert _mock_backends["chat_stream"] == 0


# ───────────────────── Offline (Ollama/Qwen) guard tests ───────────────────────
# Rule: on a local 7B model, Agent mode (multi-agent task pipeline) must be
# STRUCTURALLY unreachable — it degrades to the isolated single-pass offline
# stream (whole-file rewrite). This mirrors the frontend hiding Plan/Agent, but
# the backend guard is the real guarantee: even a stale client that sends
# mode="agent" (or a stray "create tasks" phrase) can never plan/create tasks.

def _offline_get_setting(k, d=None):
    """Route to a local Ollama model so _should_use_ollama() is True."""
    if k == "architect_model":
        return "ollama:qwen2.5-coder:7b"
    if k == "ollama_enabled":
        return "true"
    return d if d is not None else ""


@pytest.fixture
def _offline_env(monkeypatch, _mock_backends):
    """Offline user + counters for the task pipeline and the offline stream."""
    counts = {"plan_tasks": 0, "create_tasks": 0, "offline_stream": 0}

    # Force the offline route deterministically (overrides the autouse claude mock).
    monkeypatch.setattr("database.get_setting", _offline_get_setting)

    async def _plan_tasks(*a, **k):
        counts["plan_tasks"] += 1
        return {"tasks": [{"seq": 1, "title": "t", "detail": "d"}]}

    def _create_tasks(*a, **k):
        counts["create_tasks"] += 1
        return []

    async def _offline_stream(**kwargs):
        counts["offline_stream"] += 1
        yield 'data: {"type": "token", "content": "OFFLINE_ANSWER"}\n\n'
        yield 'data: {"type": "done", "content": ""}\n\n'

    # Task planner is imported fresh inside smart_stream() from services.task_planner.
    monkeypatch.setattr("services.task_planner.plan_tasks", _plan_tasks)
    monkeypatch.setattr("services.task_planner.create_tasks", _create_tasks)
    # Offline pipeline is imported fresh inside smart_stream() at dispatch time.
    monkeypatch.setattr(
        "services.offline.offline_pipeline.run_offline_stream", _offline_stream)
    return counts


@pytest.mark.parametrize("trigger", [
    {"mode": "agent"},                 # explicit Agent button
    {"mode": "edit", "force_tasks": True},  # legacy toggle still on
])
def test_offline_agent_never_reaches_task_pipeline(_offline_env, trigger):
    sid = _seed_session()
    payload = {"session_id": sid, "message": "build me a feature"}
    payload.update(trigger)
    resp = _run(chat_router.smart_stream(payload, _FakeRequest()))
    body = _run(_drain(resp))

    # The multi-agent task pipeline was NEVER entered...
    assert _offline_env["plan_tasks"] == 0
    assert _offline_env["create_tasks"] == 0
    # ...and work was served by the isolated offline (whole-file) stream instead.
    assert _offline_env["offline_stream"] == 1
    assert "OFFLINE_ANSWER" in body


def test_offline_agent_logs_the_guard(monkeypatch, _offline_env):
    """The degrade must be observable in the trace (sse_mode_offline_no_agent)."""
    events = []
    monkeypatch.setattr(chat_router, "_dlog",
                        lambda tag, **kw: events.append((tag, kw)))

    sid = _seed_session()
    _run(_drain(_run(chat_router.smart_stream(
        {"session_id": sid, "message": "create tasks for this", "mode": "agent"},
        _FakeRequest(),
    ))))

    guard = [kw for tag, kw in events if tag == "sse_mode_offline_no_agent"]
    assert guard, "offline agent-guard never logged"
    assert guard[0]["architect_model"] == "ollama:qwen2.5-coder:7b"
    assert guard[0]["mode"] == "agent"


def test_offline_edit_still_reaches_offline_stream(_offline_env):
    """Regression: plain Edit offline goes straight to the offline stream."""
    sid = _seed_session()
    _run(_drain(_run(chat_router.smart_stream(
        {"session_id": sid, "message": "add a null check", "mode": "edit"},
        _FakeRequest(),
    ))))
    assert _offline_env["offline_stream"] == 1
    assert _offline_env["plan_tasks"] == 0
