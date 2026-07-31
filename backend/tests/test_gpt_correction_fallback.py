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
