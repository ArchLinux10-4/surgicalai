/** Same rules as the backend address bar: localhost stays http, everything else https. */
export function normalizeUrl(raw: string): string {
  const text = raw.trim()
  if (!text) return ''
  const lower = text.toLowerCase()
  if (lower.startsWith('http://') || lower.startsWith('https://') || lower.startsWith('about:')) return text
  if (/^(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?(\/|$)/.test(lower)) return `http://${text}`
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(text)) return text
  return `https://${text}`
}

/** Alt=1, Ctrl=2, Meta=4, Shift=8. Matches the browser's CDP modifier bitmask. */
export function modifierMask(e: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): number {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0)
}

export function buttonName(button: number): string {
  if (button === 1) return 'middle'
  if (button === 2) return 'right'
  return 'left'
}
