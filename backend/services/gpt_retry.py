"""OpenAI (GPT)-only billing-cap-vs-rate-limit-aware retry wrapper.

WHY THIS MODULE EXISTS (Gap D)
-------------------------------
``services/api_retry.py`` (``api_call_with_retry``) is shared by every
provider that goes through ``_chat_create`` in ``services/pipeline.py``
(OpenAI/GPT, Grok, and — defensively — anything else routed through the
OpenAI-compatible SDK client) and treats ANY HTTP 429 as transient/
retryable. For OpenAI specifically that is wrong: OpenAI returns HTTP 429
for two unrelated situations, and only one of them is worth retrying.

Confirmed directly against OpenAI's own current error-codes reference
(https://developers.openai.com/api/docs/guides/error-codes/api-errors,
"API errors" table + "Handling errors" section, read 2026):
  * "429 - Rate limit reached for requests" — no dedicated ``code``, cause is
    "sending requests too quickly" — transient, retry with backoff and honor
    ``Retry-After`` when present.
  * "429 - Credit balance exhausted" -> ``code: credit_balance_exhausted``.
  * "429 - Organization spend limit reached" -> ``code:
    organization_spend_limit_exceeded``.
  * "429 - Project spend limit reached" -> ``code:
    project_spend_limit_exceeded``.
  * "429 - Organization usage limit reached" -> ``code:
    organization_usage_limit_exceeded``.
  All four of the above are billing/quota states. The docs are explicit:
  "Retrying billing, spend, or quota errors won't restore API access. Update
  the relevant credits or limits before sending another request."

Community/production reports (OpenAI community forum threads 492350,
674562, 954398, 1091175; Stack Overflow 75898276; confirmed independently
via web research, not guessed) additionally show OpenAI has returned this
same billing state historically under ``error.type ==
"insufficient_quota"`` with ``error.code == "insufficient_quota"`` — kept
as a legacy/back-compat marker here since real traffic can still hit an
older-shaped error body depending on account/API version.

Exactly the same real-world shape xAI's ``classify_429`` already handles
for Grok (see ``services/grok_provider.py`` "Gotcha 9" / ``services/
grok_retry.py``) — a billing-cap 429 can NEVER succeed on retry, so
pointlessly retrying it (up to 2x with (1, 3)s backoff, on EVERY one of the
~24 ``_chat_create`` call sites in pipeline.py: architect turns,
corrections, QA, Surgeon, lint-fix, etc.) just burns latency and HTTP calls
before showing the user a misleading "wait a moment and try again" message
instead of "add credits / raise your spend limit".

This module does NOT touch ``api_retry.py``. It provides a GPT-scoped,
drop-in-compatible replacement for ``api_call_with_retry`` (same
``fn, max_retries=2, backoff=(1, 3)`` call shape) that:
  * On a 429, classifies it via ``classify_gpt_429`` using the response
    body's ``error.code`` / ``error.type`` fields first (structured,
    matches OpenAI's own documented shape), falling back to substring
    markers only if the body isn't a well-formed dict (e.g. an unexpected
    SDK/proxy shape) — same defensive layering as Grok's ``classify_429``.
      - Billing/quota/spend-limit -> raise ``GPTBillingCapError``
        immediately, no retry.
      - Rate-limit (or any other transient error: 500/502/503/529/timeout/
        overloaded) -> retry with the same backoff shape ``(1, 3)``s, max 2
        retries, that ``api_call_with_retry`` already uses, so genuine
        transient-OpenAI-error behavior does not regress.
  * Non-429, non-transient errors -> raise immediately, unchanged.
  * ``_dlog`` on every branch: attempt start, each retry, billing-classified
    exit, transient retry, final raise, success — so if this makes anything
    worse it is immediately visible in the pipeline log stream, not silent.

Only wired into ``_chat_create`` in pipeline.py, scoped to models that are
NOT Grok/Gemini/Claude (see the guard added there) — Claude never reaches
``_chat_create`` at all (different SDK), and Grok already has its own
dedicated billing guard (``grok_retry.py``) at its primary call site, so
this module is additive there too, never a regression, since
``classify_gpt_429`` defaults to rate-limit (retry) for anything that
doesn't match an OpenAI-specific billing marker.
"""

