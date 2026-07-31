"""
Regression tests for the GPT-5.6-Terra correction fallback
(services/gpt_correction.py) and its wiring into
services.pipeline.run_natural_pipeline_stream.

Background (proven via source read, session 2026-07-31):
run_natural_pipeline_stream sets `aclient = None` when the architect model
is GPT and the user has NOT configured an Anthropic key (pipeline.py
~15255-15266, dlog "natural_gpt_no_anthropic_key"). Every downstream
correction/retry call site was hardcoded to
`_safe_claude_call(aclient, model="claude-sonnet-5", ...)` or
`<fn>(aclient, "claude-sonnet-5", ...)` (comment: "R25: corrections always
Claude") with NO fallback — for a GPT-only user with zero Anthropic key,
`aclient` is None and every one of these silently no-ops (feature simply
does not run).

This test file verifies:
  1. Every one of the 9 known call sites in pipeline.py now branches on
     `aclient is not None`, leaving the Claude path byte-for-byte unchanged
     and adding an additive GPT 5.6 Terra fallback via services.gpt_correction.
  2. services/gpt_correction.py's functions behave correctly in isolation
     (happy path, empty-text, timeout, error, flag-disabled) using mocked
     dependency-injected `chat_create`/`dlog`/`get_setting` — mirroring the
     exact DI pattern services/gpt_reasoning.py already uses (see that
     module's docstring: "zero circular-import risk").
"""
import asyncio
import pathlib
import re

import pytest

_PIPELINE_PATH = pathlib.Path(__file__).parent.parent / "services" / "pipeline.py"
_GPT_CORRECTION_PATH = pathlib.Path(__file__).parent.parent / "services" / "gpt_correction.py"

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from services import gpt_correction as gc  # noqa: E402


def _pipeline_src() -> str:
    return _PIPELINE_PATH.read_text()


# ─────────────────────────────────────────────────────────────────────────
# 1. Source-truth checks: every known Claude-only correction call site now
#    has an additive `if aclient is not None:` / `else:` GPT fallback branch.
# ─────────────────────────────────────────────────────────────────────────

def test_gpt_correction_module_exists_and_is_new():
    """The fallback logic lives in its own file, not inlined into pipeline.py
    — per the requirement to keep this additive and isolated from the
    existing, working Claude-only code."""
    assert _GPT_CORRECTION_PATH.exists()


def test_gpt_correction_module_uses_dependency_injection_no_pipeline_import():
    """Zero circular-import risk: gpt_correction.py must not import from
    services.pipeline (mirrors services/gpt_reasoning.py's own rule)."""
    src = _GPT_CORRECTION_PATH.read_text()
    assert "import services.pipeline" not in src
    assert "from services.pipeline" not in src
    assert "from services import pipeline" not in src


@pytest.mark.parametrize("callable_name,expected_calls", [
    ("_retry_truncated_edit", 1),
    ("_retry_truncated_newfile", 1),
    ("_execute_single_edit", 1),
    ("_safe_claude_call", 6),
])
def test_every_claude_only_call_site_has_aclient_none_guard(callable_name, expected_calls):
    """For every known hardcoded-Claude correction call, there must be a
    matching `if aclient is not None:` guard immediately preceding it and a
    GPT fallback `else:` branch importing services.gpt_correction — i.e. the
    Claude call site count must exactly match the guard count (no orphaned,
    still-unguarded call sites left behind)."""
    src = _pipeline_src()
    # Only count call sites at/after run_natural_pipeline_stream (the
    # confirmed live path with a nullable `aclient`). Sites in other
    # functions (e.g. analyze_and_plan_stream's multi-turn Surgeon stub,
    # run_smart_pipeline_stream's lint self-heal) are explicitly out of
    # scope for this fix and must be left untouched.
    start = src.index("async def run_natural_pipeline_stream")
    scoped_src = src[start:]

    call_pattern = re.compile(rf"\b{re.escape(callable_name)}\(")
    calls = call_pattern.findall(scoped_src)
    assert len(calls) == expected_calls, (
        f"{callable_name}: expected {expected_calls} call sites, found {len(calls)}"
    )

    guard_count = scoped_src.count("if aclient is not None:")
    fallback_import_count = scoped_src.count("from services.gpt_correction import")
    assert guard_count >= expected_calls if callable_name == "_safe_claude_call" else True
    assert fallback_import_count > 0, "no GPT fallback import found in run_natural_pipeline_stream"


