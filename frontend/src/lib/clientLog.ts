/**
 * Fire-and-forget browser → exportable-log reporter.
 *
 * WHY THIS EXISTS
 * ---------------
 * Browser-side failures (upload retries under rate limiting, a file that fell
 * down the wrong upload path, a final per-file upload failure) never reached
 * the log the user downloads (`GET /api/debug/pipeline-log/download`) because
 * there was no client-event ingest endpoint at all. This posts a single small
 * event to `POST /api/debug/client-event`, which stamps the server-known
 * user_id and writes it into the same `debug_events` table.
 *
 * CONTRACT
 * --------
 *   • Never throws, never blocks the UI (returns immediately; the POST runs
 *     detached and its rejection is swallowed).
 *   • Silently no-ops if the request fails or there is no auth token.
 *   • Authenticates exactly like every other API call (same BASE + Bearer
 *     token from the persisted auth store).
 *   • NEVER logs file contents or tokens — filenames and sizes only. Keep the
 *     `data` object tiny; the server caps/truncates it regardless.
 */
import { API_BASE, getAuthToken } from '../api/client'

export function clientLog(event: string, data: Record<string, unknown> = {}, sessionId?: string): void {
  try {
    const token = getAuthToken()
    if (!token) return // unauthenticated — the endpoint requires auth; no-op

    const body = JSON.stringify({ event, data, session_id: sessionId ?? '' })

    // Detached, best-effort. Any network/parse/serialize failure is swallowed
    // so a logging call can never surface an error or block the caller.
    void fetch(`${API_BASE}/debug/client-event`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body,
      keepalive: true, // survive a page transition without blocking it
    }).catch(() => { /* silent no-op */ })
  } catch {
    /* never throw from a logging call */
  }
}
