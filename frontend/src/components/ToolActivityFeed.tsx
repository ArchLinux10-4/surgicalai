import React, { useContext, useEffect, useMemo, useState } from 'react'
import {
  AccountTree,
  Cloud,
  DescriptionOutlined,
  EditNote,
  Extension,
  FactCheck,
  GitHub,
  History,
  Language,
  MoreHoriz,
  Search,
  Terminal,
  Visibility,
} from '@mui/icons-material'
import type { ToolKind, ToolRow } from '../types'
import { clientLog } from '../lib/clientLog'
import {
  deriveStage,
  formatDuration,
  modelFamily,
  summarizeTrail,
} from '../lib/toolActivity'

const NowContext = React.createContext(0)

function useLiveNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [active])
  return active ? now : 0
}

const KIND_CLASS: Record<ToolKind, string> = {
  file: 'text-accent bg-accent/10',
  edit: 'text-accent bg-accent/10',
  search: 'text-success bg-success/10',
  repo: 'text-success bg-success/10',
  github: 'text-purple bg-purple/10',
  web: 'text-accent bg-accent/10',
  qa: 'text-accent bg-accent/10',
  git: 'text-warning bg-warning/10',
  aws: 'text-warning bg-warning/10',
  local: 'text-warning bg-warning/10',
  mcp: 'text-warning bg-warning/10',
  history: 'text-muted bg-overlay',
  preview: 'text-muted bg-overlay',
  step: 'text-muted bg-overlay',
}

const FAMILY_DOT: Record<string, string> = {
  claude: 'bg-purple',
  gpt: 'bg-accent',
  grok: 'bg-success',
  gemini: 'bg-warning',
  local: 'bg-muted',
  unknown: 'bg-accent',
}

function KindIcon({ kind }: { kind: ToolKind }) {
  const props = { sx: { fontSize: 13 }, 'aria-hidden': true as const }
  switch (kind) {
    case 'file': return <DescriptionOutlined {...props} />
    case 'search': return <Search {...props} />
    case 'repo': return <AccountTree {...props} />
    case 'github': return <GitHub {...props} />
    case 'history': return <History {...props} />
    case 'preview': return <Visibility {...props} />
    case 'web': return <Language {...props} />
    case 'git': return <Terminal {...props} />
    case 'aws': return <Cloud {...props} />
    case 'mcp': return <Extension {...props} />
    case 'edit': return <EditNote {...props} />
    case 'qa': return <FactCheck {...props} />
    default: return <MoreHoriz {...props} />
  }
}

function statusWord(status: ToolRow['status']): string {
  if (status === 'waiting') return 'Needs approval'
  if (status === 'done') return 'Done'
  if (status === 'failed') return 'Failed'
  if (status === 'stopped') return 'Stopped'
  return 'Running'
}

function Elapsed({ startedAt }: { startedAt: number }) {
  const now = useContext(NowContext)
  if (!startedAt || !now) return null
  const text = formatDuration(Math.max(0, now - startedAt))
  if (!text) return null
  return <span className="min-w-[3.5rem] text-right text-[10px] tabular-nums text-muted/70" aria-hidden>{text}</span>
}

function settledDuration(row: ToolRow): string {
  if (typeof row.ms === 'number' && Number.isFinite(row.ms)) return formatDuration(Math.max(0, row.ms))
  if (row.endedAt && row.startedAt > 0) return formatDuration(Math.max(0, row.endedAt - row.startedAt))
  return ''
}