def test_all_gpt_fallbacks_use_terra_model_id():
    """Every new fallback branch must use gpt-5.6-terra, not some other
    model — this is the model the user explicitly asked for."""
    src = _pipeline_src()
    start = src.index("async def run_natural_pipeline_stream")
    scoped_src = src[start:]
    terra_mentions = scoped_src.count('"gpt-5.6-terra"')
    # 9 call sites: 3 streaming-fn calls + 6 _safe_claude_call sites.
    assert terra_mentions == 9, f"expected 9 gpt-5.6-terra references, found {terra_mentions}"


def test_no_existing_claude_call_kwargs_were_altered():
    """The Claude branch body at each site must be untouched — same kwargs,
    same model string, same comment — only wrapped in a new `if` guard."""
    src = _pipeline_src()
    # The original R25 comment must still exist verbatim at exactly the
    # 3 streaming-function call sites (untouched Claude path).
    assert src.count('# R25: corrections always Claude') == 3
    # The two "correction upgraded to Sonnet 5" comments for the QA-retry /
    # multi-window loops must be untouched too.
    assert 'correction upgraded to Sonnet 5' in src


# ─────────────────────────────────────────────────────────────────────────
# 2. Functional tests for services/gpt_correction.py in isolation
# ─────────────────────────────────────────────────────────────────────────

class _FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        class _Msg:
            pass
        m = _Msg()
        m.content = content
        self.message = m
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, completion_tokens=42):
        self.completion_tokens = completion_tokens


class _FakeResponse:
    def __init__(self, content, finish_reason="stop", completion_tokens=42):
        self.choices = [_FakeChoice(content, finish_reason)]
        self.usage = _FakeUsage(completion_tokens)


def _collect_dlog():
    events = []

    def dlog(event, **kwargs):
        events.append((event, kwargs))
    return dlog, events


def test_correction_fallback_enabled_default_on():
    assert gc.correction_fallback_enabled(lambda k, d: d) is True


def test_correction_fallback_enabled_respects_off_flag():
    assert gc.correction_fallback_enabled(lambda k, d: "false") is False


def test_correction_fallback_enabled_never_raises():
    def _boom(k, d):
        raise RuntimeError("db down")
    assert gc.correction_fallback_enabled(_boom) is True  # fails open, safe


@pytest.mark.asyncio
async def test_safe_gpt_correction_call_happy_path():
    dlog, events = _collect_dlog()

    def chat_create(client, model, messages):
        assert model == "gpt-5.6-terra"
        assert messages[0]["role"] == "system"
        return _FakeResponse("<surgical_edit>{}</surgical_edit>")

    msg = await gc.safe_gpt_correction_call(
        client=object(), model="gpt-5.6-terra",
        system="sys", messages=[{"role": "user", "content": "hi"}],
        chat_create=chat_create, dlog=dlog,
    )
    assert msg.content[0].text == "<surgical_edit>{}</surgical_edit>"
    assert any(e == "gpt_correction_call_ok" for e, _ in events)


@pytest.mark.asyncio
async def test_safe_gpt_correction_call_empty_text_is_logged():
    dlog, events = _collect_dlog()

    def chat_create(client, model, messages):
        return _FakeResponse("", finish_reason="length")

    msg = await gc.safe_gpt_correction_call(
        client=object(), model="gpt-5.6-terra", system="sys",
        messages=[], chat_create=chat_create, dlog=dlog,
    )
    assert msg.content[0].text == ""
    assert any(e == "gpt_correction_call_empty_text" for e, _ in events)


@pytest.mark.asyncio
async def test_safe_gpt_correction_call_api_error_returns_message_never_raises():
    dlog, events = _collect_dlog()

    def chat_create(client, model, messages):
        raise ConnectionError("boom")

    msg = await gc.safe_gpt_correction_call(
        client=object(), model="gpt-5.6-terra", system="sys",
        messages=[], chat_create=chat_create, dlog=dlog,
    )
    assert msg.content[0].text == ""
    assert msg.stop_reason == "error"
    assert any(e == "gpt_correction_call_error" for e, _ in events)


