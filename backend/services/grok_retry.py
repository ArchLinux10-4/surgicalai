"""Grok (xAI)-only billing-vs-rate-limit-aware retry wrapper for the native
streaming chat-completions call.

WHY THIS MODULE EXISTS
-----------------------
``services/api_retry.py`` (``api_call_with_retry``) is shared by every
provider (OpenAI, Claude, Grok) and treats ANY HTTP 429 as transient/
retryable — it has zero awareness of xAI's billing-cap-vs-rate-limit
distinction (see ``services/grok_provider.py``'s "Gotcha 9" section:
``classify_429`` / ``GROK_429_BILLING`` / ``GROK_429_RATE_LIMIT``). That
classifier already exists but is orphaned — only reachable from a rarely-hit
fallback path in ``grok_correction.py``, not the main Grok streaming request
in ``pipeline.py``'s ``_chat_create`` call. As a result, a hard billing-cap
429 (which can NEVER succeed on retry — confirmed by OpenAI's own error-code
docs: https://developers.openai.com/api/docs/guides/error-codes, which
explicitly separates "429 - Rate limit reached for requests" [retryable,
honor ``Retry-After`` / backoff] from "429 - Organization spend limit
reached" [[not retryable — "retrying billing, spend, or quota errors won't
restore API access"]], the same real-world distinction xAI's own 429 body
text encodes] gets pointlessly retried up to twice with (1, 3)s backoff by
``api_call_with_retry`` before the pipeline gives up with a generic error.
That's wasted latency + wasted HTTP calls on something guaranteed to fail
again, with no useful message surfaced to the user.

This module does NOT touch ``api_retry.py`` or ``_chat_create``'s shared
body. It is a Grok-only wrapper that:
  * Makes the ``client.chat.completions.create(...)`` call itself (not via
    ``api_call_with_retry``, so the shared retry path is never touched or
    even imported for behavior — only its constants are mirrored for parity).
  * On a 429, classifies it via ``grok_provider.classify_429``:
      - Billing -> raise ``GrokBillingCapError`` immediately, no retry.
      - Rate-limit (or any other transient error, 500/502/503/529/timeout/
        overloaded) -> retry with the same backoff shape ``(1, 3)``s, max 2
        retries, that ``api_call_with_retry`` already uses elsewhere, so
        genuine transient-Grok-error behavior does not regress.
  * Non-429, non-transient errors -> raise immediately, unchanged.
  * ``_dlog`` on every branch: attempt start, each retry, billing-classified
    exit, transient retry, final raise, success.

Only reachable from ``if _is_grok_model(...):`` branches in ``pipeline.py``;
nothing here touches the Claude/GPT/Gemini paths, which continue to call
``_chat_create`` -> ``api_call_with_retry`` completely unchanged.
"""

import time

try:  # pragma: no cover - import shape differs only by cwd, mirrors grok_agent_tools.py
    from services.grok_provider import classify_429, GROK_429_BILLING, GROK_429_RATE_LIMIT
except Exception:  # pragma: no cover
    try:
        from grok_provider import classify_429, GROK_429_BILLING, GROK_429_RATE_LIMIT
    except Exception:  # pragma: no cover
        classify_429 = None
        GROK_429_BILLING = "billing"
        GROK_429_RATE_LIMIT = "rate_limit"

# `database` is a top-level module (backend/database.py), NOT under
# `services` — mirrors the exact import guard grok_provider.py itself uses
# for its own `_db_dlog` fallback.
try:  # pragma: no cover - import guard only
    from database import _dlog as _db_dlog
except Exception:  # pragma: no cover
    _db_dlog = None


# Mirrors api_retry.py's _TRANSIENT_CODES / _is_transient semantics for parity
# on genuinely transient (non-billing) Grok errors. Re-implemented locally,
# not imported, so this module never risks touching api_retry.py's shared
# behavior for other providers.
_TRANSIENT_CODES = {429, 500, 502, 503, 529}
_TRANSIENT_KEYWORDS = (
    "overloaded", "rate_limit", "rate limit",
    "connection", "timeout", "529", "503",
)

# Same backoff shape as api_call_with_retry's default, for behavioral parity.
_DEFAULT_BACKOFF = (1, 3)
_DEFAULT_MAX_RETRIES = 2


def _dlog(event: str, dlog=None, **kwargs):
    """Structured debug log for this module. Prefers the caller-injected
    ``dlog`` (pipeline.py's ``_dlog``), falls back to ``database._dlog``.
    Never raises — a logging failure must never break a real request. Exact
    same defensive pattern as ``grok_provider.py``'s own ``_dlog`` helper."""
    try:
        if dlog is not None:
            dlog(event, **kwargs)
            return
        if _db_dlog is not None:
            _db_dlog(event, **kwargs)
    except Exception:
        pass


