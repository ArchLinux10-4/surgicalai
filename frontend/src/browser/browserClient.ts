import { API_BASE, getAuthToken, request, wsUrl } from '../api/client'

export interface TabInfo {
  index: number
  title: string
  display_url: string
  loading: boolean
  favicon: string
  active: boolean
}

export interface BrowserStatus {
  enabled: boolean
  running: boolean
  url: string
  display_url: string
  title: string
  favicon?: string
  loading: boolean
  can_go_back: boolean
  can_go_forward: boolean
  viewport: number[]
  pick_mode: boolean
  clients: number
  last_error: string | null
  tabs?: TabInfo[]
  active_tab?: number
}

export interface ConsoleEntry {
  ts: number
  kind: string
  text: string
  url?: string | null
  line?: number | null
}

export interface PickedNode {
  tag: string
  el_id: string | null
  classes: string[]
  attributes: Record<string, string>
  text: string
  outer_html: string
  css_path: string
  xpath: string
  rect: { x: number; y: number; width: number; height: number }
  computed: Record<string, string>
  react: { component?: string; file?: string; line?: number; owners?: string[] } | null
  page_url: string
  screenshot_png_b64?: string | null
  token: string
  shift_key?: boolean
}

export interface SourceHit {
  file: string
  file_id: string
  line: number
  score: number
  snippet: string
}

const json = (body: unknown) => ({
  method: 'POST',
  body: JSON.stringify(body),
})

export const browserApi = {
  status: () => request<BrowserStatus>('/browser/status'),
  start: () => request<BrowserStatus>('/browser/start', { method: 'POST' }),
  stop: () => request<BrowserStatus>('/browser/stop', { method: 'POST' }),
  navigate: (url: string) => request<BrowserStatus>('/browser/navigate', json({ url })),
  newTab: (url?: string) => request<BrowserStatus>('/browser/tabs', json({ url: url || null })),
  activateTab: (index: number) => request<BrowserStatus>(`/browser/tabs/${index}/activate`, { method: 'POST' }),
  closeTab: (index: number) => request<BrowserStatus>(`/browser/tabs/${index}`, { method: 'DELETE' }),
  back: () => request<BrowserStatus>('/browser/back', { method: 'POST' }),
  forward: () => request<BrowserStatus>('/browser/forward', { method: 'POST' }),
  reload: () => request<BrowserStatus>('/browser/reload', { method: 'POST' }),
  stopLoading: () => request<BrowserStatus>('/browser/stop-loading', { method: 'POST' }),
  pickMode: (on: boolean) => request<BrowserStatus>('/browser/pick-mode', json({ on })),
  console: (since = 0) => request<{ entries: ConsoleEntry[] }>(`/browser/console?since=${since}`),
  style: (token: string, css: Record<string, string>) => request<{ ok: boolean }>('/browser/style', json({ token, css })),
  clearStyle: (token: string) => request<{ ok: boolean }>(`/browser/style/${encodeURIComponent(token)}`, { method: 'DELETE' }),
  locate: (sessionId: string, node: PickedNode) =>
    request<{ candidates: SourceHit[] }>('/browser/locate', json({ session_id: sessionId, node })),
}

export function browserSocketUrl(w: number, h: number, dpr: number): string {
  const base = wsUrl('/browser/ws')
  const extra = new URLSearchParams({
    w: String(Math.round(w)),
    h: String(Math.round(h)),
    dpr: String(dpr),
  })
  return `${base}&${extra.toString()}`
}

export async function fetchScreenshot(): Promise<Blob> {
  const headers: Record<string, string> = {}
  const token = getAuthToken()
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(`${API_BASE}/browser/screenshot`, { headers })
  if (!res.ok) throw new Error('Screenshot failed')
  return res.blob()
}

export function missingBrowser(message: string | null | undefined): boolean {
  if (!message) return false
  return /playwright|chromium|not installed|docker-rebuild-local/i.test(message)
}