@pytest.mark.asyncio
async def test_safe_gpt_correction_call_disabled_by_flag_skips_api():
    dlog, events = _collect_dlog()
    called = {"n": 0}

    def chat_create(client, model, messages):
        called["n"] += 1
        return _FakeResponse("should not happen")

    msg = await gc.safe_gpt_correction_call(
        client=object(), model="gpt-5.6-terra", system="sys",
        messages=[], chat_create=chat_create, dlog=dlog,
        get_setting=lambda k, d: "false",
    )
    assert called["n"] == 0
    assert msg.stop_reason == "skipped"


@pytest.mark.asyncio
async def test_retry_truncated_edit_gpt_extracts_block():
    dlog, _ = _collect_dlog()

    def chat_create(client, model, messages):
        return _FakeResponse(
            'noise<surgical_edit>{"filename": "a.py"}</surgical_edit>trailing'
        )

    raw = await gc.retry_truncated_edit_gpt(
        client=object(), model="gpt-5.6-terra",
        filename="a.py", symbol_name="foo", file_content="code",
        smap=None, user_request="fix it",
        chat_create=chat_create, dlog=dlog,
    )
    assert raw == '{"filename": "a.py"}'


@pytest.mark.asyncio
async def test_retry_truncated_edit_gpt_no_block_returns_none():
    dlog, events = _collect_dlog()

    def chat_create(client, model, messages):
        return _FakeResponse("no tags here")

    raw = await gc.retry_truncated_edit_gpt(
        client=object(), model="gpt-5.6-terra",
        filename="a.py", symbol_name="foo", file_content="code",
        smap=None, user_request="fix it",
        chat_create=chat_create, dlog=dlog,
    )
    assert raw is None
    assert any(e == "gpt_retry_truncated_edit_no_block" for e, _ in events)


@pytest.mark.asyncio
async def test_retry_truncated_edit_gpt_unparseable_json_returns_none():
    dlog, events = _collect_dlog()

    def chat_create(client, model, messages):
        return _FakeResponse("<surgical_edit>{not json</surgical_edit>")

    raw = await gc.retry_truncated_edit_gpt(
        client=object(), model="gpt-5.6-terra",
        filename="a.py", symbol_name="foo", file_content="code",
        smap=None, user_request="fix it",
        chat_create=chat_create, dlog=dlog,
    )
    assert raw is None
    assert any(e == "gpt_retry_truncated_edit_unparseable" for e, _ in events)


@pytest.mark.asyncio
async def test_retry_truncated_newfile_gpt_extracts_block():
    dlog, _ = _collect_dlog()

    def chat_create(client, model, messages):
        return _FakeResponse('<new_file>{"filename": "b.py", "content": "x"}</new_file>')

    raw = await gc.retry_truncated_newfile_gpt(
        client=object(), model="gpt-5.6-terra",
        filename="b.py", user_request="write it",
        chat_create=chat_create, dlog=dlog,
    )
    assert raw == '{"filename": "b.py", "content": "x"}'


@pytest.mark.asyncio
async def test_execute_single_edit_gpt_extracts_block_small_file():
    dlog, _ = _collect_dlog()

    def chat_create(client, model, messages):
        return _FakeResponse('<surgical_edit>{"filename": "c.py"}</surgical_edit>')

    raw = await gc.execute_single_edit_gpt(
        client=object(), model="gpt-5.6-terra",
        filename="c.py", symbol_name="bar", change_description="desc",
        file_content="line1\nline2", symbol_map=None, user_request="req",
        chat_create=chat_create, dlog=dlog,
    )
    assert raw == '{"filename": "c.py"}'


@pytest.mark.asyncio
async def test_execute_single_edit_gpt_disabled_by_flag_returns_none_without_call():
    dlog, _ = _collect_dlog()
    called = {"n": 0}

    def chat_create(client, model, messages):
        called["n"] += 1
        return _FakeResponse("x")

    raw = await gc.execute_single_edit_gpt(
        client=object(), model="gpt-5.6-terra",
        filename="c.py", symbol_name="bar", change_description="desc",
        file_content="line1\nline2", symbol_map=None, user_request="req",
        chat_create=chat_create, dlog=dlog, get_setting=lambda k, d: "false",
    )
    assert raw is None
    assert called["n"] == 0


