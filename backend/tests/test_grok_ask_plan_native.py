"""Tests for the Grok native Ask/Plan tool-calling loop (grok_ask_plan_native.py).

Proves two things with real code/data (no live network call — fake OpenAI/xAI
SDK stream chunks, same technique as
tests/test_grok_native_edit_loop_integration.py):

1. Structural fix: when Grok is given real search/file/github tools instead of
   the <search_request>/... XML tag contract, a tool-call round is correctly
   dispatched through the (unmodified) ``_ask_plan_execute_tool_round``-shaped
   callback and the model's follow-up answer is streamed back — instead of
   the tag path's failure mode (Grok narrates, tag scanner finds nothing,
   the lookup never happens).
2. The Grok-only sandwich reinforcement text is present, ASK/PLAN-labelled
   correctly, and never mutates any Claude/GPT-facing string.

Run from backend/:  pytest tests/test_grok_ask_plan_native.py
"""
import asyncio
import json

import pytest

from services import grok_ask_plan_native as gapn


# ── Fake OpenAI/xAI streaming SDK objects (same shapes used elsewhere) ─────
class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _TCDelta:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _Choice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, delta=None, finish_reason=None):
        self.choices = [_Choice(delta, finish_reason)]


def _iter_chunks_fn(stream, **_kw):
    """Test stand-in for pipeline._iter_openai_stream_chunks: just yields the
    canned chunk list unchanged (the empty-choices guard itself is already
    covered by tests/test_grok_stream_empty_choices.py)."""
    return iter(stream)


def _drain(agen):
    async def _run():
        return [x async for x in agen]
    return asyncio.run(_run())


def _sse_events(chunks_yielded):
    return [json.loads(c[6:]) for c in chunks_yielded]


def test_ask_plan_tools_are_read_only():
    """build_grok_agent_tools(mode="ask") must never include write tools —
    this is what makes the loop safe to reuse for Ask/Plan at all."""
    from services.grok_agent_tools import build_grok_agent_tools
    tools = build_grok_agent_tools(mode="ask", github_enabled=True)
    names = {t["function"]["name"] for t in tools}
    assert "write_surgical_edit" not in names
    assert "write_new_file" not in names
    assert "write_edit_plan" not in names
    assert "request_search" in names and "request_file" in names
    assert "request_github" in names  # github_enabled=True
    assert "report_blocked" in names


def test_reinforcement_text_labels_ask_and_plan_correctly():
    ask_text = gapn.build_grok_ask_plan_reinforcement("ask")
    plan_text = gapn.build_grok_ask_plan_reinforcement("plan")
    assert "ASK MODE" in ask_text
    assert "PLAN MODE" in plan_text
    assert "Edit/Agent mode" in ask_text and "Edit/Agent mode" in plan_text
    # sandwich pattern: this is a distinct, standalone reinforcement string,
    # not a mutation of the shared CHAT_PERSONA/_ASK_DIRECTIVE text.
    assert "surgical_edit" in ask_text


def test_dispatch_context_request_routes_search_kind_correctly():
    """The _FakeMatch/_dispatch_context_request plumbing must call the
    injected execute_tool_round_fn with sr_match set (and fr_match/gr_match
    None) when tag_kind == 'search', passing the exact JSON body through
    .group(1) unchanged."""
    captured = {}

    def _fake_execute(*, sr_match, fr_match, gr_match, **_kw):
        captured["sr_body"] = sr_match.group(1) if sr_match else None
        captured["fr_match"] = fr_match
        captured["gr_match"] = gr_match
        return ["fake search result"]

    body = json.dumps({"terms": ["handle_login"], "reason": "need it"})
    result = gapn._dispatch_context_request(
        "search", body, execute_tool_round_fn=_fake_execute,
        symbol_maps_by_name={}, file_content_lookup={},
        user_id="u", session_id="s", tool_round=0,
    )
    assert result == "fake search result"
    assert captured["sr_body"] == body
    assert captured["fr_match"] is None
    assert captured["gr_match"] is None