const ActivityRow = React.memo(function ActivityRow({
  row,
  compact,
  expanded,
  onToggle,
}: {
  row: ToolRow
  compact: boolean
  expanded: boolean
  onToggle: () => void
}) {
  const live = row.status === 'running' || row.status === 'waiting'
  const label = live ? row.label : (row.doneLabel || row.label)
  const details = !!(row.summary || row.preview)
  const preview = (row.preview || '').split('\n').slice(0, 4).join('\n')
  const duration = live ? '' : settledDuration(row)
  const previewId = `tool-preview-${row.id}`
  const body = (
    <>
      <span className={`w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0 ${KIND_CLASS[row.kind] || KIND_CLASS.step}`}>
        <KindIcon kind={row.kind} />
      </span>
      <span className="min-w-0 flex-1 truncate text-[12px] text-ink/85">{label}</span>
      {!compact && row.status === 'waiting' && (
        <span className="min-w-0 flex-1 truncate text-[11px] text-warning" title={row.target || 'Needs approval'}>Needs approval</span>
      )}
      {!compact && row.status !== 'waiting' && row.target && (
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted" title={row.target}>{row.target}</span>
      )}
      {live ? <Elapsed startedAt={row.startedAt} /> : (
        <span className="min-w-[3.5rem] text-right text-[10px] tabular-nums text-muted/70" aria-hidden>{duration}</span>
      )}
      <span className="w-4 h-4 flex items-center justify-center flex-shrink-0" title={statusWord(row.status)}>
        {row.status === 'running' && (
          <span className="w-3.5 h-3.5 rounded-full border-2 border-accent/30 border-t-accent motion-safe:animate-spin" />
        )}
        {row.status === 'waiting' && (
          <span className="w-2 h-2 rounded-full bg-warning motion-safe:animate-pulse" />
        )}
        {row.status === 'done' && <span className="text-success text-[12px] leading-none">✓</span>}
        {row.status === 'failed' && <span className="text-danger text-[12px] leading-none">✕</span>}
        {row.status === 'stopped' && <span className="text-muted text-[10px] leading-none">–</span>}
        <span className="sr-only">{statusWord(row.status)}</span>
      </span>
    </>
  )
  return (
    <div className="border-t border-border/30 motion-safe:animate-[toolRowIn_160ms_ease-out]">
      {details ? (
        <button
          type="button"
          className={`w-full flex items-center gap-2 px-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 ${compact ? 'min-h-9' : 'h-7'}`}
          aria-expanded={expanded}
          aria-controls={previewId}
          onClick={onToggle}
        >
          {body}
        </button>
      ) : (
        <div className={`flex items-center gap-2 px-3 ${compact ? 'min-h-9' : 'h-7'}`} role="listitem">
          {body}
        </div>
      )}
      {compact && (row.target || row.status === 'waiting') && (
        <div className={`px-3 pb-1 pl-10 text-[11px] truncate ${row.status === 'waiting' ? 'text-warning' : 'font-mono text-muted'}`} title={row.target || 'Needs approval'}>
          {row.status === 'waiting' ? 'Needs approval' : row.target}
        </div>
      )}
      {expanded && details && (
        <div id={previewId} className="mx-3 mb-2 rounded-lg bg-overlay/60 border border-border/40 px-2.5 py-2">
          {row.summary && <div className="text-[12px] text-ink/85">{row.summary}</div>}
          {preview && (
            <pre className="mt-1 max-h-32 overflow-auto overscroll-contain whitespace-pre-wrap break-all font-mono text-[11px] text-muted">{preview}</pre>
          )}
        </div>
      )}
    </div>
  )
}, (prev, next) => prev.row === next.row && prev.compact === next.compact && prev.expanded === next.expanded)

function partitionRows(rows: ToolRow[], content: string, pinned: Set<string>, earlierOpen: boolean) {
  if (earlierOpen || !content.trim() || rows.length <= 3) return { earlier: [] as ToolRow[], shown: rows }
  const keep = new Set<number>()
  rows.forEach((row, index) => {
    if (row.status === 'running' || row.status === 'waiting' || pinned.has(row.id)) keep.add(index)
  })
  const finished = rows.map((_, index) => index).filter(index => !keep.has(index))
  for (const index of finished.slice(-2)) keep.add(index)
  const earlier: ToolRow[] = []
  const shown: ToolRow[] = []
  rows.forEach((row, index) => (keep.has(index) ? shown : earlier).push(row))
  return earlier.length ? { earlier, shown } : { earlier: [], shown: rows }
}

interface FeedProps {
  mode: 'live' | 'done'
  rows: ToolRow[]
  startedAt?: number
  model?: string
  content?: string
  progress?: string
  phaseLabel?: string
  isThinking?: boolean
  isBuildingEdit?: boolean
  compact?: boolean
  max?: number
}

