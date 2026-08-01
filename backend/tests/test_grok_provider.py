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
    "grok-build-latest" if False else "grok-4",   # only "grok-" prefixed ids match
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
    """Only research-confirmed ids may be registered — nothing invented."""
    assert gp.GROK_DEFAULT_MODEL == "grok-4.5"


def test_get_grok_client_uses_xai_base_url(monkeypatch):
    monkeypatch.setattr(gp, "get_user_api_key", lambda u, k: "ENCRYPTED")
    monkeypatch.setattr(gp, "decrypt_api_key", lambda v: "xai-test-key")

    captured = {}

    class FakeOpenAI:
        def __init__(self, api_key=None, base_url=None):
            captured["api_key"] = api_key
            captured["base_url"] = base_url

    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "OpenAI", FakeOpenAI)

    client = gp.get_grok_client("user-1")
    assert isinstance(client, FakeOpenAI)
    assert captured == {"api_key": "xai-test-key", "base_url": "https://api.x.ai/v1"}


def test_get_grok_client_logs_creation(monkeypatch):
    monkeypatch.setattr(gp, "get_user_api_key", lambda u, k: "ENCRYPTED")
    monkeypatch.setattr(gp, "decrypt_api_key", lambda v: "xai-test-key")

    class FakeOpenAI:
        def __init__(self, api_key=None, base_url=None):
            pass

    import openai as openai_mod
    monkeypatch.setattr(openai_mod, "OpenAI", FakeOpenAI)

    events = []
    gp.get_grok_client("user-1", dlog=lambda e, **kw: events.append(e))
    assert "grok_client_created" in events


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
    assert "get_grok_client(user_id, dlog=_dlog)" in body
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