def test_native_loop_dispatches_search_then_streams_final_answer():
    """End-to-end (fake stream): round 1 emits a request_search tool call,
    the loop dispatches it through execute_tool_round_fn, round 2 is a plain
    text answer — proving the round-trip actually reaches a real answer
    instead of the tag path's narration-only failure mode."""
    round1_args = json.dumps({"terms": ["handle_login"], "reason": "need it"})
    round1 = [
        _Chunk(_Delta(tool_calls=[
            _TCDelta(0, id="call_1", name="request_search", arguments=round1_args)])),
        _Chunk(finish_reason="tool_calls"),
    ]
    round2 = [
        _Chunk(_Delta(content="handle_login returns 200 on success.")),
        _Chunk(finish_reason="stop"),
    ]
    rounds = [round1, round2]
    call_log = []

    def _fake_chat_create(client, model=None, messages=None, temperature=None,
                          tools=None, stream=None):
        call_log.append({"messages": list(messages), "tools": tools})
        return rounds[len(call_log) - 1]

    def _fake_execute_tool_round(*, sr_match, fr_match, gr_match, **_kw):
        assert sr_match is not None
        return ["FILE search hit: def handle_login(req): return 200"]

    agen = gapn.run_grok_ask_plan_native_stream(
        client=object(),
        chat_model="grok-4.5",
        all_messages=[
            {"role": "system", "content": "You are SurgicalAI."},
            {"role": "user", "content": "How does login work?"},
        ],
        mode="ask",
        gh_nat_enabled=False,
        gh_known_repos=[],
        symbol_maps_by_name={},
        file_content_lookup={},
        execute_tool_round_fn=_fake_execute_tool_round,
        chat_create_fn=_fake_chat_create,
        iter_chunks_fn=_iter_chunks_fn,
        max_rounds=24,
        deadline_s=480,
        session_id="s", user_id="u", dlog=lambda *a, **k: None,
    )
    events = _sse_events(_drain(agen))

    assert len(call_log) == 2, "expected exactly two model rounds (tool call + final answer)"
    # No write tools ever offered.
    tool_names = {t["function"]["name"] for t in call_log[0]["tools"]}
    assert "write_surgical_edit" not in tool_names
    # Progress event fired while dispatching the tool.
    assert any(e["type"] == "progress" for e in events)
    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert tokens and "handle_login returns 200" in tokens[-1]
    # The final round's outgoing messages carry the tool-result observation.
    final_round_messages = call_log[1]["messages"]
    assert any(m.get("role") == "tool" for m in final_round_messages)


def test_native_loop_plain_answer_needs_no_tool_round():
    """No files/github needed this turn: single round, plain text, done —
    same shape the legacy byte-identical no-tools path already guarantees."""
    chunks = [
        _Chunk(_Delta(content="SurgicalAI is a coding assistant.")),
        _Chunk(finish_reason="stop"),
    ]

    def _fake_chat_create(client, model=None, messages=None, temperature=None,
                          tools=None, stream=None):
        return chunks

    agen = gapn.run_grok_ask_plan_native_stream(
        client=object(), chat_model="grok-4.5",
        all_messages=[{"role": "user", "content": "What is this app?"}],
        mode="plan", gh_nat_enabled=False, gh_known_repos=[],
        symbol_maps_by_name={}, file_content_lookup={},
        execute_tool_round_fn=lambda **_kw: (_ for _ in ()).throw(
            AssertionError("tool round should never run")),
        chat_create_fn=_fake_chat_create, iter_chunks_fn=_iter_chunks_fn,
        max_rounds=24, deadline_s=480, session_id="s", user_id="u",
        dlog=lambda *a, **k: None,
    )
    events = _sse_events(_drain(agen))
    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert tokens == ["SurgicalAI is a coding assistant."]


