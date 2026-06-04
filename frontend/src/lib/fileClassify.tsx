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

export type FileFilter = 'all' | 'current' | 'new'

/** AI-generated net-new file. */
export function isCreatedFile(f: SessionFile): boolean {
  return (f as any).origin === 'created'
}

/**
 * File the AI actually edited. Driven by the backend `edited` flag, which is
 * true only while an applied change is in effect — it is set by the apply
 * write-back and cleared on undo. This avoids false positives for files whose
 * `updated_at` merely bumped during upload/symbol-extraction, and guarantees
 * the badge disappears the moment an edit is undone. (`origin` kept as a
 * defensive fallback for transform-produced files.)
 */
export function isEditedFile(f: SessionFile): boolean {
  return (f as any).edited === true || (f as any).origin === 'edited'
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
  return !isCreatedFile(f) // 'current' = everything that isn't AI-created
}

export function filterFiles(files: SessionFile[], filter: FileFilter): SessionFile[] {
  return files.filter(f => matchesFileFilter(f, filter))
}

export function fileCounts(files: SessionFile[]): { all: number; current: number; new: number } {
  let created = 0
  for (const f of files) if (isCreatedFile(f)) created++
  return { all: files.length, new: created, current: files.length - created }
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
  counts: { all: number; current: number; new: number }
  size?: 'sm' | 'md'
  className?: string
}) {
  const filter = useAppStore(s => s.fileFilter)
  const setFilter = useAppStore(s => s.setFileFilter)

  const items: { id: FileFilter; label: string; n: number }[] = [
    { id: 'current', label: 'Current', n: counts.current },
    { id: 'new', label: 'New', n: counts.new },
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
