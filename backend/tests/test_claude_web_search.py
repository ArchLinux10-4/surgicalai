"""
Claude web-research (Ask/Plan mode) tests — proves:

  1. `build_web_search_tool` returns Anthropic's documented tool shape.
  2. `ClaudeWebSearchStreamTracker` correctly turns the three new streaming
     event shapes (server_tool_use / input_json_delta / web_search_tool_result,
     including the error variant) into SSE-ready dicts, with no exceptions
     leaking into the caller even on malformed input.
  3. End-to-end: `run_chat_stream(mode="ask", ...)` with the setting on adds
     the tool to the Anthropic call, forwards live search events, and
     surfaces a de-duplicated Sources list on the final `done` event —
     using a fully mocked Anthropic client (no live API call). Also proves
     the feature is OFF by default (no `tools` kwarg, no sources) so the
     already-working Claude Ask/Plan path is unaffected unless a user
     explicitly opts in.
"""
import json
import sys
import types

import pytest

sys.path.insert(0, ".")

from services import pipeline
from services.claude_web_search import (
    build_web_search_tool,
    ClaudeWebSearchStreamTracker,
    CLAUDE_WEB_SEARCH_TOOL_TYPE,
    CLAUDE_WEB_SEARCH_TOOL_NAME,
)


# ─── Unit: tool builder ──────────────────────────────────────────────────

def test_build_web_search_tool_shape():
    tool = build_web_search_tool(max_uses=3)
    assert tool == {
        "type": CLAUDE_WEB_SEARCH_TOOL_TYPE,
        "name": CLAUDE_WEB_SEARCH_TOOL_NAME,
        "max_uses": 3,
    }


def test_build_web_search_tool_default_max_uses():
    tool = build_web_search_tool()
    assert tool["max_uses"] == 5


# ─── Unit: stream tracker ────────────────────────────────────────────────

class _Block:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Event:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Delta:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_tracker_emits_search_start_query_and_results():
    tracker = ClaudeWebSearchStreamTracker(session_id="s1", user_id="u1")

    events = [
        _Event("content_block_start", index=1, content_block=_Block(
            type="server_tool_use", id="srvtoolu_1", name="web_search")),
        _Event("content_block_delta", index=1, delta=_Delta(
            type="input_json_delta", partial_json='{"query":')),
        _Event("content_block_delta", index=1, delta=_Delta(
            type="input_json_delta", partial_json='"claude shannon birth date"}')),
        _Event("content_block_stop", index=1),
        _Event("content_block_start", index=2, content_block=_Block(
            type="web_search_tool_result", tool_use_id="srvtoolu_1", content=[
                {"type": "web_search_result", "url": "https://en.wikipedia.org/wiki/Claude_Shannon",
                 "title": "Claude Shannon - Wikipedia", "page_age": "April 30, 2025"},
            ])),
    ]

    out = []
    for ev in events:
        out.extend(tracker.on_event(ev))

    types_seen = [o["type"] for o in out]
    assert types_seen == ["web_search_start", "web_search_query", "web_search_results"]

    query_event = next(o for o in out if o["type"] == "web_search_query")
    assert query_event["content"] == "claude shannon birth date"

    results_event = next(o for o in out if o["type"] == "web_search_results")
    assert results_event["error"] is None
    assert len(results_event["results"]) == 1
    r = results_event["results"][0]
    assert r["url"] == "https://en.wikipedia.org/wiki/Claude_Shannon"
    assert r["title"] == "Claude Shannon - Wikipedia"
    assert r["domain"] == "en.wikipedia.org"

    sources = tracker.get_sources()
    assert len(sources) == 1
    assert sources[0]["url"] == "https://en.wikipedia.org/wiki/Claude_Shannon"


def test_tracker_dedupes_sources_by_url_across_calls():
    tracker = ClaudeWebSearchStreamTracker()
    result_block = _Block(type="web_search_tool_result", tool_use_id="t1", content=[
        {"type": "web_search_result", "url": "https://example.com/a", "title": "A"},
        {"type": "web_search_result", "url": "https://example.com/a", "title": "A dup"},
        {"type": "web_search_result", "url": "https://example.com/b", "title": "B"},
    ])
    tracker.on_event(_Event("content_block_start", index=5, content_block=result_block))
    sources = tracker.get_sources()
    assert [s["url"] for s in sources] == ["https://example.com/a", "https://example.com/b"]


def test_tracker_handles_error_result_without_raising():
    tracker = ClaudeWebSearchStreamTracker()
    err_block = _Block(type="web_search_tool_result", tool_use_id="t1",
                        content={"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"})
    out = tracker.on_event(_Event("content_block_start", index=1, content_block=err_block))
    assert out == [{"type": "web_search_results", "results": [], "error": "max_uses_exceeded"}]
    assert tracker.get_sources() == []


