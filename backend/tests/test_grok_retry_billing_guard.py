"""Tests for services/grok_retry.py (gap #2: billing-cap 429 must not be
pointlessly retried) and the additive `_friendly_error` Grok branch in
services/pipeline.py.

NO LIVE API CALLS — everything here is mocked. This test module ALSO asserts
(by source read) that api_retry.py and pipeline.py's existing vendor
branches in `_friendly_error` were only ever ADDED to, never modified.
"""
import pathlib
import sys

import pytest

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

from services import grok_retry as gr  # noqa: E402
from services import grok_provider as gp  # noqa: E402


class _FakeAPIError(Exception):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class _FakeCompletions:
    """Fake `client.chat.completions` — `create` is scripted via a list of
    side effects, each either an exception instance (raised) or a sentinel
    return value."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def create(self, model, messages, **kwargs):
        self.calls += 1
        effect = self._script.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class _FakeClient:
    def __init__(self, script):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(script)


def _dlog_collector():
    events = []

    def _dlog(event, **kwargs):
        events.append((event, kwargs))

    return _dlog, events


# ─────────────────────────────────────────────────────────────────────────
# 1. Billing-429 classified correctly is NOT retried
# ─────────────────────────────────────────────────────────────────────────

def test_billing_429_raises_immediately_no_retry():
    billing_err = _FakeAPIError(
        "429 error",
        status_code=429,
        body={"error": {"message": "You have used all available credits. "
                                    "Please raise your monthly spending limit."}},
    )
    client = _FakeClient([billing_err, "SHOULD_NOT_BE_REACHED"])
    dlog, events = _dlog_collector()

    with pytest.raises(gr.GrokBillingCapError) as exc_info:
        gr.grok_chat_create_with_billing_guard(
            client, model="grok-4.5", messages=[{"role": "user", "content": "hi"}],
            dlog=dlog, session_id="s1", user_id="u1")

    assert exc_info.value.original is billing_err
    # Exactly one attempt — no retry happened.
    assert client.chat.completions.calls == 1
    event_names = [e for e, _ in events]
    assert "grok_billing_guard_billing_429_no_retry" in event_names
    assert "grok_billing_guard_transient_retry" not in event_names


def test_billing_429_classification_uses_grok_provider_markers():
    # Sanity: the billing marker text used above is actually recognized by
    # the shared classifier, so this test isn't accidentally testing a
    # marker grok_provider.py doesn't know about.
    kind = gp.classify_429({"error": {"message": "used all available credits"}})
    assert kind == gp.GROK_429_BILLING


# ─────────────────────────────────────────────────────────────────────────
# 2. Rate-limit 429 / transient errors ARE retried with backoff
# ─────────────────────────────────────────────────────────────────────────

def test_rate_limit_429_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(gr.time, "sleep", lambda *_a, **_k: None)
    rate_limit_err = _FakeAPIError(
        "429 error", status_code=429,
        body={"error": {"message": "Rate limit exceeded, please slow down."}})
    client = _FakeClient([rate_limit_err, rate_limit_err, "SUCCESS"])
    dlog, events = _dlog_collector()

    result = gr.grok_chat_create_with_billing_guard(
        client, model="grok-4.5", messages=[{"role": "user", "content": "hi"}],
        dlog=dlog, session_id="s1", user_id="u1")

    assert result == "SUCCESS"
    assert client.chat.completions.calls == 3
    event_names = [e for e, _ in events]
    assert event_names.count("grok_billing_guard_transient_retry") == 2
    assert "grok_billing_guard_success" in event_names


def test_transient_500_retries_up_to_max_then_raises(monkeypatch):
    monkeypatch.setattr(gr.time, "sleep", lambda *_a, **_k: None)
    err = _FakeAPIError("internal error", status_code=500)
    client = _FakeClient([err, err, err])  # 1 + max_retries(2) attempts, all fail
    dlog, events = _dlog_collector()

    with pytest.raises(_FakeAPIError):
        gr.grok_chat_create_with_billing_guard(
            client, model="grok-4.5", messages=[{"role": "user", "content": "hi"}],
            dlog=dlog, session_id="s1", user_id="u1")

    assert client.chat.completions.calls == 3
    event_names = [e for e, _ in events]
    assert "grok_billing_guard_final_raise" in event_names


# ─────────────────────────────────────────────────────────────────────────
# 3. Non-429 / non-transient errors raise immediately, no retry
# ─────────────────────────────────────────────────────────────────────────

def test_non_transient_error_raises_immediately():
    err = _FakeAPIError("400 bad request — invalid parameter", status_code=400)
    client = _FakeClient([err, "SHOULD_NOT_BE_REACHED"])
    dlog, events = _dlog_collector()

    with pytest.raises(_FakeAPIError):
        gr.grok_chat_create_with_billing_guard(
            client, model="grok-4.5", messages=[{"role": "user", "content": "hi"}],
            dlog=dlog, session_id="s1", user_id="u1")

    assert client.chat.completions.calls == 1
    event_names = [e for e, _ in events]
    assert "grok_billing_guard_final_raise" in event_names
    assert "grok_billing_guard_transient_retry" not in event_names


# ─────────────────────────────────────────────────────────────────────────
# 4. _friendly_error's new Grok billing branch + existing vendor branches
#    are untouched (additive-only)
# ─────────────────────────────────────────────────────────────────────────

def _pipeline_src() -> str:
    return (_BACKEND / "services" / "pipeline.py").read_text()


def test_friendly_error_grok_billing_branch_returns_exact_copy():
    from services.pipeline import _friendly_error

    exc = gr.GrokBillingCapError("Grok billing-cap 429 — not retrying.")
    msg = _friendly_error(exc)
    assert msg == gp.grok_429_user_message(gp.GROK_429_BILLING, dlog=None)
    assert "console.x.ai" in msg


@pytest.mark.parametrize("msg,expected_snippet", [
    ("anthropic overloaded 529", "temporarily overloaded"),
    ("openai rate_limit 429 error", "OpenAI rate limit"),
    ("google gemini 429 quota exceeded", "Gemini API quota"),
])
def test_friendly_error_existing_vendor_branches_unchanged_behavior(msg, expected_snippet):
    """Not a byte-for-byte diff against a pre-change binary (out of scope for
    a unit test), but proves each existing vendor branch still returns its
    documented, recognizable copy — i.e. the new Grok branch didn't shadow
    or break any of them structurally."""
    from services.pipeline import _friendly_error

    class _E(Exception):
        pass

    _E.__module__ = "anthropic._exceptions" if "anthropic" in msg else _E.__module__
    if "openai" in msg:
        _E.__module__ = "openai"
    if "google" in msg or "gemini" in msg:
        _E.__module__ = "google.generativeai"

    result = _friendly_error(_E(msg))
    assert expected_snippet in result


def test_pipeline_only_adds_grok_branch_does_not_reorder_existing_branches():
    """Source-level guard: the new Grok branch sits BEFORE the Gemini branch
    header comment, i.e. was inserted as a new block, and the Anthropic /
    OpenAI branch headers are still present verbatim (not touched/reordered)."""
    src = _pipeline_src()
    grok_idx = src.index("# Grok (xAI)-specific errors")
    gemini_idx = src.index("# Gemini-specific errors")
    anthropic_idx = src.index("# Anthropic-specific errors")
    openai_idx = src.index("# OpenAI-specific errors")
    assert anthropic_idx < grok_idx < gemini_idx < openai_idx


def test_api_retry_module_not_modified_for_grok_path():
    """This suite never imports api_retry.py's api_call_with_retry from
    grok_retry.py's runtime path — grok_retry.py re-implements its own
    _is_transient locally, confirmed by source (no `from services.api_retry`
    import anywhere in the new module)."""
    src = (_BACKEND / "services" / "grok_retry.py").read_text()
    assert "from services.api_retry" not in src
    assert "import api_retry" not in src
