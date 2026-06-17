"""
Retry wrapper for transient API failures (429, 500, 502, 503, 529, overloaded).
Keeps the pipeline running through momentary blips instead of crashing.

Usage:
    from services.api_retry import api_call_with_retry, async_api_call_with_retry

    # Sync (OpenAI, Anthropic sync)
    resp = api_call_with_retry(lambda: client.chat.completions.create(...))

    # Async (Anthropic async)
    resp = await async_api_call_with_retry(lambda: aclient.messages.create(...))
"""

import time
import asyncio
import logging

logger = logging.getLogger(__name__)

# HTTP status codes that are safe to retry (transient server / rate-limit errors)
_TRANSIENT_CODES = {429, 500, 502, 503, 529}


def _is_transient(exc: Exception) -> bool:
    """Return True if the exception looks like a transient API error."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status and int(status) in _TRANSIENT_CODES:
        return True
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        "overloaded", "rate_limit", "rate limit",
        "connection", "timeout", "529", "503",
    ))


def api_call_with_retry(fn, max_retries=2, backoff=(1, 3)):
    """Call fn(), retry on transient API failures.

    Args:
        fn: zero-arg callable that makes the API call
        max_retries: number of retries (total attempts = 1 + max_retries)
        backoff: sleep seconds per retry (indexed by attempt)
    Returns:
        Whatever fn() returns on success.
    Raises:
        The original exception if all retries exhausted or error is not transient.
    """
    for attempt in range(1 + max_retries):
        try:
            return fn()
        except Exception as e:
            if attempt < max_retries and _is_transient(e):
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning(
                    "[API_RETRY] attempt %d/%d failed (%s: %s), retrying in %ds",
                    attempt + 1, 1 + max_retries,
                    type(e).__name__, str(e)[:120], wait,
                )
                time.sleep(wait)
                continue
            raise


async def async_api_call_with_retry(fn, max_retries=2, backoff=(1, 3)):
    """Async version of api_call_with_retry — fn() must return an awaitable."""
    for attempt in range(1 + max_retries):
        try:
            return await fn()
        except Exception as e:
            if attempt < max_retries and _is_transient(e):
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning(
                    "[API_RETRY] attempt %d/%d failed (%s: %s), retrying in %ds",
                    attempt + 1, 1 + max_retries,
                    type(e).__name__, str(e)[:120], wait,
                )
                await asyncio.sleep(wait)
                continue
            raise
