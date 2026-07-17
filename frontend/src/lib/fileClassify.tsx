/**
 * Shared file-classification helpers + the segmented file filter control.
 *
 * One source of truth for "current vs new vs edited" so the Session Files
 * side panel, the chat-box file tray, and the mobile panel all stay in sync.
 *
 * - "new"     → AI-created net-new file (origin === 'created')
 * - "current" → user-provided file (uploaded, or an existing file the AI edited)
 * - "edited"  → derived badge: file content was changed after creation
 *
 * The active filter lives in appStore (fileFilter), so every surface that
 * renders <FileFilterTabs/> reflects — and updates — the same state.
 */
import React from 'react'
import type { SessionFile } from '../types'
import { useAppStore } from '../stores/appStore'

export type FileFilter = 'all' | 'current' | 'new' | 'edited'

/** AI-generated net-new file. */
export function isCreatedFile(f: SessionFile): boolean {
  return (f as any).origin === 'created'
}

/** File whose content was changed by the AI pipeline. */
export function isEditedFile(f: SessionFile): boolean {
  return f.origin === 'edited'
}

/** Spreadsheet/CSV file — eligible for the DataLab transform affordance. */
export function isSpreadsheetFile(f: SessionFile): boolean {
  const t = (f as any).file_type
  if (t === 'csv' || t === 'excel') return true
  return /\.(xlsx|xls|csv|tsv)$/i.test(f.filename || '')
}

/** The visual "kind" used for the leading glyph. Edited takes visual priority. */
export type FileKind = 'current' | 'new' | 'edited'
export function fileKind(f: SessionFile): FileKind {
  if (isCreatedFile(f)) return 'new'
  if (isEditedFile(f)) return 'edited'
  return 'current'
}

export function matchesFileFilter(f: SessionFile, filter: FileFilter): boolean {
  if (filter === 'all') return true
  if (filter === 'new') return isCreatedFile(f)
  if (filter === 'edited') return isEditedFile(f)
  return !isCreatedFile(f) // 'current' = everything that isn't AI-created
}

export function filterFiles(files: SessionFile[], filter: FileFilter): SessionFile[] {
  return files.filter(f => matchesFileFilter(f, filter))
}

export function fileCounts(files: SessionFile[]): { all: number; current: number; new: number; edited: number } {
  let created = 0
  let edited = 0
  for (const f of files) {
    if (isCreatedFile(f)) created++
    if (isEditedFile(f)) edited++
  }
  return { all: files.length, new: created, current: files.length - created, edited }
}

/* ── Leading kind glyph ───────────────────────────────────────────────────
   Small inline icon that distinguishes current / new / edited at a glance.
   Uses semantic color tokens so it adapts to both light and dark themes. */
export function FileKindGlyph({ file, size = 16 }: { file: SessionFile; size?: number }) {
  const kind = fileKind(file)
  const common = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }

  if (kind === 'new') {
    // Document + sparkle → freshly created
    return (
      <svg {...common} stroke="currentColor" className="text-purple shrink-0" aria-label="New file">
        <path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z" stroke="currentColor" />
        <path d="M13 3v6h6" stroke="currentColor" />
        <path d="M12 12.5l.8 1.7 1.7.8-1.7.8-.8 1.7-.8-1.7-1.7-.8 1.7-.8z" fill="currentColor" stroke="none" />
      </svg>
    )
  }
  if (kind === 'edited') {
    // Document + pencil → AI-edited
    return (
      <svg {...common} stroke="currentColor" className="text-success shrink-0" aria-label="AI-edited file">
        <path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6" stroke="currentColor" />
        <path d="M13 3v6h6" stroke="currentColor" />
        <path d="M18.5 7.5l2 2L16 14l-2.5.5.5-2.5z" stroke="currentColor" />
      </svg>
    )
  }
  // current / source file
  return (
    <svg {...common} stroke="currentColor" className="text-muted/60 shrink-0" aria-label="Current file">
      <path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z" stroke="currentColor" />
      <path d="M13 3v6h6" stroke="currentColor" />
    </svg>
  )
}

/* ── "New" pill badge ─────────────────────────────────────────────────────
   Distinct from the existing emerald "AI-Edited" badge. */
export function NewBadge({ compact = false }: { compact?: boolean }) {
  return (
    <span className={`flex items-center gap-1 ${compact ? 'text-[9px] px-1.5 py-0.5' : 'text-[10px] px-1.5 py-0.5'} font-semibold bg-purple/10 text-purple border border-purple/25 rounded-md`}>
      <svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
        <path d="M12 2l2.2 5.5L20 9l-5.8 1.5L12 16l-2.2-5.5L4 9l5.8-1.5z" />
      </svg>
      New
    </span>
  )
}

