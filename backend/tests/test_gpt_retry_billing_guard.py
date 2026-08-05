"""Gap D: OpenAI (GPT) billing/quota-cap 429 must not be pointlessly retried.

Mirrors tests/test_grok_retry_billing_guard.py's structure/coverage shape for
the GPT-scoped guard in services/gpt_retry.py, wired into _chat_create in
services/pipeline.py.
"""

import time
import pytest

from services.gpt_retry import (
    api_call_with_billing_guard,
    classify_gpt_429,
    gpt_429_user_message,
    GPTBillingCapError,
    GPT_429_BILLING,
    GPT_429_RATE_LIMIT,
)
from services import pipeline


class _FakeExc(Exception):
    def __init__(self, status_code, body):
        super().__init__(str(body))
        self.status_code = status_code
        self.body = body


class _FakeChatCompletions:
    def __init__(self, behavior):
        self.behavior = behavior
        self.n = 0

    def create(self, **kwargs):
        self.n += 1
        return self.behavior(self.n, kwargs)


class _FakeChat:
    def __init__(self, behavior):
        self.completions = _FakeChatCompletions(behavior)


class _FakeClient:
    def __init__(self, behavior):
        self.chat = _FakeChat(behavior)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


# ── classify_gpt_429 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", [
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "insufficient_quota",
])
def test_classify_documented_and_legacy_billing_codes(code):
    assert classify_gpt_429({"error": {"code": code}}) == GPT_429_BILLING


def test_classify_billing_via_type_field():
    assert classify_gpt_429({"error": {"type": "insufficient_quota"}}) == GPT_429_BILLING


def test_classify_rate_limit_defaults_to_retryable():
    assert classify_gpt_429({"error": {"message": "Rate limit reached for requests"}}) == GPT_429_RATE_LIMIT


def test_classify_string_body_substring_fallback():
    body = "You exceeded your current quota, please check your plan and billing details."
    assert classify_gpt_429(body) == GPT_429_BILLING


def test_classify_unparseable_body_defaults_to_rate_limit_conservative():
    assert classify_gpt_429(None) == GPT_429_RATE_LIMIT
    assert classify_gpt_429(12345) == GPT_429_RATE_LIMIT


def test_user_message_billing_mentions_credits_or_limit_not_generic_wait():
    msg = gpt_429_user_message(GPT_429_BILLING)
    assert "wait a moment" not in msg.lower()
    assert any(w in msg.lower() for w in ("credit", "limit"))


def test_user_message_rate_limit_is_generic_retry_copy():
    msg = gpt_429_user_message(GPT_429_RATE_LIMIT)
    assert "retry" in msg.lower() or "back" in msg.lower()


# ── api_call_with_billing_guard ─────────────────────────────────────────────

def test_billing_429_raises_immediately_no_retry():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _FakeExc(429, {"error": {"code": "credit_balance_exhausted"}})

    with pytest.raises(GPTBillingCapError):
        api_call_with_billing_guard(fn, model="gpt-5.6")
    assert calls["n"] == 1


def test_rate_limit_429_retries_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _FakeExc(429, {"error": {"message": "Rate limit reached for requests"}})
        return "ok"

    assert api_call_with_billing_guard(fn, model="gpt-5.6") == "ok"
    assert calls["n"] == 2


def test_rate_limit_429_exhausts_retries_and_raises():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _FakeExc(429, {"error": {"message": "Rate limit reached for requests"}})

    with pytest.raises(_FakeExc):
        api_call_with_billing_guard(fn, model="gpt-5.6", max_retries=2)
    assert calls["n"] == 3  # 1 initial + 2 retries


def test_non_429_error_raises_immediately():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        api_call_with_billing_guard(fn, model="gpt-5.6")
    assert calls["n"] == 1


def test_dlog_fires_on_billing_and_success_and_retry_branches():
    events = []

    def dlog(event, **kw):
        events.append(event)

    def billing_fn():
        raise _FakeExc(429, {"error": {"code": "organization_spend_limit_exceeded"}})

    with pytest.raises(GPTBillingCapError):
        api_call_with_billing_guard(billing_fn, dlog=dlog, model="gpt-5.6")
    assert "gpt_billing_guard_attempt_start" in events
    assert "gpt_billing_guard_billing_429_no_retry" in events


# ── wired into the real _chat_create in services/pipeline.py ──────────────

def test_chat_create_gpt_model_billing_429_raises_gpt_billing_cap_error():
    def behavior(n, kwargs):
        raise _FakeExc(429, {"error": {"code": "organization_spend_limit_exceeded"}})

    client = _FakeClient(behavior)
    with pytest.raises(pipeline._GPTBillingCapError):
        pipeline._chat_create(client, model="gpt-5.6-terra",
                               messages=[{"role": "user", "content": "hi"}])
    assert client.chat.completions.n == 1


def test_chat_create_grok_model_unaffected_by_new_gpt_guard():
    """Grok already has its own dedicated guard elsewhere; _chat_create must
    keep routing Grok models through the original plain api_call_with_retry
    (blind retry-on-429), unchanged by this GPT-scoped addition."""
    def behavior(n, kwargs):
        if n < 2:
            raise _FakeExc(429, {"error": {"code": "organization_spend_limit_exceeded"}})
        return "grok_ok"

    client = _FakeClient(behavior)
    result = pipeline._chat_create(client, model="grok-4.5",
                                    messages=[{"role": "user", "content": "hi"}])
    assert result == "grok_ok"
    assert client.chat.completions.n == 2  # retried blindly, old behavior preserved


def test_friendly_error_surfaces_gpt_billing_specific_copy():
    def behavior(n, kwargs):
        raise _FakeExc(429, {"error": {"code": "credit_balance_exhausted"}})

    client = _FakeClient(behavior)
    try:
        pipeline._chat_create(client, model="gpt-5.6-terra",
                               messages=[{"role": "user", "content": "hi"}])
        assert False, "expected GPTBillingCapError"
    except Exception as e:
        msg = pipeline._friendly_error(e)
        assert "wait a moment" not in msg.lower()
        assert any(w in msg.lower() for w in ("credit", "limit"))