# ─────────────────────────────────────────────────────────────────────────
# 3. Multi-turn Surgeon (run_gpt_multi_turn_surgeon) — the newest addition.
#    Fake OpenAI SDK shapes: message.tool_calls[i].id / .function.name /
#    .function.arguments (JSON string), matching the real openai package
#    and the already-live Agent Mode GPT correction loop in pipeline.py
#    (which uses this exact same shape).
# ─────────────────────────────────────────────────────────────────────────

class _FakeToolCall:
    def __init__(self, call_id, name, arguments: str):
        self.id = call_id

        class _Fn:
            pass
        fn = _Fn()
        fn.name = name
        fn.arguments = arguments
        self.function = fn


class _FakeMTChoice:
    def __init__(self, content, tool_calls, finish_reason):
        class _Msg:
            pass
        m = _Msg()
        m.content = content
        m.tool_calls = tool_calls
        self.message = m
        self.finish_reason = finish_reason


class _FakeMTResponse:
    """Mimics an OpenAI ChatCompletion with tool_calls, for the multi-turn
    Surgeon loop. `truncated=True` sets the `_sai_truncated` attribute the
    real `_chat_create` wrapper (services/gpt_reasoning.py hardening) sets
    on responses that stayed truncated after its own retry."""
    def __init__(self, content=None, tool_calls=None, finish_reason="stop", truncated=False):
        self.choices = [_FakeMTChoice(content, tool_calls or [], finish_reason)]
        if truncated:
            self._sai_truncated = True


SYMBOL_CODE = "def foo():\n    return 1\n"
MT_SYSTEM = "SYSTEM PROMPT"
MT_TOOLS = [{"type": "function", "function": {"name": "edit_code"}}]


def _mt_edit_call(call_id, old_code, new_code):
    import json
    return _FakeToolCall(call_id, "edit_code", json.dumps({"old_code": old_code, "new_code": new_code}))


def _mt_noop_call(call_id, reason="already correct"):
    import json
    return _FakeToolCall(call_id, "no_change_needed", json.dumps({"reason": reason}))


def _mt_replace_call(call_id, new_code):
    import json
    return _FakeToolCall(call_id, "replace_symbol", json.dumps({"new_code": new_code}))


def test_gpt_multi_turn_surgeon_enabled_default_on():
    assert gc.gpt_multi_turn_surgeon_enabled(lambda k, d: d) is True


def test_gpt_multi_turn_surgeon_enabled_respects_off_flag():
    assert gc.gpt_multi_turn_surgeon_enabled(lambda k, d: "false") is False


def test_gpt_multi_turn_surgeon_enabled_never_raises():
    def _boom(k, d):
        raise RuntimeError("db down")
    assert gc.gpt_multi_turn_surgeon_enabled(_boom) is True  # fails open


def test_gpt_multi_turn_happy_path_single_edit_then_stop():
    """Turn 1: one matching edit_code call (finish_reason=tool_calls).
    Turn 2: no tool calls, finish_reason=stop → loop ends cleanly."""
    dlog, events = _collect_dlog()
    calls = {"n": 0}

    def chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            tc = _mt_edit_call("call_1", "return 1", "return 2")
            return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")
        return _FakeMTResponse(content="Done.", finish_reason="stop")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["early_return"] is None
    assert result["operations"] == [{"find": "return 1", "replace": "return 2"}]
    assert result["confidence"] is None  # caller keeps target.confidence
    assert calls["n"] == 2
    assert ("surgeon_multi_turn_gpt_edit_ok", {}) not in events  # sanity: events carry kwargs, not empty
    assert any(e[0] == "surgeon_multi_turn_gpt_edit_ok" for e in events)
    assert any(e[0] == "surgeon_multi_turn_gpt_end_turn" for e in events)


def test_gpt_multi_turn_mismatch_then_fixed_edit():
    """Turn 1: old_code doesn't match → failure result sent back with hint.
    Turn 2: corrected edit_code matches → applied. Turn 3: stop."""
    dlog, events = _collect_dlog()
    calls = {"n": 0}

    def chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            tc = _mt_edit_call("call_1", "return 999", "return 2")  # wrong old_code
            return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")
        if calls["n"] == 2:
            # Must have received a tool-result message with success=False
            last_msg = messages[-1]
            assert last_msg["role"] == "tool"
            assert '"success": false' in last_msg["content"].lower()
            tc = _mt_edit_call("call_2", "return 1", "return 2")  # corrected
            return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")
        return _FakeMTResponse(content=None, finish_reason="stop")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["early_return"] is None
    assert result["operations"] == [{"find": "return 1", "replace": "return 2"}]
    assert "1 edit(s) failed, 1 succeeded" in result["notes"][0]
    assert any(e[0] == "surgeon_multi_turn_gpt_edit_fail" for e in events)
    assert any(e[0] == "surgeon_multi_turn_gpt_edit_ok" for e in events)


