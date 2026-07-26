/**
 * Bounded-concurrency task runner + rate-limit-aware retry.
 *
 * WHY THIS EXISTS
 * ---------------
 * `uploadFiles` used to fire every selected file at once via `Promise.all`.
 * The backend rate limiter allows 120 general requests per 60s per user
 * (backend/middleware/rate_limiter.py `_GENERAL_LIMIT`), so a large multi-file
 * attach blows straight past it.
 *
 * PROVEN OCCURRENCE (session c72c446e, 2026-07-26 05:54:55): 165 files were
 * selected, 148 POSTs returned 200 and 17 returned 429 inside a 22-second
 * burst. `UserManagementModal.jsx` was one of the seventeen. The UI still
 * announced "165 files ready" because it counted the files it had *selected*,
 * not the ones the server had *accepted* — so the user had no way to know that
 * 17 files were missing from the session.
 *
 * Two fixes live here:
 *   1. `runWithConcurrency` — never have more than `limit` requests in flight.
 *   2. `retryRateLimited`   — when the server does push back with 429 (or a
 *      transient 502/503/504), wait the interval the server asked for and try
 *      again instead of dropping the file on the floor.
 */

/** Statuses worth retrying: rate limiting plus transient gateway errors.
 *  4xx other than 429 are deterministic — retrying them just wastes time. */
const RETRYABLE_STATUSES = new Set([429, 502, 503, 504])

export interface RetryOptions {
  /** Total attempts, including the first one. */
  attempts?: number
  /** Base delay in ms used when the server does not tell us how long to wait. */
  baseDelayMs?: number
  /** Called before each retry — used for user-visible progress. */
  onRetry?: (attempt: number, delayMs: number, error: unknown) => void
}

const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

function statusOf(e: unknown): number | undefined {
  const s = (e as { status?: unknown })?.status
  return typeof s === 'number' ? s : undefined
}

function retryAfterMsOf(e: unknown): number | undefined {
  const ra = (e as { retryAfter?: unknown })?.retryAfter
  return typeof ra === 'number' && ra > 0 ? ra * 1000 : undefined
}

/**
 * Run `task`, retrying while the failure is retryable and attempts remain.
 * Honours the server's `Retry-After` when present, otherwise backs off
 * exponentially with jitter so a batch of retries does not resynchronise into
 * a fresh stampede.
 */
export async function retryRateLimited<T>(
  task: () => Promise<T>,
  opts: RetryOptions = {},
): Promise<T> {
  const attempts = Math.max(1, opts.attempts ?? 4)
  const base = opts.baseDelayMs ?? 1000
  let lastErr: unknown

  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      return await task()
    } catch (e) {
      lastErr = e
      const status = statusOf(e)
      const retryable = status !== undefined && RETRYABLE_STATUSES.has(status)
      if (!retryable || attempt === attempts) throw e

      // Server-directed wait wins; otherwise exponential backoff + jitter.
      const backoff = base * Math.pow(2, attempt - 1)
      const jitter = Math.floor(Math.random() * base)
      const delay = retryAfterMsOf(e) ?? backoff + jitter
      opts.onRetry?.(attempt, delay, e)
      await sleep(delay)
    }
  }
  throw lastErr
}

/**
 * Run `tasks` with at most `limit` running concurrently, preserving input
 * order in the returned array.
 *
 * Unlike `Promise.all`, a rejected task does not abort the rest: every result
 * is reported so the caller can tell the user exactly what landed and what
 * did not.
 */
export async function runWithConcurrency<T>(
  tasks: Array<() => Promise<T>>,
  limit = 4,
): Promise<Array<{ ok: true; value: T } | { ok: false; error: unknown }>> {
  const results = new Array<{ ok: true; value: T } | { ok: false; error: unknown }>(
    tasks.length,
  )
  let next = 0
  const width = Math.max(1, Math.min(limit, tasks.length))

  const worker = async () => {
    while (true) {
      const i = next++
      if (i >= tasks.length) return
      try {
        results[i] = { ok: true, value: await tasks[i]() }
      } catch (error) {
        results[i] = { ok: false, error }
      }
    }
  }

  await Promise.all(Array.from({ length: width }, worker))
  return results
}

/** Requests in flight at once. The general limit is 120/60s = 2/s sustained;
 *  4 in flight keeps latency low while leaving the 429 retry path as the
 *  adaptive brake rather than guessing at a fixed pacing interval. */
export const UPLOAD_CONCURRENCY = 4
