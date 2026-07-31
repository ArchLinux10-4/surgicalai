"""
Regression test for the GPT Agent-Mode "thinking starvation" auto-retry
(services/pipeline.py, analyze_and_plan_stream, GPT `else` branch).

Background (proven via source read + live simulation, session 2026-07-31):
Claude's Agent-Mode branch (pipeline.py ~6340-6407) already auto-retries once
with a doubled thinking budget + a user-visible "Re-analyzing with expanded
capacity..." SSE message whenever the model returns empty text
(`agent_thinking_starvation_detected` / `agent_starvation_retry_done` /
`agent_starvation_final_fail` dlog events).

Verified via a standalone simulation (real code, no mocking of _chat_create's
internals) that `_chat_create` ALSO already applies its own internal
truncation/empty-output retry for reasoning-effort GPT models via
services/gpt_reasoning.py:create_with_truncation_retry (flag `gpt5_hardening`,
default ON) -- but that inner retry never yields any SSE progress event and
never emits any `agent_*` dlog event, so a GPT user watching Agent Mode run
sees nothing symmetric to Claude's "Re-analyzing..." message, and log queries
for `agent_thinking_starvation_detected` only ever show Claude sessions.

Fix: pipeline.py's GPT `else` branch now adds a second, user-visible retry
tier using the SAME dlog event names as the Claude branch (differentiated by
the `model=` field already present on every one of those events), firing only
if `full_text` is STILL empty after `_chat_create` returns (i.e. after any
internal hardening retry already ran). This test proves that tier fires,
emits the right SSE events and dlog events, and recovers text on retry --
using two mocked `_chat_create` calls to deterministically simulate the
"starved twice, then a client-side retry with escalated budget/effort would
succeed" scenario end-to-end through the real `analyze_and_plan_stream`
generator (no internals of `_chat_create` are bypassed by shortcutting the
generator itself -- only the network-calling function is mocked, exactly like
every other test in this suite).
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from services import pipeline as pl  # noqa: E402
from services.gpt_reasoning import RETRY_MAX_COMPLETION_TOKENS  # noqa: E402


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class _FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = _FakeMessage(content)
        self.finish_reason = finish_reason


class _FakeResp:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [_FakeChoice(content, finish_reason)]


TINY_FILE = "def add(a, b):\n    return a + b\n"
GOOD_JSON = '{"intent": "chat", "chat_response": "Looks fine as-is."}'


def _patch_common(monkeypatch, model="gpt-5.6-terra"):
    monkeypatch.setattr(pl, "get_setting", lambda key, default=None:
                         model if key == "architect_model" else default)
    monkeypatch.setattr(pl, "_get_client", lambda user_id="": object())
    monkeypatch.setattr(pl, "_get_anthropic_key", lambda user_id="":
                         (_ for _ in ()).throw(ValueError("no anthropic key")))
    events = []
    monkeypatch.setattr(pl, "_dlog", lambda event, **kw: events.append((event, kw)))
    return events


async def _collect_sse(gen):
    import json
    out = []
    async for chunk in gen:
        assert chunk.startswith("data: ")
        out.append(json.loads(chunk[len("data: "):].strip()))
    return out


@pytest.mark.asyncio
async def test_gpt_starvation_retry_recovers_and_emits_symmetric_events(monkeypatch):
    events = _patch_common(monkeypatch)
    calls = {"n": 0}

    def fake_chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call (would already have exhausted _chat_create's own
            # internal hardening retry before ever returning) -- still empty.
            return _FakeResp("")
        # Our new outer retry tier's call -- recovers.
        assert kwargs.get("max_completion_tokens") == RETRY_MAX_COMPLETION_TOKENS
        assert kwargs.get("reasoning_effort") == "high"
        return _FakeResp(GOOD_JSON)

    monkeypatch.setattr(pl, "_chat_create", fake_chat_create)

    sse_events = await _collect_sse(pl.analyze_and_plan_stream(
        file_path="tiny.py", file_content=TINY_FILE,
        user_request="does this look ok?", session_id="s1", user_id="u1",
    ))

    assert calls["n"] == 2, "expected exactly one outer retry call after the starved first call"

    progress_msgs = [e["content"] for e in sse_events if e.get("type") == "progress"]
    assert any("Re-analyzing with expanded capacity" in m for m in progress_msgs), (
        "GPT branch must yield the same user-visible retry message as Claude's branch"
    )

    chat_events = [e for e in sse_events if e.get("type") == "chat"]
    assert chat_events and chat_events[0]["content"] == "Looks fine as-is."

    event_names = [e for e, _ in events]
    assert "agent_thinking_starvation_detected" in event_names
    assert "agent_starvation_retry_done" in event_names
    assert "agent_starvation_final_fail" not in event_names

    detected = next(kw for e, kw in events if e == "agent_thinking_starvation_detected")
    assert detected["model"] == "gpt-5.6-terra"
    retry_done = next(kw for e, kw in events if e == "agent_starvation_retry_done")
    assert retry_done["retry_text_len"] == len(GOOD_JSON)


@pytest.mark.asyncio
async def test_gpt_starvation_retry_still_empty_logs_final_fail(monkeypatch):
    events = _patch_common(monkeypatch)
    calls = {"n": 0}

    def fake_chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        return _FakeResp("")  # starved on every call, retry does not help

    monkeypatch.setattr(pl, "_chat_create", fake_chat_create)

    sse_events = await _collect_sse(pl.analyze_and_plan_stream(
        file_path="tiny.py", file_content=TINY_FILE,
        user_request="does this look ok?", session_id="s2", user_id="u2",
    ))

    assert calls["n"] == 2, "must call exactly once more (the outer retry), not loop forever"

    # Empty text fails JSON parsing -> the model-agnostic parse-error message fires
    # (previously hardcoded "Claude returned unexpected output" for ALL models --
    # fixed alongside this retry tier since the retry makes this path reachable
    # for GPT more meaningfully).
    error_events = [e for e in sse_events if e.get("type") == "error"]
    assert error_events and "gpt-5.6-terra" in error_events[0]["content"]
    assert "Claude" not in error_events[0]["content"]

    event_names = [e for e, _ in events]
    assert "agent_thinking_starvation_detected" in event_names
    assert "agent_starvation_retry_done" in event_names
    assert "agent_starvation_final_fail" in event_names


@pytest.mark.asyncio
async def test_gpt_no_retry_needed_when_first_call_succeeds(monkeypatch):
    """Sanity check: the new retry tier must NOT fire (and must add zero
    extra calls / SSE noise) on the normal happy path."""
    events = _patch_common(monkeypatch)
    calls = {"n": 0}

    def fake_chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        return _FakeResp(GOOD_JSON)

    monkeypatch.setattr(pl, "_chat_create", fake_chat_create)

    sse_events = await _collect_sse(pl.analyze_and_plan_stream(
        file_path="tiny.py", file_content=TINY_FILE,
        user_request="does this look ok?", session_id="s3", user_id="u3",
    ))

    assert calls["n"] == 1, "no retry call should happen when the first response is non-empty"

    event_names = [e for e, _ in events]
    assert "agent_thinking_starvation_detected" not in event_names
    assert "agent_starvation_retry_done" not in event_names
    assert "agent_starvation_final_fail" not in event_names

    progress_msgs = [e["content"] for e in sse_events if e.get("type") == "progress"]
    assert not any("Re-analyzing with expanded capacity" in m for m in progress_msgs)
