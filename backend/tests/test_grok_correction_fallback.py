"""
Regression tests for the Grok-4.5 correction fallback
(services/grok_correction.py) and its wiring into
services.pipeline.run_natural_pipeline_stream.

Mirrors tests/test_gpt_correction_fallback.py in structure and intent, adapted
for Grok. Background (re-verified by source read for THIS change, not taken on
faith from the older audit docs):

run_natural_pipeline_stream sets `aclient = None` when the architect model is
not Claude and the user has NOT configured an Anthropic key. Every downstream
correction/retry call site is `if aclient is not None: <Claude> else: <GPT
fallback>`. A Grok-only user (Grok key, no Anthropic key, and possibly no
OpenAI key either) would have been routed into the GPT fallback, which needs an
OpenAI key. This adds a third, additive `elif _is_grok_model(arch_model):`
branch at every one of those sites.

Guarantees asserted here:
  1. All 9 `if aclient is not None:` sites in run_natural_pipeline_stream have a
     Grok branch, and the Claude and GPT branches are untouched.
  2. services/gpt_correction.py is not modified or imported by the new module.
  3. services/grok_correction.py behaves correctly in isolation against mocked,
     dependency-injected chat_create/dlog/get_setting.

NO LIVE API CALLS — the xAI endpoint is never contacted.
"""
import asyncio
import pathlib
import re
import subprocess
import sys

import pytest

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

_PIPELINE_PATH = _BACKEND / "services" / "pipeline.py"
_GROK_CORRECTION_PATH = _BACKEND / "services" / "grok_correction.py"
_GPT_CORRECTION_PATH = _BACKEND / "services" / "gpt_correction.py"

from services import grok_correction as grc  # noqa: E402


def _pipeline_src() -> str:
    return _PIPELINE_PATH.read_text()


def _scoped_src() -> str:
    src = _pipeline_src()
    return src[src.index("async def run_natural_pipeline_stream"):]


def _collect_dlog():
    events = []

    def dlog(event, **kw):
        events.append((event, kw))

    return dlog, events


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = _FakeMessage(content)
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, completion_tokens=42):
        self.completion_tokens = completion_tokens


class _FakeResponse:
    """Shape of an xAI/OpenAI ChatCompletion response object."""

    def __init__(self, content, finish_reason="stop", completion_tokens=42):
        self.choices = [_FakeChoice(content, finish_reason)]
        self.usage = _FakeUsage(completion_tokens)


# ─────────────────────────────────────────────────────────────────────────
# 1. Source-truth: additive wiring, existing branches untouched
# ─────────────────────────────────────────────────────────────────────────

def test_grok_correction_module_exists_and_is_a_new_separate_file():
    assert _GROK_CORRECTION_PATH.exists()


def test_grok_correction_does_not_import_pipeline_or_gpt_correction():
    src = _GROK_CORRECTION_PATH.read_text()
    for bad in ("from services.pipeline", "import services.pipeline",
                "from services import pipeline",
                "from services.gpt_correction", "import gpt_correction",
                "from services import gpt_correction"):
        assert bad not in src, f"unexpected import: {bad}"


def test_gpt_correction_file_is_unmodified_by_this_change():
    """Hard requirement: gpt_correction.py must not be touched at all. Compared
    against HEAD via git rather than asserted in prose."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--",
         "backend/services/gpt_correction.py"],
        cwd=str(_BACKEND.parent), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"gpt_correction.py was modified: {out.stdout}"


def test_backend_pipeline_dead_code_file_is_unmodified():
    """backend/pipeline.py is confirmed dead code and must be left alone."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", "backend/pipeline.py"],
        cwd=str(_BACKEND.parent), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_all_nine_aclient_guards_have_a_grok_branch():
    """Every `if aclient is not None:` correction site in
    run_natural_pipeline_stream must now have exactly one matching
    `elif _is_grok_model(arch_model):` branch — no site left Grok-blind."""
    scoped = _scoped_src()
    guards = scoped.count("if aclient is not None:")
    grok_branches = scoped.count("elif _is_grok_model(arch_model):")
    assert guards == 9, f"expected 9 aclient guards, found {guards}"
    assert grok_branches == guards, (
        f"{guards} guards but {grok_branches} Grok branches")


