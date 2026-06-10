"""
Per-user rate limiting for SurgicalAI.

In-memory token bucket — no Redis dependency needed.
Limits reset on deploy (acceptable for per-minute windows on Railway).

Two tiers:
  • pipeline  — endpoints that call Claude API (expensive)
  • general   — everything else (cheap DB/proxy calls)
"""
import time
import threading
from fastapi.responses import JSONResponse


# ── Rate-limited path groups ──────────────────────────────────────────────────
# Endpoints that trigger Claude API calls — most expensive operations
_PIPELINE_PATHS = frozenset({
    "/api/chat/send",
    "/api/chat/stream",
    "/api/chat/smart-stream",
    "/api/surgical/analyze",
    "/api/surgical/analyze-stream",
})

# Limits: (max_requests, window_seconds)
_PIPELINE_LIMIT = (10, 60)     # 10 Claude calls per minute per user
_GENERAL_LIMIT  = (120, 60)    # 120 requests per minute per user


# ── Token bucket ──────────────────────────────────────────────────────────────
class _Bucket:
    """Continuous-refill token bucket."""
    __slots__ = ("max_tokens", "refill_rate", "tokens", "last_refill")

    def __init__(self, max_tokens: int, window: float):
        self.max_tokens = max_tokens
        self.refill_rate = max_tokens / window
        self.tokens = float(max_tokens)
        self.last_refill = time.monotonic()

    def consume(self) -> bool:
        now = time.monotonic()
        self.tokens = min(
            self.max_tokens,
            self.tokens + (now - self.last_refill) * self.refill_rate,
        )
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    @property
    def retry_after(self) -> int:
        """Seconds until next token is available."""
        if self.tokens >= 1.0:
            return 0
        return max(1, int((1.0 - self.tokens) / self.refill_rate) + 1)


class _RateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def _bucket(self, key: str, max_t: int, window: int) -> _Bucket:
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = _Bucket(max_t, window)
            return self._buckets[key]

    def check(self, user_id: str, path: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        if path in _PIPELINE_PATHS:
            cap, window = _PIPELINE_LIMIT
            tier = "pipeline"
        else:
            cap, window = _GENERAL_LIMIT
            tier = "general"

        bucket = self._bucket(f"{user_id}:{tier}", cap, window)
        if bucket.consume():
            return True, 0
        return False, bucket.retry_after

    def cleanup(self, max_idle: int = 3600):
        """Remove buckets idle longer than max_idle seconds."""
        now = time.monotonic()
        with self._lock:
            stale = [k for k, b in self._buckets.items()
                     if now - b.last_refill > max_idle]
            for k in stale:
                del self._buckets[k]


_limiter = _RateLimiter()


def check_rate_limit(
    user_id: str,
    path: str,
    cors_headers: dict | None = None,
) -> JSONResponse | None:
    """
    Check rate limit for user + path.
    Returns None if allowed, or a 429 JSONResponse if rate-limited.
    Called from auth_middleware in main.py after JWT is validated.
    """
    allowed, retry_after = _limiter.check(user_id, path)
    if allowed:
        return None

    headers = {"Retry-After": str(retry_after)}
    if cors_headers:
        headers.update(cors_headers)

    return JSONResponse(
        status_code=429,
        content={
            "detail": f"Rate limit exceeded. Try again in {retry_after} second{'s' if retry_after != 1 else ''}.",
            "retry_after": retry_after,
        },
        headers=headers,
    )