def test_tracker_never_raises_on_malformed_event():
    tracker = ClaudeWebSearchStreamTracker()
    # Completely unrelated event shape (e.g. a plain text block) must be a no-op.
    out = tracker.on_event(_Event("content_block_start", index=0, content_block=_Block(type="text")))
    assert out == []
    # An event missing expected attributes must not raise either.
    out2 = tracker.on_event(object())
    assert out2 == []


# ─── Integration: run_chat_stream(mode="ask") with the setting on ───────

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


def _search_and_answer_round():
    """One round: Claude searches once, then answers with the result found."""
    result_block = _Block(type="web_search_tool_result", tool_use_id="srvtoolu_1", content=[
        {"type": "web_search_result", "url": "https://example.com/weather",
         "title": "NYC Weather", "page_age": "today"},
    ])
    return [
        _Event("content_block_start", index=0, content_block=_Block(type="server_tool_use", name="web_search")),
        _Event("content_block_delta", index=0, delta=_Delta(type="input_json_delta", partial_json='{"query":"NYC weather"}')),
        _Event("content_block_stop", index=0),
        _Event("content_block_start", index=1, content_block=result_block),
        _Event("content_block_delta", index=2, delta=_Delta(text="It's sunny in NYC today.")),
        _Event("content_block_stop", index=2),
    ]


@pytest.mark.asyncio
async def test_web_search_enabled_adds_tool_and_surfaces_sources(monkeypatch):
    fake_client = _FakeClient([_search_and_answer_round()])

    monkeypatch.setattr(pipeline, "_is_claude_model", lambda m: True)
    monkeypatch.setattr(pipeline, "_is_gemini_model", lambda m: False)
    monkeypatch.setattr(pipeline, "_should_use_ollama", lambda m: False)
    monkeypatch.setattr(pipeline, "_get_anthropic_key", lambda user_id="": "fake-key")
    monkeypatch.setattr(pipeline, "AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setattr(pipeline, "_max_output_tokens", lambda m: 4096)
    monkeypatch.setattr(pipeline, "_get_thinking_kwargs", lambda m, budget: {})
    monkeypatch.setattr(pipeline, "_get_effort_kwargs", lambda m: {})
    monkeypatch.setattr(pipeline, "get_setting",
                         lambda key, default=None: "true" if key == "web_search_enabled" else default)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "What's the weather in NYC?"}],
        model="claude-fake-model",
        user_id="",
        session_id="test-session-ws",
        mode="ask",
    ))

    # The Anthropic call must actually carry the web_search tool.
    call_kwargs = fake_client.messages.calls[0]
    assert call_kwargs["tools"] == [build_web_search_tool()]

    # Live search events forwarded to the frontend.
    assert any(e["type"] == "web_search_start" for e in events)
    assert any(e["type"] == "web_search_query" and e["content"] == "NYC weather" for e in events)
    assert any(e["type"] == "web_search_results" for e in events)

    # Final done event carries the de-duplicated sources list.
    done = events[-1]
    assert done["type"] == "done"
    assert done["web_search_sources"] == [{
        "url": "https://example.com/weather",
        "title": "NYC Weather",
        "page_age": "today",
        "domain": "example.com",
    }]

    # Legacy live-token streaming is untouched — the answer still streamed.
    token_text = "".join(e["content"] for e in events if e["type"] == "token")
    assert "sunny in NYC" in token_text


@pytest.mark.asyncio
async def test_web_search_disabled_by_default_no_tool_no_sources(monkeypatch):
    """Proves the feature is strictly opt-in: with the setting left at its
    default (off), the already-working Claude Ask/Plan path is byte-for-byte
    unaffected — no `tools` kwarg sent, no web_search_sources on `done`."""
    plain_round = [
        _Event("content_block_delta", index=0, delta=_Delta(text="Paris is the capital of France.")),
        _Event("content_block_stop", index=0),
    ]
    fake_client = _FakeClient([plain_round])

    monkeypatch.setattr(pipeline, "_is_claude_model", lambda m: True)
    monkeypatch.setattr(pipeline, "_is_gemini_model", lambda m: False)
    monkeypatch.setattr(pipeline, "_should_use_ollama", lambda m: False)
    monkeypatch.setattr(pipeline, "_get_anthropic_key", lambda user_id="": "fake-key")
    monkeypatch.setattr(pipeline, "AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setattr(pipeline, "_max_output_tokens", lambda m: 4096)
    monkeypatch.setattr(pipeline, "_get_thinking_kwargs", lambda m, budget: {})
    monkeypatch.setattr(pipeline, "_get_effort_kwargs", lambda m: {})
    # get_setting left as the real implementation → "web_search_enabled" defaults to false.

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "What's the capital of France?"}],
        model="claude-fake-model",
        user_id="",
        session_id="test-session-ws-off",
        mode="ask",
    ))

    call_kwargs = fake_client.messages.calls[0]
    assert "tools" not in call_kwargs

    done = events[-1]
    assert done["type"] == "done"
    assert "web_search_sources" not in done
    assert not any(e["type"].startswith("web_search_") for e in events)