import time

# `database` is a top-level module (backend/database.py), NOT under
# `services` — mirrors the exact import guard grok_retry.py itself uses for
# its own `_db_dlog` fallback.
try:  # pragma: no cover - import guard only
    from database import _dlog as _db_dlog
except Exception:  # pragma: no cover
    _db_dlog = None


GPT_429_BILLING = "billing"
GPT_429_RATE_LIMIT = "rate_limit"

# Structured `error.code` / `error.type` values that OpenAI documents (or has
# been observed to return) for a billing/quota/spend-limit 429. Checked first
# against the parsed error body — precise, no false positives.
_BILLING_CODES = {
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "insufficient_quota",  # legacy shape, still seen in the wild (2024-2026 community reports)
}

# Substring fallback only used when the error body isn't a parseable dict
# with `error.code`/`error.type` (e.g. proxy/wrapper mangles the body, or an
# SDK version surfaces only a rendered message string). Mirrors the same
# defensive fallback shape as grok_provider.py's `_BILLING_MARKERS`.
_BILLING_MARKERS = (
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "insufficient_quota",
    "exceeded your current quota",
    "check your plan and billing",
    "spend limit",
)

# Mirrors api_retry.py's _TRANSIENT_CODES / _is_transient semantics for
# parity on genuinely transient (non-billing) OpenAI errors. Re-implemented
# locally, not imported, so this module never risks touching api_retry.py's
# shared behavior for other providers.
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
    same defensive pattern as ``grok_retry.py``'s own ``_dlog`` helper."""
    try:
        if dlog is not None:
            dlog(event, **kwargs)
            return
        if _db_dlog is not None:
            _db_dlog(event, **kwargs)
    except Exception:
        pass


