"""
Tests for the xAI Grok provider adapter (services/grok_provider.py) and its
additive wiring into services/pipeline.py's `_get_client_for_model` factory.

Scope:
  * `_is_grok_model` — model-id matcher (mirrors _is_claude_model/_is_gemini_model)
  * `_get_grok_key`  — per-user key resolution via the existing `user_api_keys`
                       table under the new key_type="grok" (no schema change)
  * `get_grok_client`— OpenAI-SDK client pointed at https://api.x.ai/v1
  * pipeline.py source-truth checks: the Grok branch is additive and the
    existing Claude / Gemini / OpenAI branches are unchanged.

NO LIVE API CALLS. The xAI endpoint is never contacted — everything here is
mocked. Live-endpoint verification is an explicit, documented open gap pending
a real xAI API key.
"""
import pathlib
import sys

import pytest

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

_PIPELINE_PATH = _BACKEND / "services" / "pipeline.py"
_GROK_PROVIDER_PATH = _BACKEND / "services" / "grok_provider.py"

from services import grok_provider as gp  # noqa: E402


def _pipeline_src() -> str:
    return _PIPELINE_PATH.read_text()


# ─────────────────────────────────────────────────────────────────────────
# 1. _is_grok_model
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model", [
    "grok-4.5",
    "grok-4.5-latest",
    "grok-4.6",
    "grok-4",   # only "grok-" prefixed ids match
    "grok-code-fast-1",
])
def test_is_grok_model_true_for_grok_ids(model):
    assert gp._is_grok_model(model) is True


@pytest.mark.parametrize("model", [
    "", None, "claude-sonnet-5", "gpt-5.6-terra", "gemini-2.5-pro",
    "models/gemini-2.5-pro", "ollama:llama3", "o3-mini",
    "grok",          # bare name without the dash is not a model id
    "xai/grok-4.5",  # gateway-prefixed ids are not what pipeline passes
])
def test_is_grok_model_false_for_everything_else(model):
    assert gp._is_grok_model(model) is False


def test_is_grok_model_does_not_overlap_pipeline_matchers():
    """A Grok id must not be claimed by the Claude or Gemini matchers, and vice
    versa — otherwise the new elif branch could shadow an existing provider."""
    src = _pipeline_src()
    assert "def _is_claude_model" in src and "def _is_gemini_model" in src
    # Reproduce the two existing matchers verbatim from source semantics.
    assert not "grok-4.5".startswith("claude-")
    assert not "grok-4.5".startswith("gemini-")
    assert not "grok-4.5".startswith("models/gemini")
    assert gp._is_grok_model("claude-sonnet-5") is False
    assert gp._is_grok_model("gemini-2.5-pro") is False


# ─────────────────────────────────────────────────────────────────────────
# 2. _get_grok_key — key_type="grok" in the existing table, Fernet-decrypted
# ─────────────────────────────────────────────────────────────────────────

def test_get_grok_key_reads_grok_key_type_and_decrypts(monkeypatch):
    seen = {}

    def fake_get_user_api_key(user_id, key_type):
        seen["user_id"] = user_id
        seen["key_type"] = key_type
        return "ENCRYPTED"

    monkeypatch.setattr(gp, "get_user_api_key", fake_get_user_api_key)
    monkeypatch.setattr(gp, "decrypt_api_key", lambda v: "xai-plaintext" if v == "ENCRYPTED" else "")

    assert gp._get_grok_key("user-1") == "xai-plaintext"
    assert seen == {"user_id": "user-1", "key_type": "grok"}


def test_get_grok_key_raises_when_no_key_configured(monkeypatch):
    monkeypatch.setattr(gp, "get_user_api_key", lambda u, k: None)
    with pytest.raises(ValueError) as ei:
        gp._get_grok_key("user-1")
    assert "Grok API key not configured" in str(ei.value)


def test_get_grok_key_raises_when_no_user_id(monkeypatch):
    """Per-user only — no global/env fallback (same rule as every other
    provider in this codebase)."""
    monkeypatch.setattr(gp, "get_user_api_key", lambda u, k: "ENCRYPTED")
    with pytest.raises(ValueError):
        gp._get_grok_key("")


