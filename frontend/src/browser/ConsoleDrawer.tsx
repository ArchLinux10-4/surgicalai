import { useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { isErrorKind, useBrowserStore } from './browserStore'
import type { ConsoleEntry } from './browserClient'

export function ConsoleDrawer() {
  const open = useBrowserStore((s) => s.drawerOpen)
  const entries = useBrowserStore((s) => s.consoleEntries)
  const displayUrl = useBrowserStore((s) => s.displayUrl)
  const [filter, setFilter] = useState<'all' | 'errors' | 'network'>('all')
  if (!open) return null
  const rows = entries.filter((entry) => {
    if (filter === 'errors') return entry.kind === 'error' || entry.kind === 'pageerror'
    if (filter === 'network') return entry.kind === 'requestfailed' || entry.kind === 'response4xx5xx'
    return true
  })
  const add = (text: string) => {
    useAppStore.getState().appendToComposer(text)
    useAppStore.getState().setBrowserOpen(false)
  }
  return (
    <div className="flex h-56 shrink-0 flex-col border-t border-border bg-surface">
      <div className="flex items-center gap-1 border-b border-border px-2 py-1">
        {(['all', 'errors', 'network'] as const).map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => setFilter(id)}
            className={`rounded-md px-2 py-0.5 text-[11px] font-semibold ${filter === id ? 'bg-overlay text-ink' : 'text-muted hover:text-ink'}`}
          >
            {id === 'all' ? 'All' : id === 'errors' ? 'Errors' : 'Network'}
          </button>
        ))}
        <button
          type="button"
          className="ml-auto text-[11px] text-accent hover:underline"
          onClick={() => add(formatBlock(displayUrl, entries.filter((entry) => isErrorKind(entry.kind))))}
        >
          Add all errors
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-1 font-mono text-[11px]">
        {rows.length === 0 && <p className="py-3 text-muted">No console messages.</p>}
        {rows.map((entry, i) => (
          <div key={`${entry.ts}-${i}`} className="flex items-start gap-2 border-b border-border/40 py-1">
            <span className={isErrorKind(entry.kind) ? 'text-danger' : 'text-muted'}>{entry.kind}</span>
            <span className="min-w-0 flex-1 whitespace-pre-wrap text-ink">{entry.text}</span>
            <button type="button" className="shrink-0 text-accent" onClick={() => add(formatBlock(displayUrl, [entry]))}>Add</button>
          </div>
        ))}
      </div>
    </div>
  )
}

function formatBlock(pageUrl: string, entries: ConsoleEntry[]): string {
  const lines = entries.map((entry) => {
    const where = entry.url ? ` (${entry.url}${entry.line ? ':' + entry.line : ''})` : ''
    return `[${entry.kind}] ${entry.text}${where}`
  })
  return `Console (${pageUrl || 'browser'}):\n${lines.join('\n') || '(none)'}`
}