def test_each_grok_branch_sits_between_the_claude_and_gpt_branches():
    """Ordering: Claude (`if aclient is not None:`) -> Grok (`elif`) -> GPT
    (`else:`). Proven per-site by walking the source, so the pre-existing
    Claude and GPT branches keep their exact roles."""
    scoped = _scoped_src()
    for m in re.finditer(r"elif _is_grok_model\(arch_model\):", scoped):
        before = scoped[:m.start()]
        after = scoped[m.end():m.end() + 3000]
        assert before.rindex("if aclient is not None:") > 0
        assert "from services.grok_correction import" in after[:900]
        # The GPT fallback still follows in an `else:` after the Grok branch.
        assert "from services.gpt_correction import" in after


def test_gpt_fallback_call_sites_are_all_still_present_and_unchanged():
    """The GPT fallback must remain at all 9 sites with its model id intact."""
    scoped = _scoped_src()
    assert scoped.count('"gpt-5.6-terra"') == 9
    assert scoped.count("from services.gpt_correction import") == 9


def test_grok_branches_use_only_the_confirmed_model_id():
    scoped = _scoped_src()
    assert scoped.count('"grok-4.5"') == 9
    # No invented Grok ids anywhere in pipeline.py.
    for invented in ("grok-4.6", "grok-5", "grok-4.5-turbo", "grok-max"):
        assert invented not in _pipeline_src()


def test_claude_correction_branches_untouched():
    """The Claude QA/correction path must be byte-for-byte intact: its marker
    comment and its hardcoded model id are unchanged."""
    src = _pipeline_src()
    assert "# R25: corrections always Claude" in src
    assert 'model="claude-sonnet-5"' in src
    scoped = _scoped_src()
    assert scoped.count("_safe_claude_call(") == 6
    assert scoped.count("_retry_truncated_edit(") == 1
    assert scoped.count("_retry_truncated_newfile(") == 1
    assert scoped.count("_execute_single_edit(") == 1


def test_grok_correction_functions_referenced_by_pipeline_all_exist():
    """Every symbol pipeline.py imports from the new module must exist, so no
    site can blow up with ImportError at runtime."""
    scoped = _scoped_src()
    names = set()
    for m in re.finditer(r"from services\.grok_correction import ([a-z_0-9]+)", scoped):
        names.add(m.group(1))
    assert names == {
        "safe_grok_correction_call", "retry_truncated_edit_grok",
        "retry_truncated_newfile_grok", "execute_single_edit_grok",
    }, names
    for n in names:
        assert callable(getattr(grc, n)), f"{n} missing from grok_correction"


def test_every_function_in_grok_correction_logs_via_dlog():
    """Rule: _dlog on every new backend code path."""
    src = _GROK_CORRECTION_PATH.read_text()
    lines = src.splitlines()
    starts = [i for i, ln in enumerate(lines)
              if ln.startswith("def ") or ln.startswith("async def ")]
    unlogged = []
    for idx, s in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        name = lines[s].split("(")[0].replace("async def ", "").replace("def ", "")
        if name == "_dlog":
            continue
        if "_dlog(" not in "\n".join(lines[s:end]):
            unlogged.append(name)
    assert unlogged == [], f"functions with no _dlog call: {unlogged}"


# ─────────────────────────────────────────────────────────────────────────
# 2. Kill switch
# ─────────────────────────────────────────────────────────────────────────

def test_flag_defaults_on():
    dlog, events = _collect_dlog()
    assert grc.grok_correction_fallback_enabled(lambda k, d: d, dlog=dlog) is True
    assert any(e == "grok_correction_flag_checked" for e, _ in events)


def test_flag_respects_explicit_false():
    assert grc.grok_correction_fallback_enabled(lambda k, d: "false") is False
    assert grc.grok_correction_fallback_enabled(lambda k, d: "FALSE") is False


def test_flag_fails_open_on_lookup_error():
    dlog, events = _collect_dlog()

    def boom(k, d):
        raise RuntimeError("db down")

    assert grc.grok_correction_fallback_enabled(boom, dlog=dlog) is True
    assert any(e == "grok_correction_flag_error" for e, _ in events)