class GPTBillingCapError(Exception):
    """Raised when an OpenAI (GPT) request hits a classified billing/quota/
    spend-limit 429 (credit balance exhausted, org/project spend limit,
    usage limit, or legacy insufficient_quota) rather than a genuine rate
    limit.

    This is a stable, explicit marker type — not a fragile substring hack —
    so ``pipeline.py``'s ``_friendly_error()`` can reliably identify it and
    surface ``gpt_429_user_message(GPT_429_BILLING)`` copy to the user
    instead of the generic "wait a moment and try again" fallback.
    """

    def __init__(self, message: str = "", *, original: Exception = None,
                 code: str = ""):
        super().__init__(message or "OpenAI billing-cap 429")
        self.original = original
        self.code = code


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
    on genuine transient OpenAI errors without importing shared behavior."""
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
    """Best-effort extraction of the raw error body dict/text from an
    OpenAI-SDK exception. Falls back to str(exc). Never raises."""
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


def classify_gpt_429(error_body, dlog=None, session_id: str = "",
                      user_id: str = "", model: str = "") -> str:
    """Classify an OpenAI 429 as a retryable rate limit vs. a hard billing/
    quota/spend-limit cap.

    Both arrive as HTTP 429 but only one is retryable: a billing/quota/
    spend-limit 429 will never succeed on retry and must surface an
    "add credits / raise your spend limit" message instead (see module
    docstring for the OpenAI docs + community-report citations). Checks the
    structured ``error.code`` / ``error.type`` fields first (OpenAI's own
    documented shape); falls back to substring markers only if the body
    isn't a parseable dict. Defaults to ``rate_limit`` — the conservative
    choice, since treating a real rate limit as a billing failure would kill
    an otherwise-recoverable run. Never raises.
    """
    try:
        body = error_body
        if isinstance(body, str):
            import json as _json
            try:
                body = _json.loads(body)
            except Exception:
                body = None
        if isinstance(body, dict):
            err = body.get("error") if isinstance(body.get("error"), dict) else body
            code = str(err.get("code") or "").strip().lower()
            etype = str(err.get("type") or "").strip().lower()
            if code in _BILLING_CODES or etype in _BILLING_CODES:
                _dlog("gpt_429_billing_cap", dlog=dlog,
                      session_id=session_id, user_id=user_id, model=model,
                      code=code or etype, retryable=False, source="structured")
                return GPT_429_BILLING
    except Exception as e:
        _dlog("gpt_429_classify_error", dlog=dlog,
              session_id=session_id, user_id=user_id, model=model,
              error_type=type(e).__name__, error=str(e)[:200])

    try:
        text = error_body if isinstance(error_body, str) else str(error_body)
    except Exception:
        text = ""
    low = (text or "").lower()
    for marker in _BILLING_MARKERS:
        if marker in low:
            _dlog("gpt_429_billing_cap", dlog=dlog,
                  session_id=session_id, user_id=user_id, model=model,
                  marker=marker, retryable=False, source="substring_fallback")
            return GPT_429_BILLING

    _dlog("gpt_429_rate_limit", dlog=dlog,
          session_id=session_id, user_id=user_id, model=model, retryable=True)
    return GPT_429_RATE_LIMIT


def gpt_429_user_message(kind: str, dlog=None) -> str:
    """User-facing copy for each 429 kind. Never raises."""
    if kind == GPT_429_BILLING:
        _dlog("gpt_429_message_billing", dlog=dlog, kind=kind)
        return ("OpenAI rejected the request: this account's credit balance is exhausted or "
                "an organization/project spend or usage limit has been reached. Add credits "
                "or raise the limit at platform.openai.com/settings/organization/billing — "
                "retrying will not help until that is resolved.")
    _dlog("gpt_429_message_rate_limit", dlog=dlog, kind=kind)
    return "OpenAI rate limit hit — backing off and retrying."


def api_call_with_billing_guard(
    fn, max_retries: int = _DEFAULT_MAX_RETRIES, backoff=_DEFAULT_BACKOFF,
    dlog=None, model: str = "", session_id: str = "", user_id: str = "",
):
    """Drop-in-compatible replacement for ``api_retry.api_call_with_retry``
    (same ``fn, max_retries=2, backoff=(1, 3)`` positional/keyword shape),
    scoped to OpenAI/GPT models.

    Classifies any 429 as billing/quota-cap (raise immediately, no retry,
    via ``GPTBillingCapError``) vs rate-limit/transient (retry with the
    same backoff shape ``api_call_with_retry`` uses elsewhere). Non-429,
    non-transient errors raise immediately, unchanged. Never silently
    swallows an exception — every branch is ``_dlog``'d.
    """
    _dlog("gpt_billing_guard_attempt_start", dlog=dlog,
          session_id=session_id, user_id=user_id, model=model,
          max_retries=max_retries)

    for attempt in range(1 + max_retries):
        try:
            result = fn()
            _dlog("gpt_billing_guard_success", dlog=dlog,
                  session_id=session_id, user_id=user_id, model=model,
                  attempt=attempt + 1)
            return result
        except Exception as e:
            if _is_429(e):
                error_body = _extract_error_body(e)
                kind = classify_gpt_429(error_body, dlog=dlog,
                                         session_id=session_id, user_id=user_id,
                                         model=model)
                if kind == GPT_429_BILLING:
                    _dlog("gpt_billing_guard_billing_429_no_retry", dlog=dlog,
                          session_id=session_id, user_id=user_id, model=model,
                          attempt=attempt + 1,
                          error_type=type(e).__name__, error=str(e)[:300])
                    raise GPTBillingCapError(
                        "OpenAI billing/quota-cap 429 — not retrying.",
                        original=e) from e
                # rate-limit 429 -> fall through to transient retry handling below.

            if attempt < max_retries and _is_transient(e):
                wait = backoff[min(attempt, len(backoff) - 1)]
                _dlog("gpt_billing_guard_transient_retry", dlog=dlog,
                      session_id=session_id, user_id=user_id, model=model,
                      attempt=attempt + 1, max_retries=max_retries, wait_s=wait,
                      error_type=type(e).__name__, error=str(e)[:300])
                time.sleep(wait)
                continue

            _dlog("gpt_billing_guard_final_raise", dlog=dlog,
                  session_id=session_id, user_id=user_id, model=model,
                  attempt=attempt + 1,
                  error_type=type(e).__name__, error=str(e)[:300])
            raise