/* ── Segmented Current / New / All filter ─────────────────────────────────
   Bound directly to appStore.fileFilter so all instances stay in sync.
   `counts` drives the badge numbers + auto-hides empty buckets is NOT done
   (we keep all three visible for a stable, predictable control). */
export function FileFilterTabs({
  counts,
  size = 'md',
  className = '',
}: {
  counts: { all: number; current: number; new: number; edited: number }
  size?: 'sm' | 'md'
  className?: string
}) {
  const filter = useAppStore(s => s.fileFilter)
  const setFilter = useAppStore(s => s.setFileFilter)

  const items: { id: FileFilter; label: string; n: number }[] = [
    { id: 'current', label: 'Current', n: counts.current },
    { id: 'new', label: 'New', n: counts.new },
    { id: 'edited', label: 'Edited', n: counts.edited },
    { id: 'all', label: 'All', n: counts.all },
  ]

  const pad = size === 'sm' ? 'px-2 py-0.5 text-[10px]' : 'px-2.5 py-1 text-[11px]'

  return (
    <div className={`inline-flex items-center gap-0.5 p-0.5 rounded-lg bg-overlay/60 border border-border/60 ${className}`} role="tablist">
      {items.map(it => {
        const active = filter === it.id
        return (
          <button
            key={it.id}
            role="tab"
            aria-selected={active}
            onClick={() => setFilter(it.id)}
            className={`flex items-center gap-1.5 rounded-md font-medium transition-colors ${pad} ${
              active
                ? 'bg-accent/15 text-accent shadow-sm'
                : 'text-muted/70 hover:text-ink hover:bg-overlay'
            }`}
          >
            {it.label}
            <span className={`text-[9px] font-semibold px-1 rounded-full ${active ? 'bg-accent/20 text-accent' : 'bg-border/50 text-muted/70'}`}>
              {it.n}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/* ── Per-file diff-stats tracker ──────────────────────────────────────────
   Tracks cumulative added/removed line counts per file, sourced from the
   QA diff card's own line counts at apply-time (recordDiffStats) and
   reversed on undo (revertDiffStats). Persisted to localStorage — mirrors
   the applied/skipped persistence pattern already used for diff cards —
   and broadcasts a DOM event so every mounted badge re-renders instantly. */
export interface DiffStats { added: number; removed: number }

const diffStatsKey = (fileId: string) => `sai-diffstats:${fileId}`
const DIFF_STATS_EVENT = 'sai-diffstats-changed'

export function readDiffStats(fileId: string): DiffStats {
  try {
    const raw = localStorage.getItem(diffStatsKey(fileId))
    if (raw) return JSON.parse(raw)
  } catch {}
  return { added: 0, removed: 0 }
}

function writeDiffStats(fileId: string, stats: DiffStats) {
  try {
    if (stats.added === 0 && stats.removed === 0) {
      localStorage.removeItem(diffStatsKey(fileId))
    } else {
      localStorage.setItem(diffStatsKey(fileId), JSON.stringify(stats))
    }
  } catch {}
  try {
    document.dispatchEvent(new CustomEvent(DIFF_STATS_EVENT, { detail: { fileId } }))
  } catch {}
}

/** Call when a diff card is applied — accumulates added/removed lines for the file. */
export function recordDiffStats(fileId: string, added: number, removed: number) {
  const cur = readDiffStats(fileId)
  writeDiffStats(fileId, { added: cur.added + added, removed: cur.removed + removed })
}

/** Call when an applied diff is undone — reverses the previously recorded stats. */
export function revertDiffStats(fileId: string, added: number, removed: number) {
  const cur = readDiffStats(fileId)
  writeDiffStats(fileId, {
    added: Math.max(0, cur.added - added),
    removed: Math.max(0, cur.removed - removed),
  })
}

/** Live-updating hook so badges re-render immediately as edits are applied/undone. */
export function useDiffStats(fileId: string): DiffStats {
  const [stats, setStats] = React.useState<DiffStats>(() => readDiffStats(fileId))
  React.useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail
      if (!detail || detail.fileId === fileId) setStats(readDiffStats(fileId))
    }
    document.addEventListener(DIFF_STATS_EVENT, handler)
    return () => document.removeEventListener(DIFF_STATS_EVENT, handler)
  }, [fileId])
  return stats
}

/** Compact +N/-N badge shown next to the AI-Edited pill. Renders nothing until stats exist. */
export function DiffStatsBadge({ fileId }: { fileId: string }) {
  const { added, removed } = useDiffStats(fileId)
  if (added === 0 && removed === 0) return null
  return (
    <span className="flex items-center gap-1 text-[9px] font-medium px-1.5 py-0.5 bg-overlay text-muted border border-border rounded-md" title={`+${added} / -${removed} lines`}>
      {added > 0 && <span className="text-success">+{added}</span>}
      {removed > 0 && <span className="text-danger">-{removed}</span>}
    </span>
  )
}
