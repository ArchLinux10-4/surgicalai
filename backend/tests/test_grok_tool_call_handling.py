"""
One test group per documented xAI/Grok gotcha, each proving the actual
mitigation in services/grok_provider.py against realistic mocked payloads
modelled on the cited real-world bug reports.

Gotcha -> mitigation -> cited source:
  1  assistant tool_calls with empty content 400s   -> sanitize_outgoing_messages   langchain#34140
  2  presence/frequency_penalty + stop rejected     -> strip_unsupported_params     docs.x.ai reasoning
  3  o-series reasoning_effort must not misfire     -> o_series_injection_can_misfire  codebase audit
  4  HTML-entity-encoded tool args                  -> decode_tool_call_arguments   openclaw#35173
  5  finish_reason "stop" with tool calls present    -> should_continue_agent_loop   vercel/ai#12218
  6  usage on every streamed chunk                   -> accumulate_stream_usage      docs.x.ai streaming
  7  custom tool named web_search silently refused   -> check_tool_name_collisions   pi-web-access#138
  8  prompt cache needs a stable conversation key    -> prompt_cache_headers         docs.x.ai grok-4-5
  9  429 = rate limit OR billing cap                 -> classify_429                 continue#10373
 10  malformed tool-call argument JSON               -> parse_tool_call_arguments    22ad61a / 7c1ac0f

NO LIVE API CALLS — mocked payloads only.
"""
import pathlib
import sys

import pytest

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

from services import grok_provider as gp  # noqa: E402

_PIPELINE_PATH = _BACKEND / "services" / "pipeline.py"


class _Rec:
    """Collects (event, kwargs) so tests can assert _dlog fired on the path."""

    def __init__(self):
        self.events = []

    def __call__(self, event, **kw):
        self.events.append((event, kw))

    @property
    def names(self):
        return [e for e, _ in self.events]


# ── Gotcha 1: "Each message must have at least one content element" ──────────