def test_get_grok_key_raises_on_decrypt_failure_and_logs(monkeypatch):
    events = []
    monkeypatch.setattr(gp, "get_user_api_key", lambda u, k: "CORRUPT")

    def boom(_):
        raise ValueError("bad token")

    monkeypatch.setattr(gp, "decrypt_api_key", boom)
    with pytest.raises(ValueError):
        gp._get_grok_key("user-1", dlog=lambda e, **kw: events.append(e))
    assert "api_key_decrypt_failed" in events
    assert "grok_key_missing" in events


# ─────────────────────────────────────────────────────────────────────────
# 3. get_grok_client — base_url must be xAI's, key must be the user's
# ─────────────────────────────────────────────────────────────────────────

def test_grok_base_url_constant_is_xai():
    assert gp.GROK_BASE_URL == "https://api.x.ai/v1"


def test_grok_default_model_is_the_confirmed_shipping_id():
    """Default stays 4.5; 4.6 is also a confirmed selectable id."""
    assert gp.GROK_DEFAULT_MODEL == "grok-4.5"
    assert "grok-4.5" in gp.GROK_CONFIRMED_MODELS
    assert "grok-4.6" in gp.GROK_CONFIRMED_MODELS
    # xAI rejects the hyphenated form (docs / migration notes).
    assert "grok-4-6" not in gp.GROK_CONFIRMED_MODELS


def test_get_grok_client_uses_xai_base_url(monkeypatch):
    monkeypatch.setattr(gp, "get_user_api_key", lambda u, k: "ENCRYPTED")
    monkeypatch.setattr(gp, "decrypt_api_key", lambda v: "xai-test-key")

    captured = {}

    class FakeOpenAI:
        def __init__(self, api_key=None, base_url=None, default_headers=None):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["default_headers"] = default_headers

    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "OpenAI", FakeOpenAI)

    client = gp.get_grok_client("user-1")
    assert isinstance(client, FakeOpenAI)
    assert captured["api_key"] == "xai-test-key"
    assert captured["base_url"] == "https://api.x.ai/v1"
    # Additive: the client now always carries the x-grok-conv-id cache header
    # (see test_grok_cache_headers.py for the dedicated coverage of this).
    assert captured["default_headers"] == {"x-grok-conv-id": "surgicalai-user-1"}


def test_get_grok_client_logs_creation(monkeypatch):
    monkeypatch.setattr(gp, "get_user_api_key", lambda u, k: "ENCRYPTED")
    monkeypatch.setattr(gp, "decrypt_api_key", lambda v: "xai-test-key")

    class FakeOpenAI:
        def __init__(self, api_key=None, base_url=None, default_headers=None):
            pass

    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "OpenAI", FakeOpenAI)

    events = []
    gp.get_grok_client("user-1", dlog=lambda e, **kw: events.append(e))
    assert "grok_client_created" in events
    assert "grok_client_created_with_cache_headers" in events


def test_get_grok_client_propagates_missing_key_error(monkeypatch):
    monkeypatch.setattr(gp, "get_user_api_key", lambda u, k: None)
    with pytest.raises(ValueError):
        gp.get_grok_client("user-1")


# ─────────────────────────────────────────────────────────────────────────
# 4. pipeline.py wiring — additive only
# ─────────────────────────────────────────────────────────────────────────

def test_pipeline_imports_grok_matcher_from_new_module_not_duplicated():
    """pipeline.py must import _is_grok_model from services.grok_provider
    rather than redefining it, keeping pipeline.py edits minimal."""
    src = _pipeline_src()
    assert "from services.grok_provider import _is_grok_model, get_grok_client" in src
    assert "def _is_grok_model" not in src


def test_get_client_for_model_has_additive_grok_branch():
    src = _pipeline_src()
    start = src.index("def _get_client_for_model")
    body = src[start:start + 1800]
    assert "if _is_grok_model(model):" in body
    # Additive session_id passthrough (grok-cache-header fix): the call now
    # forwards session_id so the returned client carries a stable
    # per-conversation prompt-cache header.
    assert "get_grok_client(user_id, dlog=_dlog, session_id=session_id)" in body
    # The pre-existing Gemini branch and the OpenAI fall-through are unchanged.
    assert "if _is_gemini_model(model):" in body
    assert 'base_url="https://generativelanguage.googleapis.com/v1beta/openai/"' in body
    assert "return _get_client(user_id)" in body