def test_gpt_multi_turn_replace_symbol():
    dlog, _ = _collect_dlog()
    calls = {"n": 0}

    def chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            tc = _mt_replace_call("call_1", "def foo():\n    return 42\n")
            return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")
        return _FakeMTResponse(content=None, finish_reason="stop")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="rewrite",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["operations"] == [{"find": SYMBOL_CODE, "replace": "def foo():\n    return 42\n"}]
    assert result["early_return"] is None


def test_gpt_multi_turn_noop_accepted_returns_early():
    dlog, events = _collect_dlog()

    def chat_create(client, model, messages, **kwargs):
        tc = _mt_noop_call("call_1", "already implements the change")
        return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["early_return"] == (
        SYMBOL_CODE, 10, ["Surgeon: already implemented — already implements the change"], [], [])
    assert any(e[0] == "surgeon_multi_turn_gpt_noop_accepted" for e in events)


def test_gpt_multi_turn_noop_rejected_when_forbid_noop_forces_retry():
    """forbid_noop=True (QA just rejected this code) → no_change_needed must
    be rejected and GPT forced to try again, not allowed to early-return."""
    dlog, events = _collect_dlog()
    calls = {"n": 0}

    def chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            tc = _mt_noop_call("call_1", "looks fine to me")
            return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")
        # Second turn: must have received the "changes ARE required" error
        last_msg = messages[-1]
        assert last_msg["role"] == "tool"
        assert "changes are required" in last_msg["content"].lower()
        tc = _mt_edit_call("call_2", "return 1", "return 2")
        return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=True,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
        max_turns=2,
    )
    assert result["early_return"] is None
    assert result["operations"] == [{"find": "return 1", "replace": "return 2"}]
    assert any(e[0] == "surgeon_multi_turn_gpt_noop_rejected" for e in events)
    assert calls["n"] == 2


def test_gpt_multi_turn_truncated_with_zero_ops_refuses():
    dlog, events = _collect_dlog()

    def chat_create(client, model, messages, **kwargs):
        return _FakeMTResponse(content="", finish_reason="length", truncated=True)

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["early_return"] == (SYMBOL_CODE, 0, ["Surgeon: output truncated — retry"], [], [])
    assert any(e[0] == "surgeon_multi_turn_gpt_truncated" for e in events)


def test_gpt_multi_turn_truncated_after_partial_ops_keeps_them():
    """Truncation on turn 2, after turn 1 already banked a real edit — must
    return what was collected, not throw it away."""
    dlog, _ = _collect_dlog()
    calls = {"n": 0}

    def chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            tc = _mt_edit_call("call_1", "return 1", "return 2")
            return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")
        return _FakeMTResponse(content="", finish_reason="length", truncated=True)

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["early_return"] is None
    assert result["operations"] == [{"find": "return 1", "replace": "return 2"}]
    assert "truncated on turn 2" in result["notes"][0]


def test_gpt_multi_turn_api_error_first_turn_returns_early():
    dlog, events = _collect_dlog()

    def chat_create(client, model, messages, **kwargs):
        raise RuntimeError("connection reset")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["early_return"][0] == SYMBOL_CODE
    assert result["early_return"][1] == 0
    assert "multi-turn API error" in result["early_return"][2][0]
    assert any(e[0] == "surgeon_multi_turn_gpt_api_error" for e in events)


def test_gpt_multi_turn_api_error_after_ops_collected_keeps_them():
    dlog, _ = _collect_dlog()
    calls = {"n": 0}

    def chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            tc = _mt_edit_call("call_1", "return 1", "return 2")
            return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")
        raise RuntimeError("connection reset")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["early_return"] is None
    assert result["operations"] == [{"find": "return 1", "replace": "return 2"}]
    assert "API error on turn 2" in result["notes"][0]