function ToolActivityFeedInner({
  mode, rows, startedAt, model, content, progress, phaseLabel, isThinking, isBuildingEdit, compact, max,
}: FeedProps) {
  const live = mode === 'live'
  const [open, setOpen] = useState(false)
  const [earlierOpen, setEarlierOpen] = useState(false)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const source = useMemo(() => {
    if (max && rows.length > max) return rows.slice(rows.length - max)
    return rows
  }, [rows, max])

  const pinned = useMemo(() => {
    const ids = new Set<string>()
    for (const [id, value] of Object.entries(expanded)) if (value) ids.add(id)
    return ids
  }, [expanded])

  if (!live && rows.length === 0) return null
  if (compact && max && source.length === 0) return null

  const family = modelFamily(model)
  const halted = source.length > 0
    && source.some(row => row.status === 'stopped')
    && !source.some(row => row.status === 'running' || row.status === 'waiting')
  const stage = halted && !(content || '').trim()
    ? { label: 'Stopped', tone: 'think' as const }
    : (live && rows.length === 0 && phaseLabel)
      ? { label: phaseLabel, tone: 'work' as const }
      : deriveStage({
        rows: source,
        content,
        progress,
        isThinking,
        isBuildingEdit,
      })
  const { earlier, shown } = live
    ? partitionRows(source, content || '', pinned, earlierOpen)
    : { earlier: [] as ToolRow[], shown: source }

  const list = (
    <div role="list">
      {earlier.length > 0 && (
        <button
          type="button"
          className="h-7 w-full px-3 text-left text-[11px] text-muted hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          onClick={() => setEarlierOpen(true)}
        >
          {earlier.length} earlier step{earlier.length === 1 ? '' : 's'}
        </button>
      )}
      {shown.map(row => {
        const failedOpen = row.status === 'failed' && !!(row.summary || row.preview)
        const isOpen = (expanded[row.id] ?? failedOpen) && row.id !== '__omitted__'
        return (
          <ActivityRow
            key={row.id}
            row={row}
            compact={!!compact}
            expanded={isOpen}
            onToggle={() => setExpanded(prev => ({ ...prev, [row.id]: !isOpen }))}
          />
        )
      })}
    </div>
  )

  return (
      <div className="mb-2 rounded-lg border border-border/50 bg-overlay/30 overflow-hidden">
        {live ? (
          <div className="h-8 px-3 flex items-center gap-2 min-w-0">
            <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${FAMILY_DOT[family.family]}`} aria-hidden />
            <span className="text-[12px] font-medium text-ink/90 flex-shrink-0">{family.name}</span>
            <span className="text-muted/40" aria-hidden>·</span>
            <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-ink/90" aria-live="polite">{stage.label}</span>
            {halted ? (
              <span className="text-[10px] text-muted flex-shrink-0">Stopped</span>
            ) : startedAt ? (
              <span aria-hidden><Elapsed startedAt={startedAt} /></span>
            ) : null}
          </div>
        ) : (
          <button
            type="button"
            className="h-8 w-full px-3 flex items-center gap-2 text-left text-[12px] font-medium text-ink/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
            aria-expanded={open}
            onClick={() => setOpen(value => !value)}
          >
            <span aria-hidden>{open ? '▾' : '▸'}</span>
            <span className="truncate">{summarizeTrail(rows)}</span>
          </button>
        )}
        {(live || open) && list}
      </div>
  )
}

function LiveClock({ active, children }: { active: boolean; children: React.ReactNode }) {
  const now = useLiveNow(active)
  return <NowContext.Provider value={now}>{children}</NowContext.Provider>
}

class FeedErrorBoundary extends React.Component<{ children: React.ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  componentDidCatch(error: Error) {
    clientLog('tool_feed_render_error', { message: (error?.message || 'render error').slice(0, 200) })
  }
  render() {
    if (this.state.failed) return null
    return this.props.children
  }
}

export function ToolActivityFeed(props: FeedProps) {
  return (
    <FeedErrorBoundary>
      <LiveClock active={props.mode === 'live'}>
        <ToolActivityFeedInner {...props} />
      </LiveClock>
    </FeedErrorBoundary>
  )
}
