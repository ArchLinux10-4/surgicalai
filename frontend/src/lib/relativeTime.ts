/**
 * Relative timestamps for server-stored datetimes.
 *
 * SQLite `CURRENT_TIMESTAMP` and `datetime.utcnow().isoformat()` are UTC
 * *without* a `Z` / offset. `new Date("2026-08-13 06:51:00")` treats that as
 * *local*, so in US timezones the stamp is hours in the future and every
 * relative formatter collapses to "just now".
 *
 * History in InlineDiffCard already fixed this (`iso + 'Z'`). Keep that rule
 * here so the file tray matches History.
 */

export function parseServerUtc(iso?: string | null): Date | null {
  if (!iso) return null
  const s = String(iso).trim()
  if (!s) return null
  const withZ = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(s) ? s : `${s}Z`
  const d = new Date(withZ)
  return Number.isNaN(d.getTime()) ? null : d
}

/** Short relative label — same buckets as History (`just now` / `Nm ago` / `Nh ago`). */
export function relativeTimeFromServer(iso?: string | null): string {
  const d = parseServerUtc(iso)
  if (!d) return ''
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}