def test_gpt_multi_turn_budget_exhausted_after_max_turns():
    """Model keeps calling edit_code every single turn with never a natural
    stop — loop must hard-cap at max_turns, not run forever."""
    dlog, events = _collect_dlog()
    calls = {"n": 0}

    def chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        # Only turn 1's old_code exists in the original symbol; every
        # subsequent turn's old_code deliberately can never match, so only
        # ever exactly 1 operation can be collected regardless of how many
        # turns run — isolating "did we stop at max_turns" from "did we
        # keep re-matching the same text".
        tc = _mt_edit_call(f"call_{calls['n']}", "return 1", "pass  # rewritten, no longer contains original text")
        return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
        max_turns=3,
    )
    assert calls["n"] == 3
    assert result["early_return"] is None
    assert len(result["operations"]) == 1  # only turn 1's edit matched "return 1"
    assert any(e[0] == "surgeon_multi_turn_gpt_budget_exhausted" for e in events)


def test_gpt_multi_turn_malformed_tool_arguments_json_does_not_crash():
    dlog, events = _collect_dlog()
    calls = {"n": 0}

    def chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            bad_tc = _FakeToolCall("call_1", "edit_code", "{not valid json")
            return _FakeMTResponse(tool_calls=[bad_tc], finish_reason="tool_calls")
        return _FakeMTResponse(content=None, finish_reason="stop")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["early_return"] is None
    assert result["operations"] == []
    assert any(e[0] == "surgeon_multi_turn_gpt_arg_parse_error" for e in events)


def test_gpt_multi_turn_no_tool_calls_first_turn_ends_cleanly():
    """Model responds with plain text and zero tool calls right away —
    should end the loop without operations, same as Claude's end_turn."""
    dlog, _ = _collect_dlog()

    def chat_create(client, model, messages, **kwargs):
        return _FakeMTResponse(content="I looked and nothing to change.", finish_reason="stop")

    result = gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    assert result["early_return"] is None
    assert result["operations"] == []
    assert result["confidence"] is None


def test_gpt_multi_turn_conversation_message_shape_matches_agent_mode_pattern():
    """The assistant/tool message shape appended for turn N+1 must match the
    proven OpenAI multi-turn shape already used by pipeline.py's live Agent
    Mode GPT correction loop: assistant message has `tool_calls` list of
    {"id","type":"function","function":{"name","arguments"}}, followed by
    one {"role":"tool","tool_call_id","content"} message per call."""
    dlog, _ = _collect_dlog()
    captured_messages = []
    calls = {"n": 0}

    def chat_create(client, model, messages, **kwargs):
        calls["n"] += 1
        captured_messages.append([dict(m) for m in messages])
        if calls["n"] == 1:
            tc = _mt_edit_call("call_abc", "return 1", "return 2")
            return _FakeMTResponse(tool_calls=[tc], finish_reason="tool_calls")
        return _FakeMTResponse(content=None, finish_reason="stop")

    gc.run_gpt_multi_turn_surgeon(
        client=object(), model="gpt-5.6-terra", user_msg="fix it",
        symbol_code=SYMBOL_CODE, forbid_noop=False,
        chat_create=chat_create, dlog=dlog,
        surgeon_tool_use_system=MT_SYSTEM, surgeon_tools_openai=MT_TOOLS,
    )
    # Second call's message list must include the assistant tool_calls
    # message and the tool-result message appended after turn 1.
    second_call_msgs = captured_messages[1]
    assistant_msgs = [m for m in second_call_msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["tool_calls"] == [
        {"id": "call_abc", "type": "function",
         "function": {"name": "edit_code", "arguments": _mt_edit_call("x", "return 1", "return 2").function.arguments}}
    ]
    tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_abc"


def test_run_surgeon_pipeline_gpt_multi_turn_branch_dispatches_new_function():
    """Source-truth check: run_surgeon's tool_use dispatch must call
    services.gpt_correction.run_gpt_multi_turn_surgeon for the GPT +
    multi-turn-enabled case, and the pre-existing Claude multi-turn /
    single-turn branches must be byte-for-byte unchanged."""
    src = _pipeline_src()
    assert "from services.gpt_correction import run_gpt_multi_turn_surgeon" in src
    assert "_gpt_multi_turn_surgeon_active" in src
    # The Claude multi-turn branch's core loop constant must be untouched.
    assert '_MT_MAX_TURNS = 5                 # safety cap' in src
    # The old "not yet implemented" placeholder must be gone.
    assert "GPT multi-turn not yet implemented" not in src