def test_gotcha1_assistant_tool_calls_message_gets_empty_string_content():
    rec = _Rec()
    # Shape reproduced from langchain#34140: AIMessage with tool_calls, no content.
    messages = [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    out = gp.sanitize_outgoing_messages(messages, dlog=rec)
    assert out[1]["content"] == ""
    assert out[1]["tool_calls"] == messages[1]["tool_calls"]
    assert "grok_sanitize_messages_done" in rec.names


def test_gotcha1_explicit_null_content_is_replaced_not_left_null():
    out = gp.sanitize_outgoing_messages(
        [{"role": "assistant", "content": None,
          "tool_calls": [{"id": "c", "type": "function",
                          "function": {"name": "f", "arguments": "{}"}}]}])
    assert out[0]["content"] == ""
    assert out[0]["content"] is not None


def test_gotcha1_does_not_mutate_caller_messages():
    msgs = [{"role": "assistant", "tool_calls": [{"id": "c"}]}]
    gp.sanitize_outgoing_messages(msgs)
    assert "content" not in msgs[0]


def test_gotcha1_leaves_normal_messages_untouched():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "assistant", "content": "partial",
         "tool_calls": [{"id": "c", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
    ]
    out = gp.sanitize_outgoing_messages(msgs)
    assert out[0] is msgs[0] and out[1] is msgs[1] and out[2] is msgs[2]


def test_gotcha1_empty_and_none_inputs_are_safe():
    rec = _Rec()
    assert gp.sanitize_outgoing_messages([], dlog=rec) == []
    assert gp.sanitize_outgoing_messages(None, dlog=rec) == []
    assert "grok_sanitize_messages_noop" in rec.names


# ── Gotcha 2: penalty/stop params are rejected, not ignored ─────────────────

def test_gotcha2_disallowed_params_are_stripped():
    rec = _Rec()
    cleaned = gp.strip_unsupported_params(
        {"model": "grok-4.5", "temperature": 0.2, "max_tokens": 4096,
         "presence_penalty": 0.5, "frequency_penalty": 0.5, "stop": ["\n\n"]},
        dlog=rec)
    assert set(cleaned) == {"model", "temperature", "max_tokens"}
    removed = [kw for e, kw in rec.events if e == "grok_strip_params_removed"]
    assert removed and set(removed[0]["removed"]) == {
        "presence_penalty", "frequency_penalty", "stop"}


def test_gotcha2_camelcase_spellings_also_stripped():
    cleaned = gp.strip_unsupported_params(
        {"presencePenalty": 1, "frequencyPenalty": 1, "stop": "x", "top_p": 0.9})
    assert cleaned == {"top_p": 0.9}


def test_gotcha2_clean_kwargs_pass_through_unchanged_and_are_not_mutated():
    rec = _Rec()
    original = {"model": "grok-4.5", "temperature": 0.0}
    cleaned = gp.strip_unsupported_params(original, dlog=rec)
    assert cleaned == original and cleaned is not original
    assert "grok_strip_params_clean" in rec.names


def test_gotcha2_empty_kwargs_safe():
    rec = _Rec()
    assert gp.strip_unsupported_params({}, dlog=rec) == {}
    assert gp.strip_unsupported_params(None, dlog=rec) == {}
    assert "grok_strip_params_noop" in rec.names


# ── Gotcha 3: o-series reasoning_effort injection must never fire for Grok ──

def test_gotcha3_grok_id_is_not_in_pipeline_reasoning_effort_models():
    """Read the REAL set out of pipeline.py and prove a Grok id is not in it,
    so the reasoning_effort kwarg injection cannot misfire. This is a live
    regression guard: adding a grok-* id to that set fails this test."""
    from services import pipeline  # heavy import, done lazily inside the test
    rec = _Rec()
    for model in ("grok-4.5", "grok-4.5-latest", "grok-4", "grok-code-fast-1"):
        assert gp.o_series_injection_can_misfire(
            model, pipeline.REASONING_EFFORT_MODELS, dlog=rec) is False
    assert "grok_o_series_gate_safe" in rec.names
    # And the gate genuinely does catch its intended o-series targets.
    assert "o3-mini" in pipeline.REASONING_EFFORT_MODELS


def test_gotcha3_detector_reports_true_if_a_grok_id_were_ever_added():
    rec = _Rec()
    assert gp.o_series_injection_can_misfire(
        "grok-4.5", {"o3-mini", "grok-4.5"}, dlog=rec) is True
    assert "grok_o_series_gate_would_misfire" in rec.names


def test_gotcha3_pipeline_has_defensive_comment_at_injection_site():
    src = _PIPELINE_PATH.read_text()
    idx = src.index('if base_model in REASONING_EFFORT_MODELS and "reasoning_effort" not in kwargs:')
    preceding = src[max(0, idx - 900):idx]
    assert "NOTE (Grok)" in preceding
    # No functional change to the injection itself.
    assert 'kwargs["reasoning_effort"] = _re.lower()' in src


# ── Gotcha 4: HTML-entity-encoded tool arguments ────────────────────────────

def test_gotcha4_html_entities_in_shell_command_are_decoded():
    rec = _Rec()
    # Exact failure shape from openclaw#35173.
    raw = '{"command": "cd /tmp &amp;&amp; echo &quot;hi&quot;"}'
    decoded = gp.decode_tool_call_arguments(raw, dlog=rec)
    assert decoded == '{"command": "cd /tmp && echo "hi""}'
    assert "&amp;" not in decoded and "&quot;" not in decoded
    assert "grok_tool_args_decoded" in rec.names


def test_gotcha4_clean_arguments_returned_byte_identical():
    rec = _Rec()
    raw = '{"command": "cd /tmp && ls"}'
    assert gp.decode_tool_call_arguments(raw, dlog=rec) == raw
    assert "grok_tool_args_decode_clean" in rec.names


def test_gotcha4_non_string_and_empty_inputs_safe():
    rec = _Rec()
    assert gp.decode_tool_call_arguments("", dlog=rec) == ""
    assert gp.decode_tool_call_arguments(None, dlog=rec) is None
    assert gp.decode_tool_call_arguments({"a": 1}, dlog=rec) == {"a": 1}


def test_gotcha4_decoded_arguments_are_json_parseable_afterwards():
    import json
    raw = '{"command": "a &amp;&amp; b", "flag": "&lt;x&gt;"}'
    parsed = json.loads(gp.decode_tool_call_arguments(raw))
    assert parsed["command"] == "a && b"
    assert parsed["flag"] == "<x>"


# ── Gotcha 5: finish_reason lies when tool calls are present ────────────────

def test_gotcha5_stop_with_tool_calls_still_continues_loop():
    rec = _Rec()
    # vercel/ai#12218: xAI raw "completed" -> mapped "stop", tool call present.
    tool_calls = [{"id": "call_0", "type": "function",
                   "function": {"name": "edit_code", "arguments": "{}"}}]
    assert gp.should_continue_agent_loop("stop", tool_calls, dlog=rec) is True
    assert "grok_loop_continue_tool_calls_present" in rec.names


def test_gotcha5_completed_raw_reason_with_tool_calls_continues():
    assert gp.should_continue_agent_loop("completed", [{"id": "c"}]) is True


def test_gotcha5_stop_with_no_tool_calls_ends_loop():
    rec = _Rec()
    assert gp.should_continue_agent_loop("stop", [], dlog=rec) is False
    assert gp.should_continue_agent_loop("stop", None, dlog=rec) is False
    assert "grok_loop_stop" in rec.names


def test_gotcha5_tool_calls_finish_reason_without_parsed_calls_continues():
    rec = _Rec()
    assert gp.should_continue_agent_loop("tool_calls", [], dlog=rec) is True
    assert "grok_loop_continue_finish_reason_only" in rec.names


def test_gotcha5_is_provider_agnostic_never_raises_on_weird_input():
    class Boom:
        def __bool__(self):
            raise RuntimeError("nope")

    rec = _Rec()
    assert gp.should_continue_agent_loop("stop", Boom(), dlog=rec) is False
    assert "grok_loop_decision_error" in rec.names


# ── Gotcha 6: usage on every streamed chunk (must not be summed) ────────────

def test_gotcha6_per_chunk_usage_is_overwritten_not_summed():
    rec = _Rec()
    running = None
    # xAI sends a full cumulative usage object on EVERY chunk.
    for u in ({"prompt_tokens": 100, "completion_tokens": 5},
              {"prompt_tokens": 100, "completion_tokens": 12},
              {"prompt_tokens": 100, "completion_tokens": 31}):
        running = gp.accumulate_stream_usage(running, u, dlog=rec)
    assert running == {"prompt_tokens": 100, "completion_tokens": 31}
    assert "grok_stream_usage_overwritten" in rec.names


def test_gotcha6_none_chunk_usage_keeps_previous_value():
    rec = _Rec()
    prev = {"prompt_tokens": 7, "completion_tokens": 3}
    assert gp.accumulate_stream_usage(prev, None, dlog=rec) is prev
    assert "grok_stream_usage_chunk_empty" in rec.names


def test_gotcha6_supports_sdk_object_usage_not_just_dicts():
    class Usage:
        prompt_tokens = 10
        completion_tokens = 4

    rec = _Rec()
    out = gp.accumulate_stream_usage(None, Usage(), dlog=rec)
    assert out.completion_tokens == 4
    assert "grok_stream_usage_overwritten" in rec.names


# ── Gotcha 7: reserved built-in tool-name collisions ───────────────────────

def test_gotcha7_web_search_collision_detected_nested_shape():
    rec = _Rec()
    tools = [
        {"type": "function", "function": {"name": "web_search", "parameters": {}}},
        {"type": "function", "function": {"name": "read_file", "parameters": {}}},
    ]
    assert gp.check_tool_name_collisions(tools, dlog=rec) == ["web_search"]
    assert "grok_tool_name_collision" in rec.names


def test_gotcha7_all_reserved_names_detected_flat_shape():
    tools = [{"type": "function", "name": n} for n in gp.GROK_RESERVED_TOOL_NAMES]
    assert gp.check_tool_name_collisions(tools) == list(gp.GROK_RESERVED_TOOL_NAMES)


def test_gotcha7_current_surgicalai_tool_names_do_not_collide():
    """The tools this app actually exposes today are collision-free — the check
    exists so a future `web_search` tool fails loudly instead of silently."""
    rec = _Rec()
    tools = [{"type": "function", "function": {"name": n}} for n in
             ("read_file", "edit_code", "replace_symbol", "list_files",
              "search_code", "no_change_needed", "github_search")]
    assert gp.check_tool_name_collisions(tools, dlog=rec) == []
    assert "grok_tool_names_clean" in rec.names


def test_gotcha7_empty_and_malformed_tool_lists_safe():
    assert gp.check_tool_name_collisions(None) == []
    assert gp.check_tool_name_collisions([]) == []
    assert gp.check_tool_name_collisions(["not-a-dict", 5, None]) == []


# ── Gotcha 8: stable prompt-cache routing key ──────────────────────────────

def test_gotcha8_cache_key_is_stable_per_session():
    rec = _Rec()
    a = gp.prompt_cache_key_for_session("sess-abc", "user-1", dlog=rec)
    b = gp.prompt_cache_key_for_session("sess-abc", "user-1", dlog=rec)
    assert a == b == "surgicalai-sess-abc"
    assert "grok_cache_key_built" in rec.names


def test_gotcha8_cache_key_differs_across_sessions():
    assert (gp.prompt_cache_key_for_session("s1", "u")
            != gp.prompt_cache_key_for_session("s2", "u"))


def test_gotcha8_cache_key_falls_back_to_user_then_anon():
    assert gp.prompt_cache_key_for_session("", "user-9") == "surgicalai-user-9"
    assert gp.prompt_cache_key_for_session("", "") == "surgicalai-anon"


def test_gotcha8_header_name_is_x_grok_conv_id():
    rec = _Rec()
    headers = gp.prompt_cache_headers("sess-abc", "user-1", dlog=rec)
    assert headers == {"x-grok-conv-id": "surgicalai-sess-abc"}
    assert "grok_cache_headers_built" in rec.names


# ── Gotcha 9: 429 rate limit vs. billing cap ───────────────────────────────

def test_gotcha9_billing_cap_429_classified_as_non_retryable():
    rec = _Rec()
    # Verbatim message shape from continue#10373.
    body = ('{"error": "Your team 1234 has either used all available credits '
            'or reached its monthly spending limit."}')
    assert gp.classify_429(body, dlog=rec) == gp.GROK_429_BILLING
    kinds = dict((e, kw) for e, kw in rec.events)
    assert kinds["grok_429_billing_cap"]["retryable"] is False


def test_gotcha9_plain_rate_limit_429_classified_as_retryable():
    rec = _Rec()
    body = '{"error": "Rate limit exceeded, please slow down."}'
    assert gp.classify_429(body, dlog=rec) == gp.GROK_429_RATE_LIMIT
    kinds = dict((e, kw) for e, kw in rec.events)
    assert kinds["grok_429_rate_limit"]["retryable"] is True


def test_gotcha9_accepts_dict_body_and_unknown_body_defaults_to_rate_limit():
    assert gp.classify_429({"error": {"message": "monthly spending limit"}}) == gp.GROK_429_BILLING
    assert gp.classify_429(None) == gp.GROK_429_RATE_LIMIT
    assert gp.classify_429("") == gp.GROK_429_RATE_LIMIT
    assert gp.classify_429("something else entirely") == gp.GROK_429_RATE_LIMIT


def test_gotcha9_user_messages_differ_and_billing_says_do_not_retry():
    rec = _Rec()
    billing = gp.grok_429_user_message(gp.GROK_429_BILLING, dlog=rec)
    rate = gp.grok_429_user_message(gp.GROK_429_RATE_LIMIT, dlog=rec)
    assert "console.x.ai" in billing and "will not help" in billing
    assert "rate limit" in rate.lower() and billing != rate
    assert "grok_429_message_billing" in rec.names
    assert "grok_429_message_rate_limit" in rec.names


# ── Gotcha 10: malformed tool-call argument JSON ────────────────────────────

def test_gotcha10_valid_arguments_parse_with_no_feedback():
    rec = _Rec()
    ok, parsed, feedback = gp.parse_tool_call_arguments(
        '{"path": "a.py", "old_code": "x"}', dlog=rec, tool_name="edit_code")
    assert ok is True and parsed["path"] == "a.py" and feedback == ""
    assert "grok_tool_args_parsed" in rec.names


def test_gotcha10_malformed_json_echoes_the_models_own_snippet_back():
    rec = _Rec()
    broken = '{"path": "a.py", "old_code": "unterminated'
    ok, parsed, feedback = gp.parse_tool_call_arguments(
        broken, dlog=rec, tool_name="edit_code")
    assert ok is False and parsed == {}
    # Same defensive pattern as the shipped GPT fix (22ad61a / 7c1ac0f):
    # the model must see its own broken text.
    assert broken in feedback
    assert "edit_code" in feedback
    assert "grok_tool_args_json_error" in rec.names


def test_gotcha10_html_encoded_then_valid_json_parses_end_to_end():
    """Gotcha 4 and 10 compose: entity-decoding happens before JSON parsing."""
    ok, parsed, feedback = gp.parse_tool_call_arguments(
        '{"command": "cd /tmp &amp;&amp; ls"}', tool_name="exec")
    assert ok is True and parsed["command"] == "cd /tmp && ls" and feedback == ""


def test_gotcha10_empty_arguments_get_explicit_feedback():
    rec = _Rec()
    ok, parsed, feedback = gp.parse_tool_call_arguments("", dlog=rec, tool_name="edit_code")
    assert ok is False and parsed == {} and "empty arguments" in feedback
    assert "grok_tool_args_empty" in rec.names


def test_gotcha10_non_object_json_rejected_with_feedback():
    rec = _Rec()
    ok, parsed, feedback = gp.parse_tool_call_arguments('["a","b"]', dlog=rec, tool_name="t")
    assert ok is False and parsed == {} and "JSON object" in feedback
    assert "grok_tool_args_not_object" in rec.names


def test_gotcha10_never_raises_on_any_input():
    for bad in (None, 5, {"already": "dict"}, b"bytes", "null", "true"):
        ok, parsed, feedback = gp.parse_tool_call_arguments(bad, tool_name="t")
        assert isinstance(ok, bool) and isinstance(parsed, dict)


# ── Composite shaper: all outgoing mitigations in one call ──────────────────

def test_shape_grok_request_applies_all_outgoing_mitigations():
    rec = _Rec()
    messages = [{"role": "assistant",
                 "tool_calls": [{"id": "c", "type": "function",
                                 "function": {"name": "f", "arguments": "{}"}}]}]
    kwargs = {"temperature": 0.1, "presence_penalty": 0.4, "stop": ["x"]}
    tools = [{"type": "function", "function": {"name": "web_search"}}]

    msgs, kw, warnings = gp.shape_grok_request(
        messages, kwargs, dlog=rec, session_id="s1", user_id="u1", tools=tools)

    assert msgs[0]["content"] == ""
    assert kw == {"temperature": 0.1}
    assert warnings and "web_search" in warnings[0]
    assert "grok_request_shaped_with_warnings" in rec.names


def test_shape_grok_request_clean_input_has_no_warnings():
    rec = _Rec()
    msgs, kw, warnings = gp.shape_grok_request(
        [{"role": "user", "content": "hi"}], {"temperature": 0.0}, dlog=rec,
        tools=[{"type": "function", "function": {"name": "read_file"}}])
    assert warnings == []
    assert "grok_request_shaped" in rec.names


# ── Generic (non-provider-specific) malformed-JSON handling already covers Grok

def test_existing_generic_tool_json_error_handling_covers_grok_unchanged():
    """The already-shipped malformed-tool-argument-JSON handling in Agent Mode's
    non-Claude tool loop (pipeline.py, dlog event
    `agent_mode_gpt_correction_args_json_error`) is reached from the
    `else:` side of `if _agent_use_claude:` — i.e. ANY non-Claude architect
    model, including Grok — and keys only off the OpenAI-shaped
    `_cotc.function.arguments`. So Grok tool calls are already covered with no
    code change. Proven by source read rather than asserted in prose."""
    src = _PIPELINE_PATH.read_text()
    assert "agent_mode_gpt_correction_args_json_error" in src
    idx = src.index("agent_mode_gpt_correction_args_json_error")
    window = src[max(0, idx - 1200):idx]
    # Reached via the generic non-Claude branch, not a GPT-only model check.
    assert "if _agent_use_claude:" in window or "_corr_oai_tcalls" in window
    assert "_agent_use_claude = _is_claude_model(architect_model)" in src
    # It echoes the model's own broken payload back — provider-agnostic.
    assert '"your_call_was"' in src


def test_agent_mode_non_claude_branch_routes_grok_to_xai_client():
    """Real gap found during this work: Agent Mode's non-Claude branch built a
    raw OpenAI client (`_get_client(user_id)`), which would have sent a
    `grok-4.5` request to api.openai.com. Grok is now routed to the xAI client
    there; the non-Grok path is unchanged."""
    src = _PIPELINE_PATH.read_text()
    idx = src.index("_agent_use_claude = _is_claude_model(architect_model)")
    block = src[idx:idx + 1600]
    assert "if _is_grok_model(architect_model):" in block
    # Additive session_id passthrough (grok-cache-header fix): session_id is
    # in scope here (used in the adjacent _dlog call), so it is now forwarded.
    assert "get_grok_client(user_id, dlog=_dlog, session_id=session_id)" in block
    assert 'agent_mode_grok_client' in block
    # GPT/other-model behaviour untouched.
    assert "_agent_oai_client = _get_client(user_id)" in block