def test_get_client_for_model_grok_branch_precedes_gemini_branch():
    """Ordering sanity: the Grok check must not be placed after the generic
    OpenAI fall-through (which would make it dead code)."""
    src = _pipeline_src()
    start = src.index("def _get_client_for_model")
    body = src[start:start + 1800]
    assert body.index("if _is_grok_model(model):") < body.index("return _get_client(user_id)")


def test_grok_provider_does_not_import_pipeline():
    """Zero circular-import risk (same rule gpt_correction.py/gpt_reasoning.py
    follow) — pipeline.py imports grok_provider, so the reverse must not exist."""
    src = _GROK_PROVIDER_PATH.read_text()
    assert "import services.pipeline" not in src
    assert "from services.pipeline" not in src
    assert "from services import pipeline" not in src


def test_grok_provider_does_not_import_gpt_correction():
    """gpt_correction.py must stay entirely untouched and unimported (its name
    may only appear in prose/docstrings, never in an import statement)."""
    src = _GROK_PROVIDER_PATH.read_text()
    assert "import gpt_correction" not in src
    assert "from services.gpt_correction" not in src
    assert "from services import gpt_correction" not in src


def test_every_public_provider_function_logs_via_dlog():
    """Rule: _dlog must appear in every new backend code path. Assert every
    top-level function in the module contains at least one _dlog( call."""
    src = _GROK_PROVIDER_PATH.read_text()
    lines = src.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("def ")]
    unlogged = []
    for idx, s in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        name = lines[s].split("(")[0][4:]
        # _dlog itself is the logger. _is_grok_model is a single-expression pure
        # string predicate copied verbatim in shape from pipeline.py's
        # _is_claude_model/_is_gemini_model (which also do not log) — logging on
        # every model-id comparison would flood the debug stream, and its
        # callers log the outcome instead.
        if name in ("_dlog", "_is_grok_model"):
            continue
        body = "\n".join(lines[s:end])
        if "_dlog(" not in body:
            unlogged.append(name)
    assert unlogged == [], f"functions with no _dlog call: {unlogged}"


def test_pipeline_openai_and_gemini_key_helpers_untouched():
    """The existing key helpers must be byte-for-byte intact — Grok's key
    helper lives in the new module instead."""
    src = _pipeline_src()
    assert "def _get_gemini_key" in src
    assert "def _get_client(user_id" in src or "def _get_client(" in src
    assert "def _get_grok_key" not in src


# ─────────────────────────────────────────────────────────────────────────
# `run_natural_pipeline_stream`'s "GPT turn" branch — proven live against
# api.x.ai on 2026-08-01 (not the docs alone) to have two real bugs before
# this fix: (1) it built a raw OpenAI client for ANY non-Claude architect
# model, including Grok, misrouting every Grok chat turn to api.openai.com;
# (2) it passed `stop=<real sequence list>` unconditionally, which xAI
# hard-400s with "Model grok-4.5 does not support parameter stop." (a bare
# `stop: null` is tolerated — only a populated list 400s — confirmed live).
# `_chat_create`'s own `stop`-stripping only fires for
# `base_model in NO_TEMPERATURE_MODELS` (OpenAI reasoning ids), which never
# contains a `grok-*` id, so it does not cover this call site.
# ─────────────────────────────────────────────────────────────────────────

def _natural_pipeline_gpt_turn_src() -> str:
    src = _pipeline_src()
    start = src.index("GPT/Grok turn (parity with the Claude branch")
    # Slice to the first `for _attempt in range(3):` after the marker, which
    # is the top of the retry loop this branch feeds into.
    end = src.index("for _attempt in range(3):", start)
    return src[start:end]


def test_natural_pipeline_gpt_turn_routes_grok_to_xai_client():
    """The main-chat (non-Agent-Mode) turn loop must route a Grok architect
    model to `get_grok_client`, not the raw OpenAI client, before ever
    building the request kwargs."""
    block = _natural_pipeline_gpt_turn_src()
    assert "if _is_grok_model(arch_model):" in block
    # Additive session_id passthrough (grok-cache-header fix).
    assert "_gpt_client = get_grok_client(user_id, dlog=_dlog, session_id=session_id)" in block
    assert "_gpt_client = _get_client(user_id)" in block  # GPT path retained


