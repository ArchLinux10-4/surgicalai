import { create } from 'zustand'
import type { ConsoleEntry, PickedNode, SourceHit, TabInfo } from './browserClient'

export interface HoverBox {
  rect: { x: number; y: number; width: number; height: number }
  tag: string
}

export interface InspectorState {
  node: PickedNode
  edits: Record<string, string>
  source: SourceHit[]
}

interface BrowserState {
  connected: boolean
  displayUrl: string
  title: string
  favicon: string
  loading: boolean
  canGoBack: boolean
  canGoForward: boolean
  pickMode: boolean
  cursor: string
  hover: HoverBox | null
  consoleEntries: ConsoleEntry[]
  unreadConsoleErrors: number
  drawerOpen: boolean
  inspectorOpen: boolean
  inspector: InspectorState | null
  lastError: string | null
  navError: string | null
  starting: boolean
  sessionUsed: boolean
  lastPick: PickedNode | null
  presetId: string
  attachShot: boolean
  reloadAfterApply: boolean
  focusAddress: number
  viewport: { w: number; h: number }
  tabs: TabInfo[]
  activeTab: number
  setPreset: (id: string) => void
  setDrawerOpen: (open: boolean) => void
  setInspectorOpen: (open: boolean) => void
  setAttachShot: (on: boolean) => void
  setReloadAfterApply: (on: boolean) => void
  requestAddressFocus: () => void
  rememberUrl: (sessionId: string | null, url: string) => void
  lastUrl: (sessionId: string | null) => string
}

function readFlag(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    if (v == null) return fallback
    return v !== '0'
  } catch {
    return fallback
  }
}

const ERROR_KINDS = new Set(['error', 'pageerror', 'requestfailed', 'response4xx5xx'])

export const useBrowserStore = create<BrowserState>((set, get) => ({
  connected: false,
  displayUrl: '',
  title: '',
  favicon: '',
  loading: false,
  canGoBack: false,
  canGoForward: false,
  pickMode: false,
  cursor: 'auto',
  hover: null,
  consoleEntries: [],
  unreadConsoleErrors: 0,
  drawerOpen: false,
  inspectorOpen: false,
  inspector: null,
  lastError: null,
  navError: null,
  starting: false,
  sessionUsed: false,
  lastPick: null,
  presetId: 'responsive',
  attachShot: readFlag('surgicalai.browser.attachShot', true),
  reloadAfterApply: readFlag('surgicalai.browser.reloadAfterApply', true),
  focusAddress: 0,
  viewport: { w: 1280, h: 800 },
  tabs: [],
  activeTab: 0,
  setPreset: (presetId) => set({ presetId }),
  setDrawerOpen: (drawerOpen) => set({
    drawerOpen,
    unreadConsoleErrors: drawerOpen ? 0 : get().unreadConsoleErrors,
  }),
  setInspectorOpen: (inspectorOpen) => set({ inspectorOpen }),
  setAttachShot: (attachShot) => {
    try { localStorage.setItem('surgicalai.browser.attachShot', attachShot ? '1' : '0') } catch { /* ignore */ }
    set({ attachShot })
  },
  setReloadAfterApply: (reloadAfterApply) => {
    try { localStorage.setItem('surgicalai.browser.reloadAfterApply', reloadAfterApply ? '1' : '0') } catch { /* ignore */ }
    set({ reloadAfterApply })
  },
  requestAddressFocus: () => set((s) => ({ focusAddress: s.focusAddress + 1 })),
  rememberUrl: (sessionId, url) => {
    if (!sessionId || !url) return
    try { localStorage.setItem(`surgicalai.browser.lastUrl.${sessionId}`, url) } catch { /* ignore */ }
  },
  lastUrl: (sessionId) => {
    if (!sessionId) return ''
    try { return localStorage.getItem(`surgicalai.browser.lastUrl.${sessionId}`) || '' } catch { return '' }
  },
}))

export function pushConsole(entry: ConsoleEntry) {
  const state = useBrowserStore.getState()
  const unread = !state.drawerOpen && ERROR_KINDS.has(entry.kind)
    ? state.unreadConsoleErrors + 1
    : state.unreadConsoleErrors
  useBrowserStore.setState({
    consoleEntries: [...state.consoleEntries, entry].slice(-500),
    unreadConsoleErrors: unread,
  })
}

export function isErrorKind(kind: string): boolean {
  return ERROR_KINDS.has(kind)
}