def test_native_loop_report_blocked_yields_reason_as_final_answer():
    blocked_args = json.dumps({"reason": "the referenced file was never attached"})
    chunks = [
        _Chunk(_Delta(tool_calls=[
            _TCDelta(0, id="call_1", name="report_blocked", arguments=blocked_args)])),
        _Chunk(finish_reason="tool_calls"),
    ]

    def _fake_chat_create(client, model=None, messages=None, temperature=None,
                          tools=None, stream=None):
        return chunks

    agen = gapn.run_grok_ask_plan_native_stream(
        client=object(), chat_model="grok-4.5",
        all_messages=[{"role": "user", "content": "Explain foo.py"}],
        mode="ask", gh_nat_enabled=False, gh_known_repos=[],
        symbol_maps_by_name={}, file_content_lookup={},
        execute_tool_round_fn=lambda **_kw: [],
        chat_create_fn=_fake_chat_create, iter_chunks_fn=_iter_chunks_fn,
        max_rounds=24, deadline_s=480, session_id="s", user_id="u",
        dlog=lambda *a, **k: None,
    )
    events = _sse_events(_drain(agen))
    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert len(tokens) == 1
    assert "the referenced file was never attached" in tokens[0]


def test_native_loop_round_cap_yields_budget_message():
    """If the model keeps calling tools forever, the round cap must still
    terminate the loop with a friendly message (mirrors the tag loop's
    budget_exhausted_rounds behavior) instead of looping unboundedly."""
    search_args = json.dumps({"terms": ["x"], "reason": "r"})

    def _always_tool_call(client, model=None, messages=None, temperature=None,
                          tools=None, stream=None):
        return [
            _Chunk(_Delta(tool_calls=[
                _TCDelta(0, id="call_n", name="request_search", arguments=search_args)])),
            _Chunk(finish_reason="tool_calls"),
        ]

    agen = gapn.run_grok_ask_plan_native_stream(
        client=object(), chat_model="grok-4.5",
        all_messages=[{"role": "user", "content": "Loop forever"}],
        mode="ask", gh_nat_enabled=False, gh_known_repos=[],
        symbol_maps_by_name={}, file_content_lookup={},
        execute_tool_round_fn=lambda **_kw: ["result"],
        chat_create_fn=_always_tool_call, iter_chunks_fn=_iter_chunks_fn,
        max_rounds=2, deadline_s=480, session_id="s", user_id="u",
        dlog=lambda *a, **k: None,
    )
    events = _sse_events(_drain(agen))
    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert len(tokens) == 1
    assert "lookup budget" in tokens[0]


def test_native_loop_emits_tool_named_and_reasoning_progress():
    """Real-time UX: reasoning deltas → thinking_*; tool name → progress."""
    search_args = json.dumps({"terms": ["login"], "reason": "find auth"})
    rounds = [
        [
            _Chunk(_Delta(reasoning_content="I should search for login.")),
            _Chunk(_Delta(tool_calls=[
                _TCDelta(0, id="call_1", name="request_search", arguments=search_args)])),
            _Chunk(finish_reason="tool_calls"),
        ],
        [
            _Chunk(_Delta(content="Auth is in login.py.")),
            _Chunk(finish_reason="stop"),
        ],
    ]
    call_log = []
    events_log = []

    def _fake_chat_create(client, model=None, messages=None, temperature=None,
                          tools=None, stream=None):
        call_log.append(1)
        return rounds[len(call_log) - 1]

    def _dlog(event, **kw):
        events_log.append(event)

    agen = gapn.run_grok_ask_plan_native_stream(
        client=object(), chat_model="grok-4.5",
        all_messages=[{"role": "user", "content": "How does login work?"}],
        mode="ask", gh_nat_enabled=False, gh_known_repos=[],
        symbol_maps_by_name={}, file_content_lookup={},
        execute_tool_round_fn=lambda **_kw: ["hit"],
        chat_create_fn=_fake_chat_create, iter_chunks_fn=_iter_chunks_fn,
        max_rounds=24, deadline_s=480, session_id="s", user_id="u",
        dlog=_dlog,
    )
    events = _sse_events(_drain(agen))
    types = [e["type"] for e in events]
    assert "thinking_start" in types
    assert "thinking" in types
    assert "thinking_end" in types
    progress = [e["content"] for e in events if e["type"] == "progress"]
    assert any("request_search" in p for p in progress)
    assert any("Next:" in p or "Looking at the code" in p for p in progress)
    assert "grok_agent_tool_named" in events_log
    assert "grok_agent_reasoning_delta" in events_log
    assert "grok_agent_tool_progress" in events_log