def test_natural_pipeline_gpt_turn_strips_stop_for_grok_only():
    """`strip_unsupported_params` must be applied to the outgoing kwargs only
    when the architect model is Grok — GPT's `stop` handling must be
    untouched (still flows straight into `_chat_create`, which does its own
    o-series-specific stripping for OpenAI models)."""
    block = _natural_pipeline_gpt_turn_src()
    assert "strip_unsupported_params(" in block
    # The strip call itself must be guarded by the same _is_grok_model check,
    # not applied unconditionally (that would be harmless for GPT today, but
    # would silently start being a functional change if GPT ever needed a
    # disallowed param this set doesn't know about).
    guard_idx = block.index("if _is_grok_model(arch_model):", block.index("_gpt_call_kwargs ="))
    strip_idx = block.index("strip_unsupported_params(")
    assert guard_idx < strip_idx


def test_natural_pipeline_gpt_turn_gpt_call_kwargs_unchanged_shape():
    """For a non-Grok model, the effective kwargs passed to `_chat_create`
    must still be exactly `messages`, `stream=True`, `max_tokens`, `stop` —
    same four keys as before this fix, just built via a dict instead of
    inline call-site literals (functionally identical for GPT)."""
    block = _natural_pipeline_gpt_turn_src()
    assert '"messages": _gpt_msgs' in block
    assert '"stream": True' in block
    assert '"max_tokens": _max_output_tokens(arch_model)' in block
    assert '"stop": _gpt_stop' in block


def test_natural_pipeline_gpt_turn_dlogs_grok_client_route():
    block = _natural_pipeline_gpt_turn_src()
    assert '_dlog("natural_pipeline_grok_client"' in block


def test_natural_pipeline_claude_branch_above_is_unmodified():
    """The `if _natural_use_claude:` branch that this `else:` pairs with must
    not be touched by this fix — only the `else:` (non-Claude) side changed."""
    src = _pipeline_src()
    assert "_natural_use_claude = _is_claude_model(arch_model)" in src
    # The Claude streaming call must still be the Anthropic client, untouched.
    idx = src.index("_natural_use_claude = _is_claude_model(arch_model)")
    claude_block = src[idx:idx + 4000]
    assert "aclient" in claude_block or "anthropic" in claude_block.lower()


# ─────────────────────────────────────────────────────────────────────────
# 8. Round-3 client-misrouting fixes across pipeline.py (source-truth).
#
# Same class of bug as the Agent Mode and main-chat GPT-turn fixes above: a
# `client = _get_client(user_id)` (raw OpenAI client) at a call site that then
# sends a USER-SELECTABLE model id (architect_model / surgeon_model / a
# non-hardcoded model param) to that client. For a `grok-*` selection this
# misroutes the request to api.openai.com with the wrong key/model instead of
# api.x.ai. Each fix guards the client construction on `_is_grok_model(<var>)`,
# routes Grok to `get_grok_client(user_id, dlog=_dlog)`, keeps the GPT path
# (`_get_client(user_id)`) byte-identical, and emits a `_dlog(...)` on the new
# Grok branch. None of these call sites pass `stop` / `presence_penalty` /
# `frequency_penalty`, so `strip_unsupported_params` is (correctly) NOT applied
# here — only the two params-carrying sites fixed earlier need it.
# ─────────────────────────────────────────────────────────────────────────

def _grok_route_block(dlog_event: str) -> str:
    """Slice a window of pipeline.py source starting at the unique `_dlog`
    event name emitted on one of the round-3 Grok-routing branches."""
    src = _pipeline_src()
    marker = f'_dlog("{dlog_event}"'
    assert marker in src, f"missing dlog marker for {dlog_event}"
    start = src.index(marker)
    # Look a little before the marker to capture the guard line, and after it
    # to capture the get_grok_client call and the GPT else-branch.
    return src[start - 200:start + 400]


# call sites where a `session_id` local was already in scope (grok-cache-header
# fix): these now forward it explicitly to get_grok_client. The three call
# sites whose enclosing function has no `session_id` param
# (run_chat_grok_client / file_creator_grok_client / gpt_direct_rewrite_grok_client)
# are deliberately left on the plain two-arg call.
_SITES_WITH_SESSION_ID = {
    "run_chat_stream_grok_client",
    "multi_file_architect_grok_client",
    "smart_pipeline_chat_grok_client",
    "smart_architect_grok_client",
    "lint_fix_grok_client",
}