class GrokBillingCapError(Exception):
    """Raised when a Grok (xAI) request hits a classified billing-cap 429
    (spend limit / out of credits) rather than a genuine rate limit.

    This is a stable, explicit marker type — not a fragile substring hack —
    so ``pipeline.py``'s ``_friendly_error()`` can reliably identify it later
    and surface ``grok_provider.grok_429_user_message(GROK_429_BILLING, ...)``
    copy to the user instead of a generic fallback message.
    """

    def __init__(self, message: str = "", *, original: Exception = None):
        super().__init__(message or "Grok billing-cap 429")
        self.original = original


def _status_code(exc: Exception):
    return getattr(exc, "status_code", None) or getattr(exc, "status", None)


def _is_429(exc: Exception) -> bool:
    status = _status_code(exc)
    if status is not None:
        try:
            if int(status) == 429:
                return True
        except Exception:
            pass
    msg = str(exc).lower()
    return "429" in msg or "rate_limit" in msg or "rate limit" in msg or "too many requests" in msg


def _is_transient(exc: Exception) -> bool:
    """Local re-implementation of api_retry.py's _is_transient, for parity
    on genuine transient Grok errors without importing shared behavior."""
    status = _status_code(exc)
    if status:
        try:
            if int(status) in _TRANSIENT_CODES:
                return True
        except Exception:
            pass
    msg = str(exc).lower()
    return any(kw in msg for kw in _TRANSIENT_KEYWORDS)


def _extract_error_body(exc: Exception):
    """Best-effort extraction of the raw error body text/dict from an SDK
    exception, for classify_429. Falls back to str(exc). Never raises."""
    try:
        body = getattr(exc, "body", None)
        if body:
            return body
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                return response.json()
            except Exception:
                text = getattr(response, "text", None)
                if text:
                    return text
        message = getattr(exc, "message", None)
        if message:
            return message
    except Exception:
        pass
    return str(exc)


def grok_chat_create_with_billing_guard(
    client, model: str, messages: list, dlog=None,
    session_id: str = "", user_id: str = "",
    max_retries: int = _DEFAULT_MAX_RETRIES, backoff=_DEFAULT_BACKOFF,
    **kwargs,
):
    """Grok-only wrapper around ``client.chat.completions.create(...)``.

    Classifies any 429 as billing-cap (raise immediately, no retry, via
    ``GrokBillingCapError``) vs rate-limit/transient (retry with the same
    backoff shape ``api_call_with_retry`` uses elsewhere). Non-429,
    non-transient errors raise immediately, unchanged. Never silently
    swallows an exception — every branch is ``_dlog``'d.
    """
    _dlog("grok_billing_guard_attempt_start", dlog=dlog,
          session_id=session_id, user_id=user_id, model=model,
          max_retries=max_retries)

    for attempt in range(1 + max_retries):
        try:
            result = client.chat.completions.create(model=model, messages=messages, **kwargs)
            _dlog("grok_billing_guard_success", dlog=dlog,
                  session_id=session_id, user_id=user_id, model=model,
                  attempt=attempt + 1)
            return result
        except Exception as e:
            if _is_429(e):
                error_body = _extract_error_body(e)
                kind = None
                if classify_429 is not None:
                    kind = classify_429(error_body, dlog=dlog,
                                        session_id=session_id, user_id=user_id)
                if kind == GROK_429_BILLING:
                    _dlog("grok_billing_guard_billing_429_no_retry", dlog=dlog,
                          session_id=session_id, user_id=user_id, model=model,
                          attempt=attempt + 1,
                          error_type=type(e).__name__, error=str(e)[:300])
                    raise GrokBillingCapError(
                        "Grok billing-cap 429 — not retrying.", original=e) from e
                # rate-limit 429 -> fall through to transient retry handling below.

            if attempt < max_retries and _is_transient(e):
                wait = backoff[min(attempt, len(backoff) - 1)]
                _dlog("grok_billing_guard_transient_retry", dlog=dlog,
                      session_id=session_id, user_id=user_id, model=model,
                      attempt=attempt + 1, max_retries=max_retries, wait_s=wait,
                      error_type=type(e).__name__, error=str(e)[:300])
                time.sleep(wait)
                continue

            _dlog("grok_billing_guard_final_raise", dlog=dlog,
                  session_id=session_id, user_id=user_id, model=model,
                  attempt=attempt + 1,
                  error_type=type(e).__name__, error=str(e)[:300])
            raise
