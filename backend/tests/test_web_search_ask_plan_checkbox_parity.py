"""
Tests for the Research-checkbox parity fix: Ask/Plan mode gaining the same
per-message web-search opt-in that the Edit/Agent "Research" checkbox
already had (`run_natural_pipeline_stream`'s `web_search_enabled` param).

Background (proven via code, not guessed — see chat.py `smart_stream`):
  - The per-message checkbox has ALWAYS written a per-session setting key
    (`web_search_research_session_{session_id}`) unconditionally, regardless
    of `mode`. Only Edit/Agent (`run_natural_pipeline_stream`) ever READ it.
  - Ask/Plan's `run_chat_stream` only had the global, always-on
    `web_search_enabled` Settings toggle — no per-message control.

This fix adds a `web_search_enabled` parameter to `run_chat_stream` (default
False, so every existing caller that omits it is unaffected) and OR's it
with the legacy global Settings toggle inside the existing Claude-only gate.
chat.py's Ask/Plan branch now reads the same per-session setting the
checkbox already writes and passes it through.

Scope: pipeline.py's `run_chat_stream` gating logic only. Does NOT touch
the already-working OpenAI/Sonnet-5 QA path, Edit/Agent's
`run_natural_pipeline_stream` (unchanged, own test suite), or the
ClaudeWebSearchStreamTracker internals (already covered by
test_claude_web_search.py).
"""
import json
import sys

import pytest

sys.path.insert(0, ".")

from services import pipeline
from services.claude_web_search import build_web_search_tool


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Event:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


class _Delta:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeStreamCtx:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self._events:
            yield e


class _FakeMessages:
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        events = self._rounds.pop(0)
        return _FakeStreamCtx(events)


class _FakeClient:
    def __init__(self, rounds):
        self.messages = _FakeMessages(rounds)


async def _collect(agen):
    out = []
    async for chunk in agen:
        assert chunk.startswith("data: ")
        out.append(json.loads(chunk[6:]))
    return out


def _plain_answer_round(text="Plain answer."):
    return [
        _Event("content_block_delta", index=0, delta=_Delta(text=text)),
        _Event("content_block_stop", index=0),
    ]


def _apply_common_mocks(monkeypatch, fake_client):
    monkeypatch.setattr(pipeline, "_is_claude_model", lambda m: True)
    monkeypatch.setattr(pipeline, "_is_gemini_model", lambda m: False)
    monkeypatch.setattr(pipeline, "_should_use_ollama", lambda m: False)
    monkeypatch.setattr(pipeline, "_get_anthropic_key", lambda user_id="": "fake-key")
    monkeypatch.setattr(pipeline, "AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setattr(pipeline, "_max_output_tokens", lambda m: 4096)
    monkeypatch.setattr(pipeline, "_get_thinking_kwargs", lambda m, budget: {})
    monkeypatch.setattr(pipeline, "_get_effort_kwargs", lambda m: {})


@pytest.mark.asyncio
async def test_per_message_flag_true_adds_tool_when_global_setting_off(monkeypatch):
    """The new per-message opt-in works on its own, with the legacy global
    Settings toggle left off — proves the checkbox now genuinely controls
    Ask/Plan web search instead of being silently ignored."""
    fake_client = _FakeClient([_plain_answer_round()])
    _apply_common_mocks(monkeypatch, fake_client)
    # get_setting left as the real implementation → "web_search_enabled"
    # (the legacy global toggle) defaults to false.

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "What's new in AI?"}],
        model="claude-fake-model",
        user_id="",
        session_id="test-session-checkbox-on",
        mode="ask",
        web_search_enabled=True,
    ))

    call_kwargs = fake_client.messages.calls[0]
    assert call_kwargs["tools"] == [build_web_search_tool()]
    done = events[-1]
    assert done["type"] == "done"


@pytest.mark.asyncio
async def test_per_message_flag_false_and_global_setting_true_still_works(monkeypatch):
    """Backward compatibility: a user who already relies on the legacy
    global Settings toggle (and never touches the new per-message checkbox)
    keeps working exactly as before — the OR, not a replace."""
    fake_client = _FakeClient([_plain_answer_round()])
    _apply_common_mocks(monkeypatch, fake_client)
    monkeypatch.setattr(pipeline, "get_setting",
                         lambda key, default=None: "true" if key == "web_search_enabled" else default)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "What's new in AI?"}],
        model="claude-fake-model",
        user_id="",
        session_id="test-session-global-on",
        mode="plan",
        web_search_enabled=False,
    ))

    call_kwargs = fake_client.messages.calls[0]
    assert call_kwargs["tools"] == [build_web_search_tool()]
    done = events[-1]
    assert done["type"] == "done"


@pytest.mark.asyncio
async def test_both_off_no_tool(monkeypatch):
    """Neither the per-message checkbox nor the legacy global toggle is on
    → strictly no change from the pre-fix default-off behavior."""
    fake_client = _FakeClient([_plain_answer_round()])
    _apply_common_mocks(monkeypatch, fake_client)
    # get_setting left as the real implementation → global toggle defaults false.

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "What's new in AI?"}],
        model="claude-fake-model",
        user_id="",
        session_id="test-session-both-off",
        mode="ask",
        web_search_enabled=False,
    ))

    call_kwargs = fake_client.messages.calls[0]
    assert "tools" not in call_kwargs
    done = events[-1]
    assert done["type"] == "done"
    assert "web_search_sources" not in done


@pytest.mark.asyncio
async def test_edit_mode_ignores_per_message_flag_in_run_chat_stream(monkeypatch):
    """Sanity/scope guard: run_chat_stream's web-search gate is still hard-
    restricted to mode in ("ask", "plan"). Edit/Agent get web search through
    the SEPARATE, already-working run_natural_pipeline_stream path — this
    fix must never accidentally turn it on inside run_chat_stream for a mode
    that function isn't meant to serve."""
    fake_client = _FakeClient([_plain_answer_round()])
    _apply_common_mocks(monkeypatch, fake_client)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "Refactor this function."}],
        model="claude-fake-model",
        user_id="",
        session_id="test-session-edit-mode",
        mode="edit",
        web_search_enabled=True,
    ))

    call_kwargs = fake_client.messages.calls[0]
    assert "tools" not in call_kwargs
    done = events[-1]
    assert done["type"] == "done"
