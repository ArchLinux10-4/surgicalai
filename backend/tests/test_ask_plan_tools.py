"""
Ask/Plan mode tool-loop test (search_request / file_request).

Evidence for this fix: run_chat_stream (used by Ask/Plan mode) had zero
tag-handling for ANY model, so Claude could never search the codebase or
pull a full file when answering a question — it only saw a 300-line
preview of one file. This test proves the new bounded tool-round loop
(reusing the proven _resolve_search_multifile / _parse_filereq_content
helpers from the Edit pipeline) actually executes a search_request,
feeds results back to the model, and emits a clean final answer with no
raw tag markup — using a fully mocked Anthropic client (no live API call).
"""
import json
import sys
import types
import pytest

sys.path.insert(0, ".")

from services import pipeline


class _FakeEvent:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeDelta:
    def __init__(self, text=None, thinking=None):
        self.text = text
        self.thinking = thinking


def _text_events(text):
    """Simulate a Claude stream emitting one big text delta then stopping."""
    return [
        _FakeEvent("content_block_delta", delta=_FakeDelta(text=text)),
        _FakeEvent("content_block_stop"),
    ]


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


@pytest.mark.asyncio
async def test_ask_plan_search_request_round_trip(monkeypatch):
    session_files = [
        {"filename": "utils.py", "content": "def compute_total(items):\n    return sum(items)\n"},
        {"filename": "main.py", "content": "from utils import compute_total\n\ndef run():\n    return compute_total([1, 2, 3])\n"},
    ]

    round1_text = '<search_request>{"terms": ["compute_total"]}</search_request>'
    round2_text = "compute_total sums a list of items. No tags here."

    fake_client = _FakeClient([_text_events(round1_text), _text_events(round2_text)])

    monkeypatch.setattr(pipeline, "_is_claude_model", lambda m: True)
    monkeypatch.setattr(pipeline, "_is_gemini_model", lambda m: False)
    monkeypatch.setattr(pipeline, "_should_use_ollama", lambda m: False)
    monkeypatch.setattr(pipeline, "_get_anthropic_key", lambda user_id="": "fake-key")
    monkeypatch.setattr(pipeline, "AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setattr(pipeline, "_max_output_tokens", lambda m: 4096)
    monkeypatch.setattr(pipeline, "_get_thinking_kwargs", lambda m, budget: {})
    monkeypatch.setattr(pipeline, "_get_effort_kwargs", lambda m: {})

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "What does compute_total do?"}],
        model="claude-fake-model",
        user_id="",
        session_id="test-session-123",
        session_files=session_files,
    ))

    # Two model calls happened: initial + one tool round.
    assert len(fake_client.messages.calls) == 2

    # Second call's messages must include the tool (search) results, proving
    # the search was actually executed and fed back — not just dropped.
    second_call_msgs = fake_client.messages.calls[1]["messages"]
    joined = json.dumps(second_call_msgs)
    assert "Search results for" in joined
    assert "compute_total" in joined

    # Final token event must be clean prose with no raw tag markup leaked
    # to the user, and must not contain the round-1 tag text.
    token_events = [e for e in events if e.get("type") == "token"]
    assert token_events, "expected at least one token event"
    final_text = "".join(e["content"] for e in token_events)
    assert "<search_request>" not in final_text
    assert "compute_total sums a list" in final_text

    # Progress event shown to the user while the tool round executes.
    assert any(e.get("type") == "progress" for e in events)

    # Stream must always end with a done event.
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_ask_plan_gpt_search_request_round_trip(monkeypatch):
    """Proves the fix: GPT (and, by the same code path, Gemini) now gets
    the exact same search_request tool loop as Claude — this is the bug
    this patch closes. Before this fix, GPT/Gemini fell straight into the
    bare legacy branch below with zero tool access."""
    session_files = [
        {"filename": "utils.py", "content": "def compute_total(items):\n    return sum(items)\n"},
        {"filename": "main.py", "content": "from utils import compute_total\n\ndef run():\n    return compute_total([1, 2, 3])\n"},
    ]

    round1_text = '<search_request>{"terms": ["compute_total"]}</search_request>'
    round2_text = "compute_total sums a list of items. No tags here."

    class _FakeChoice:
        def __init__(self, content):
            self.delta = types.SimpleNamespace(content=content)

    class _FakeChunk:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    calls = []

    def _fake_chat_create(client, **kwargs):
        calls.append(kwargs)
        text = round1_text if len(calls) == 1 else round2_text
        return iter([_FakeChunk(text)])

    monkeypatch.setattr(pipeline, "_is_claude_model", lambda m: False)
    monkeypatch.setattr(pipeline, "_is_gemini_model", lambda m: False)
    monkeypatch.setattr(pipeline, "_should_use_ollama", lambda m: False)
    monkeypatch.setattr(pipeline, "_get_client_for_model", lambda model, user_id="", session_id="": object())
    monkeypatch.setattr(pipeline, "_chat_create", _fake_chat_create)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "What does compute_total do?"}],
        model="gpt-4.1",
        user_id="",
        session_id="test-session-gpt",
        session_files=session_files,
    ))

    # Two model calls happened: initial + one tool round — same shape as
    # the Claude test above, proving GPT gets the identical tool loop.
    assert len(calls) == 2
    second_call_msgs = calls[1]["messages"]
    joined = json.dumps(second_call_msgs)
    assert "Search results for" in joined
    assert "compute_total" in joined

    token_events = [e for e in events if e.get("type") == "token"]
    assert token_events, "expected at least one token event"
    final_text = "".join(e["content"] for e in token_events)
    assert "<search_request>" not in final_text
    assert "compute_total sums a list" in final_text
    assert any(e.get("type") == "progress" for e in events)
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_ask_plan_ollama_unchanged(monkeypatch):
    """Ollama is the one deliberate exclusion (it runs a fully separate
    offline streaming path that never reaches the tag protocol either
    way). This proves the exclusion and the legacy behavior are both
    still exactly what they were before this patch, even when
    session_files is supplied."""
    monkeypatch.setattr(pipeline, "_is_claude_model", lambda m: False)
    monkeypatch.setattr(pipeline, "_is_gemini_model", lambda m: False)
    monkeypatch.setattr(pipeline, "_should_use_ollama", lambda m: True)

    captured = {}

    class _FakeResp:
        def iter_lines(self):
            return iter([json.dumps({"message": {"content": "plain ollama answer, no tools"}})])

    class _FakeStreamCtx:
        def __enter__(self):
            return _FakeResp()

        def __exit__(self, *a):
            return False

    def _fake_httpx_stream(method, url, json=None, timeout=None):
        captured["messages"] = json["messages"]
        return _FakeStreamCtx()

    monkeypatch.setattr(pipeline.httpx, "stream", _fake_httpx_stream)

    session_files = [{"filename": "a.py", "content": "x = 1\n"}]

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        model="ollama:qwen2.5-coder:7b",
        user_id="",
        session_id="s-ollama",
        session_files=session_files,
    ))

    system_msg = next(m["content"] for m in captured["messages"] if m["role"] == "system")
    assert "search_request" not in system_msg
    assert "Session Files (index only)" not in system_msg
    assert any(e.get("type") == "token" and "plain ollama answer" in e.get("content", "") for e in events)
