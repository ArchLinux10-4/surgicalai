import { describe, expect, it } from 'vitest'
import { parseServerUtc, relativeTimeFromServer } from './relativeTime'

describe('parseServerUtc', () => {
  it('treats SQLite CURRENT_TIMESTAMP (no Z) as UTC, not local', () => {
    const d = parseServerUtc('2026-08-13 06:51:00')
    expect(d).not.toBeNull()
    expect(d!.toISOString()).toBe('2026-08-13T06:51:00.000Z')
  })

  it('keeps already-Z timestamps', () => {
    const d = parseServerUtc('2026-08-13T06:51:00Z')
    expect(d!.toISOString()).toBe('2026-08-13T06:51:00.000Z')
  })
})

describe('relativeTimeFromServer', () => {
  it('does not collapse past UTC stamps to just now in US timezones', () => {
    // 3 hours ago in UTC — bare Date() in CDT would read as local and look
    // ~2h in the future → "just now". Our parser must report ~3h ago.
    const threeHoursAgoUtc = new Date(Date.now() - 3 * 60 * 60 * 1000)
      .toISOString()
      .replace('T', ' ')
      .replace(/\.\d{3}Z$/, '')
    expect(relativeTimeFromServer(threeHoursAgoUtc)).toBe('3h ago')
  })

  it('returns just now for very recent stamps', () => {
    const nowUtc = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')
    expect(relativeTimeFromServer(nowUtc)).toBe('just now')
  })
})