def test_flag_key_name_is_grok_specific_not_shared_with_gpt():
    """A separate setting so disabling GPT's fallback does not disable Grok's."""
    keys = []
    grc.grok_correction_fallback_enabled(lambda k, d: keys.append(k) or d)
    assert keys == ["grok_correction_fallback"]


# ─────────────────────────────────────────────────────────────────────────
# 3. safe_grok_correction_call
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_safe_grok_correction_call_happy_path_returns_message_shape():
    dlog, events = _collect_dlog()
    seen = {}

    def chat_create(client, model=None, messages=None, **kw):
        seen["model"] = model
        seen["messages"] = messages
        return _FakeResponse("corrected text")

    msg = await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="sys",
        messages=[{"role": "user", "content": "fix"}],
        chat_create=chat_create, dlog=dlog)

    # Anthropic-Message-shaped so existing call sites need no change.
    assert msg.content[0].type == "text"
    assert msg.content[0].text == "corrected text"
    assert msg.usage.output_tokens == 42
    assert seen["model"] == "grok-4.5"
    # system is folded into the message array (xAI/OpenAI shape).
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert any(e == "grok_correction_call_ok" for e, _ in events)


@pytest.mark.asyncio
async def test_safe_grok_correction_call_accepts_claude_only_kwargs():
    """Call sites pass the same kwargs to Claude/GPT/Grok — Claude-only ones
    must be absorbed rather than raising TypeError."""
    def chat_create(client, model=None, messages=None, **kw):
        return _FakeResponse("ok")

    msg = await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s", messages=[],
        chat_create=chat_create,
        desired_text_tokens=12000, thinking_budget=8000, retry_on_starve=True)
    assert msg.content[0].text == "ok"


@pytest.mark.asyncio
async def test_safe_grok_correction_call_empty_text_logged():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        return _FakeResponse("", finish_reason="length")

    msg = await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s", messages=[],
        chat_create=chat_create, dlog=dlog)
    assert msg.content[0].text == ""
    assert any(e == "grok_correction_call_empty_text" for e, _ in events)


@pytest.mark.asyncio
async def test_safe_grok_correction_call_api_error_never_raises():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        raise ConnectionError("boom")

    msg = await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s", messages=[],
        chat_create=chat_create, dlog=dlog)
    assert msg.content[0].text == "" and msg.stop_reason == "error"
    assert any(e == "grok_correction_call_exception" for e, _ in events)


@pytest.mark.asyncio
async def test_safe_grok_correction_call_billing_429_classified_not_retryable():
    """xAI 429s are ambiguous; a spend-cap 429 must be logged as
    non-retryable with the console.x.ai remediation message."""
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        raise RuntimeError("Error code: 429 - Your team has either used all "
                           "available credits or reached its monthly spending limit.")

    await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s", messages=[],
        chat_create=chat_create, dlog=dlog)
    hits = [kw for e, kw in events if e == "grok_correction_call_429"]
    assert hits and hits[0]["kind"] == "billing"
    assert hits[0]["retryable"] is False
    assert "console.x.ai" in hits[0]["user_message"]


@pytest.mark.asyncio
async def test_safe_grok_correction_call_plain_429_classified_retryable():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        raise RuntimeError("Error code: 429 - rate limit exceeded")

    await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s", messages=[],
        chat_create=chat_create, dlog=dlog)
    hits = [kw for e, kw in events if e == "grok_correction_call_429"]
    assert hits and hits[0]["kind"] == "rate_limit" and hits[0]["retryable"] is True


@pytest.mark.asyncio
async def test_safe_grok_correction_call_timeout_returns_empty_message():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        import time
        time.sleep(0.5)
        return _FakeResponse("too late")

    msg = await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s", messages=[],
        chat_create=chat_create, dlog=dlog, timeout_s=0.05)
    assert msg.content[0].text == "" and msg.stop_reason == "timeout"
    assert any(e == "grok_correction_call_timeout" for e, _ in events)


