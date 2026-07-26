"""
Per-user rate limiting for SurgicalAI.

In-memory token bucket — no Redis dependency needed.
Limits reset on deploy (acceptable for per-minute windows on Railway).

SCALING WARNING: this dict lives in a single process. It assumes exactly
one backend instance. If this service is ever horizontally scaled to 2+
instances (autoscaling, overlapping zero-downtime deploys), each instance
will enforce its own independent limit, so a user's effective rate limit
becomes (per-instance limit × instance count). Fine for one instance;
revisit with a shared store (e.g. Redis) before scaling out.

Two tiers:
  • pipeline  — endpoints that call Claude API (expensive)
  • general   — everything else (cheap DB/proxy calls)
"""
import time
import threading
from typing import NamedTuple
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


class RateLimitDecision(NamedTuple):
    """Result of a rate-limit check.

    Carries the tier/cap/window that were actually applied so the rejection
    log line records real data rather than re-deriving which tier fired.
    Backward-compatible-ish: `allowed, retry_after` still unpack as the first
    two fields, but the single caller reads fields by name.
    """
    allowed: bool
    retry_after: int
    tier: str
    cap: int
    window: int


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

    def check(self, user_id: str, path: str) -> "RateLimitDecision":
        """Return the full decision (allowed, retry_after, tier, cap, window).

        Returns the tier/cap/window that were actually applied so a rejection
        log line is real data, not a re-guess of which tier fired.
        """
        if path in _PIPELINE_PATHS:
            cap, window = _PIPELINE_LIMIT
            tier = "pipeline"
        else:
            cap, window = _GENERAL_LIMIT
            tier = "general"

        bucket = self._bucket(f"{user_id}:{tier}", cap, window)
        if bucket.consume():
            return RateLimitDecision(True, 0, tier, cap, window)
        return RateLimitDecision(False, bucket.retry_after, tier, cap, window)

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
    decision = _limiter.check(user_id, path)
    if decision.allowed:
        # ALLOW path is per-request hot code — deliberately NO logging here.
        return None

    retry_after = decision.retry_after

    # ── Record every rejection into the exportable log ────────────────────
    # This is the rejection path only (never the allow path above), so it adds
    # no per-request overhead to normal traffic. Lazy import + fully guarded:
    # a logging failure must never stop the 429 from being returned.
    try:
        import debug_events as _debug_events
        _debug_events.emit(
            "rate_limit_rejected",
            user_id=user_id,
            path=path,
            tier=decision.tier,
            retry_after=retry_after,
            cap=decision.cap,
            window=decision.window,
        )
    except Exception:
        pass  # never let a logging failure suppress the 429

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