@pytest.mark.parametrize("dlog_event,model_var", [
    ("run_chat_grok_client", "chat_model"),
    ("run_chat_stream_grok_client", "chat_model"),
    ("multi_file_architect_grok_client", "arch_model"),
    ("file_creator_grok_client", "creator_model"),
    ("gpt_direct_rewrite_grok_client", "model"),
    ("smart_pipeline_chat_grok_client", "chat_model"),
    ("smart_architect_grok_client", "arch_model"),
    ("lint_fix_grok_client", "_lint_surg_model"),
])
def test_round3_site_routes_grok_to_xai_client(dlog_event, model_var):
    """Every round-3 fixed call site must guard on `_is_grok_model(<var>)`,
    build the client via `get_grok_client(user_id, dlog=_dlog[, session_id=session_id])`
    for Grok, and still fall back to the raw `_get_client(user_id)` for
    non-Grok models."""
    block = _grok_route_block(dlog_event)
    assert f"if _is_grok_model({model_var}):" in block
    expected_call = (
        "get_grok_client(user_id, dlog=_dlog, session_id=session_id)"
        if dlog_event in _SITES_WITH_SESSION_ID
        else "get_grok_client(user_id, dlog=_dlog)"
    )
    assert expected_call in block
    assert "_get_client(user_id)" in block  # GPT path retained


@pytest.mark.parametrize("dlog_event", [
    "run_chat_grok_client",
    "run_chat_stream_grok_client",
    "multi_file_architect_grok_client",
    "file_creator_grok_client",
    "gpt_direct_rewrite_grok_client",
    "smart_pipeline_chat_grok_client",
    "smart_architect_grok_client",
    "lint_fix_grok_client",
])
def test_round3_site_dlogs_grok_client_route(dlog_event):
    """Hard project rule: every new Grok branch logs via `_dlog(...)`."""
    src = _pipeline_src()
    assert f'_dlog("{dlog_event}"' in src


@pytest.mark.parametrize("dlog_event", [
    "run_chat_grok_client",
    "run_chat_stream_grok_client",
    "multi_file_architect_grok_client",
    "file_creator_grok_client",
    "gpt_direct_rewrite_grok_client",
    "smart_pipeline_chat_grok_client",
    "smart_architect_grok_client",
    "lint_fix_grok_client",
])
def test_round3_site_grok_guard_precedes_get_grok_client(dlog_event):
    """The `_is_grok_model(...)` guard must come before the `get_grok_client`
    call in each block — i.e. Grok routing is conditional, never
    unconditional (which would break the GPT path)."""
    block = _grok_route_block(dlog_event)
    guard_idx = block.index("if _is_grok_model(")
    expected_call = (
        "get_grok_client(user_id, dlog=_dlog, session_id=session_id)"
        if dlog_event in _SITES_WITH_SESSION_ID
        else "get_grok_client(user_id, dlog=_dlog)"
    )
    grok_client_idx = block.index(expected_call)
    assert guard_idx < grok_client_idx


def test_round3_fixes_do_not_add_strip_unsupported_params():
    """None of the round-3 call sites pass stop/presence_penalty/
    frequency_penalty, so no NEW `strip_unsupported_params` call should be
    introduced by them — the helper must be applied exactly once, at the
    single pre-existing main-chat GPT-turn site."""
    src = _pipeline_src()
    # `strip_unsupported_params(` is INVOKED exactly once (the earlier
    # `run_natural_pipeline_stream` fix); the bare name also appears once on the
    # import line. The round-3 fixes must not add another invocation.
    assert src.count("strip_unsupported_params(") == 1  # single call site
    assert src.count("strip_unsupported_params") == 2   # + import line


def test_round3_intentional_hardcoded_gpt_fallbacks_are_untouched():
    """Sites that send a HARDCODED GPT model id (e.g. `"gpt-4.1"`,
    `"gpt-5.6-terra"`) are intentional GPT-only fallbacks and must keep the
    plain `_get_client(user_id)` with no Grok guard — a Grok selection never
    reaches them."""
    src = _pipeline_src()
    # QA-for-new-file / QA-agent / file-creator-no-key GPT fallbacks: hardcoded.
    assert 'client, "gpt-4.1"' in src           # file_creator no-Anthropic-key path
    assert '_get_client(user_id), "gpt-5.6-terra"' in src  # retry/execute GPT fallbacks