@pytest.mark.asyncio
async def test_safe_grok_correction_call_disabled_by_flag_skips_api():
    called = {"n": 0}

    def chat_create(client, model=None, messages=None, **kw):
        called["n"] += 1
        return _FakeResponse("should not happen")

    msg = await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s", messages=[],
        chat_create=chat_create, get_setting=lambda k, d: "false")
    assert called["n"] == 0 and msg.stop_reason == "skipped"


@pytest.mark.asyncio
async def test_safe_grok_correction_call_never_sends_disallowed_params():
    """Grok 4.5 rejects presence_penalty/frequency_penalty/stop outright."""
    seen = {}

    def chat_create(client, model=None, messages=None, **kw):
        seen.update(kw)
        return _FakeResponse("ok")

    await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s", messages=[],
        chat_create=chat_create)
    for banned in ("presence_penalty", "frequency_penalty", "stop"):
        assert banned not in seen


@pytest.mark.asyncio
async def test_safe_grok_correction_call_forces_content_on_tool_call_messages():
    """xAI 400s when an assistant message has tool_calls and no content."""
    seen = {}

    def chat_create(client, model=None, messages=None, **kw):
        seen["messages"] = messages
        return _FakeResponse("ok")

    await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s",
        messages=[{"role": "assistant", "content": None,
                   "tool_calls": [{"id": "c"}]}],
        chat_create=chat_create)
    for m in seen["messages"]:
        assert m.get("content") is not None


@pytest.mark.asyncio
async def test_safe_grok_correction_call_tolerates_garbage_response_shape():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        return object()  # not a ChatCompletion at all

    msg = await grc.safe_grok_correction_call(
        object(), model="grok-4.5", system="s", messages=[],
        chat_create=chat_create, dlog=dlog)
    assert msg.content[0].text == ""
    assert any(e == "grok_correction_text_extract_failed" for e, _ in events)


# ─────────────────────────────────────────────────────────────────────────
# 4. Focused retry / single-edit helpers
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_truncated_edit_grok_extracts_block():
    def chat_create(client, model=None, messages=None, **kw):
        return _FakeResponse(
            'noise<surgical_edit>{"filename": "a.py"}</surgical_edit>trailing')

    raw = await grc.retry_truncated_edit_grok(
        object(), "grok-4.5", "a.py", "foo", "code", None, "fix it", chat_create)
    assert raw == '{"filename": "a.py"}'


@pytest.mark.asyncio
async def test_retry_truncated_edit_grok_no_block_returns_none():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        return _FakeResponse("no tags here")

    raw = await grc.retry_truncated_edit_grok(
        object(), "grok-4.5", "a.py", "foo", "code", None, "fix it",
        chat_create, dlog)
    assert raw is None
    assert any(e == "grok_retry_truncated_edit_no_block" for e, _ in events)


@pytest.mark.asyncio
async def test_retry_truncated_edit_grok_unparseable_json_returns_none():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        return _FakeResponse("<surgical_edit>{not json</surgical_edit>")

    raw = await grc.retry_truncated_edit_grok(
        object(), "grok-4.5", "a.py", "foo", "code", None, "fix it",
        chat_create, dlog)
    assert raw is None
    assert any(e == "grok_retry_truncated_edit_unparseable" for e, _ in events)


@pytest.mark.asyncio
async def test_retry_truncated_edit_grok_api_error_returns_none():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        raise ConnectionError("down")

    raw = await grc.retry_truncated_edit_grok(
        object(), "grok-4.5", "a.py", "foo", "code", None, "fix it",
        chat_create, dlog)
    assert raw is None
    assert any(e == "grok_retry_truncated_edit_error" for e, _ in events)


@pytest.mark.asyncio
async def test_retry_truncated_edit_grok_disabled_by_flag():
    called = {"n": 0}

    def chat_create(client, model=None, messages=None, **kw):
        called["n"] += 1
        return _FakeResponse("x")

    raw = await grc.retry_truncated_edit_grok(
        object(), "grok-4.5", "a.py", "foo", "code", None, "fix it",
        chat_create, None, lambda k, d: "false")
    assert raw is None and called["n"] == 0


@pytest.mark.asyncio
async def test_retry_truncated_newfile_grok_extracts_block():
    def chat_create(client, model=None, messages=None, **kw):
        return _FakeResponse('<new_file>{"filename": "b.py", "content": "x"}</new_file>')

    raw = await grc.retry_truncated_newfile_grok(
        object(), "grok-4.5", "b.py", "write it", chat_create)
    assert raw == '{"filename": "b.py", "content": "x"}'


@pytest.mark.asyncio
async def test_retry_truncated_newfile_grok_no_block_returns_none():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        return _FakeResponse("sorry, cannot")

    raw = await grc.retry_truncated_newfile_grok(
        object(), "grok-4.5", "b.py", "write it", chat_create, dlog)
    assert raw is None
    assert any(e == "grok_retry_truncated_newfile_no_block" for e, _ in events)


@pytest.mark.asyncio
async def test_execute_single_edit_grok_extracts_block_small_file():
    def chat_create(client, model=None, messages=None, **kw):
        return _FakeResponse('<surgical_edit>{"filename": "c.py"}</surgical_edit>')

    raw = await grc.execute_single_edit_grok(
        object(), "grok-4.5", "c.py", "bar", "desc", "line1\nline2", None,
        "req", chat_create)
    assert raw == '{"filename": "c.py"}'


@pytest.mark.asyncio
async def test_execute_single_edit_grok_windows_large_files():
    dlog, events = _collect_dlog()

    class _Sym:
        name = "bar"
        full_path = "bar"
        start_line = 500
        end_line = 520

        class symbol_type:
            value = "function"

    class _Map:
        symbols = [_Sym()]

    def chat_create(client, model=None, messages=None, **kw):
        return _FakeResponse('<surgical_edit>{"filename": "big.py"}</surgical_edit>')

    big = "\n".join(f"line{i}" for i in range(2000))
    raw = await grc.execute_single_edit_grok(
        object(), "grok-4.5", "big.py", "bar", "desc", big, _Map(), "req",
        chat_create, dlog)
    assert raw == '{"filename": "big.py"}'
    assert any(e == "grok_execute_single_edit_windowed" for e, _ in events)


@pytest.mark.asyncio
async def test_execute_single_edit_grok_disabled_by_flag_returns_none():
    called = {"n": 0}

    def chat_create(client, model=None, messages=None, **kw):
        called["n"] += 1
        return _FakeResponse("x")

    raw = await grc.execute_single_edit_grok(
        object(), "grok-4.5", "c.py", "bar", "desc", "x", None, "req",
        chat_create, None, lambda k, d: "false")
    assert raw is None and called["n"] == 0


@pytest.mark.asyncio
async def test_execute_single_edit_grok_api_error_returns_none():
    dlog, events = _collect_dlog()

    def chat_create(client, model=None, messages=None, **kw):
        raise RuntimeError("xai down")

    raw = await grc.execute_single_edit_grok(
        object(), "grok-4.5", "c.py", "bar", "desc", "x", None, "req",
        chat_create, dlog)
    assert raw is None
    assert any(e == "grok_execute_single_edit_error" for e, _ in events)


# ─────────────────────────────────────────────────────────────────────────
# 5. No collision with existing tests that patch pipeline.OpenAI
# ─────────────────────────────────────────────────────────────────────────

def test_grok_client_factory_does_not_depend_on_pipeline_openai_symbol():
    """The audit flagged a risk that Grok tests patching `pipeline.OpenAI`
    could break GPT-path tests. The Grok client is built inside
    grok_provider.get_grok_client (which imports OpenAI locally), so no test
    here patches pipeline.OpenAI at all."""
    needle = "setattr(pipeline, " + '"OpenAI"'
    tests_dir = _BACKEND / "tests"
    for path in (tests_dir / "test_grok_provider.py",
                 tests_dir / "test_grok_tool_call_handling.py",
                 tests_dir / "test_grok_correction_fallback.py"):
        # `needle` is assembled by concatenation above so that it does not
        # appear literally in this file's own source and self-trip the check.
        assert needle not in path.read_text(), (
            f"{path.name} patches pipeline.OpenAI — would collide with the "
            f"existing GPT-path tests")
