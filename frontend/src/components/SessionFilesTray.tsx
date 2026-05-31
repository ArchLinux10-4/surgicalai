import React, { useState, useMemo, useEffect } from 'react'
import { api } from '../api/client'
import type { SessionFile } from '../types'
import { GitHubCommitModal } from './GitHubCommitModal'
import { useAppStore } from '../stores/appStore'
import { FileFilterTabs, NewBadge, FileKindGlyph, matchesFileFilter, fileCounts, isCreatedFile } from '../lib/fileClassify'
import { GitHub } from '@mui/icons-material';

interface SessionFilesTrayProps {
  sessionId: string
  sessionFiles: SessionFile[]
  /** Opens the file picker. When omitted, the "Add" affordances are hidden. */
  onAddFiles?: () => void
  /** Removes a file. When omitted, the per-row × and "Clear all" are hidden. */
  onRemove?: (fileId: string) => void
}

function relativeTime(dateStr?: string): string {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffSec = Math.floor(diffMs / 1000)
  if (diffSec < 60) return 'just now'
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  const diffDay = Math.floor(diffHr / 24)
  return `${diffDay}d ago`
}

// Theme-safe language badge classes (semantic tokens — adapt to light + dark)
function langColor(lang: string): string {
  const map: Record<string, string> = {
    typescript: 'bg-accent/15 text-accent border-accent/25',
    javascript: 'bg-warning/15 text-warning border-warning/25',
    python: 'bg-success/15 text-success border-success/25',
    rust: 'bg-orange/15 text-orange border-orange/25',
    go: 'bg-accent/15 text-accent border-accent/25',
    html: 'bg-danger/15 text-danger border-danger/25',
    css: 'bg-purple/15 text-purple border-purple/25',
    json: 'bg-overlay text-muted border-border',
    markdown: 'bg-overlay text-muted border-border',
  }
  return map[lang?.toLowerCase()] ?? 'bg-overlay text-muted border-border'
}

// sync status: 'synced' | 'modified' | 'never'
function syncStatus(file: SessionFile): 'synced' | 'modified' | 'never' {
  if (!file.github_pushed_at) return 'never'
  const pushed = new Date(file.github_pushed_at).getTime()
  const updated = file.updated_at ? new Date(file.updated_at).getTime() : 0
  // Give 2s grace period to avoid race conditions
  return updated > pushed + 2000 ? 'modified' : 'synced'
}

export function SessionFilesTray({ sessionId, sessionFiles, onAddFiles, onRemove }: SessionFilesTrayProps) {
  // Collapsed by default — the drawer is a calm one-line bar until opened.
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [downloading, setDownloading] = useState<string | null>(null)
  const [showCommitModal, setShowCommitModal] = useState(false)
  const [appliedIds, setAppliedIds] = useState<Set<string>>(new Set())
  const { setSessionFiles } = useAppStore()
  const fileFilter = useAppStore(s => s.fileFilter)
  const counts = useMemo(() => fileCounts(sessionFiles), [sessionFiles])

  // Load applied change IDs so the AI-edited badge stays accurate after undo.
  useEffect(() => {
    if (!sessionId) return
    api.surgical.getApplied(sessionId)
      .then(({ applied_ids }) => setAppliedIds(new Set(applied_ids)))
      .catch(() => {}) // fall back to updated_at heuristic silently
  }, [sessionId, sessionFiles])

  const isFileEdited = (file: SessionFile): boolean => {
    const heuristic = !!(file.updated_at && file.updated_at !== file.created_at)
    if (appliedIds.size === 0) return heuristic
    return heuristic && appliedIds.size > 0
  }

  // Sort: modified-since-push first, then synced, then never; AI-edited before unedited.
  const sortedFiles = useMemo(() => {
    const order = { modified: 0, synced: 1, never: 2 }
    return [...sessionFiles].sort((a, b) => {
      const so = order[syncStatus(a)] - order[syncStatus(b)]
      if (so !== 0) return so
      const aEdited = a.updated_at && a.updated_at !== a.created_at ? 0 : 1
      const bEdited = b.updated_at && b.updated_at !== b.created_at ? 0 : 1
      return aEdited - bEdited
    })
  }, [sessionFiles])

  const visibleFiles = useMemo(() => {
    const q = query.trim().toLowerCase()
    return sortedFiles.filter(f =>
      matchesFileFilter(f, fileFilter) &&
      (!q || f.filename.toLowerCase().includes(q))
    )
  }, [sortedFiles, query, fileFilter])

  const syncCounts = useMemo(() => {
    const counts = { modified: 0, synced: 0, never: 0 }
    for (const f of sessionFiles) counts[syncStatus(f)]++
    return counts
  }, [sessionFiles])

  const totalLines = useMemo(
    () => sessionFiles.reduce((sum, f) => sum + (f.lines || 0), 0),
    [sessionFiles],
  )

  const hasGithubFiles = sessionFiles.some(f => f.github_meta)
  const hasOutOfSync = syncCounts.modified > 0
  const aiEditedCount = sessionFiles.filter(f => isFileEdited(f)).length

  const handleDownload = async (file: SessionFile) => {
    setDownloading(file.id)
    try {
      await api.sessionFiles.download(sessionId, file.id, file.filename)
    } catch {
      // silently fail
    } finally {
      setDownloading(null)
    }
  }

  const handleClearAll = () => {
    if (!onRemove) return
    if (!window.confirm(`Remove all ${sessionFiles.length} files from this session?`)) return
    sessionFiles.forEach(f => onRemove(f.id))
  }

  if (sessionFiles.length === 0) return null

  const fileWord = sessionFiles.length === 1 ? 'file' : 'files'

  return (
    <>
      <div className="border border-border/70 rounded-xl bg-surface/60 overflow-hidden mb-2.5 backdrop-blur-sm">
        {/* ── Collapsed bar (always visible) ─────────────────────── */}
        <div className="w-full flex items-center justify-between px-3.5 py-2.5">
          <button
            onClick={() => setOpen(o => !o)}
            className="flex items-center gap-2 min-w-0 flex-1 text-left select-none group"
          >
            <svg
              className={`w-3 h-3 text-muted/60 shrink-0 transition-transform ${open ? 'rotate-90' : ''}`}
              fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}
            >
              <path d="M9 5l7 7-7 7" />
            </svg>
            <svg className="w-3.5 h-3.5 text-muted/70 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path d="M3 7a2 2 0 012-2h3l2 2h9a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
            </svg>
            <span className="text-[12px] font-semibold text-ink truncate">
              {sessionFiles.length} {fileWord}
            </span>
            <span className="text-[11px] text-muted/60 truncate">
              · {totalLines.toLocaleString()} lines in context
            </span>
            {aiEditedCount > 0 && (
              <span className="text-[10px] font-medium px-1.5 py-0.5 bg-success/10 text-success border border-success/20 rounded-full shrink-0">
                {aiEditedCount} AI-edited
              </span>
            )}
            {hasOutOfSync && (
              <span className="text-[10px] font-semibold text-warning shrink-0">
                {syncCounts.modified} modified
              </span>
            )}
          </button>

          <div className="flex items-center gap-2 shrink-0 pl-2">
            {hasGithubFiles && (
              <button
                onClick={() => setShowCommitModal(true)}
                className={`flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-semibold border transition-all ${
                  hasOutOfSync
                    ? 'bg-warning/20 hover:bg-warning/30 text-warning border-warning/40 animate-pulse'
                    : 'bg-overlay hover:bg-overlay text-ink border-border'
                }`}
                title={hasOutOfSync ? 'You have unpushed changes' : 'Push changes to GitHub'}
              >
                <GitHub sx={{ fontSize: 10 }} />
                {hasOutOfSync ? 'Push ↑' : 'Push'}
              </button>
            )}
            {onAddFiles && (
              <button
                onClick={onAddFiles}
                className="flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-medium text-accent hover:bg-accent/10 transition-colors"
                title="Add files"
              >
                + Add
              </button>
            )}
          </div>
        </div>

        {/* ── Expanded panel ─────────────────────────────────────── */}
        {open && (
          <div className="border-t border-border/50">
            {/* Current / New / All segmented filter — synced with the side panel */}
            <div className="px-3 py-2 border-b border-border/40">
              <FileFilterTabs counts={counts} size="sm" />
            </div>
            {/* Search filter */}
            {sessionFiles.length > 4 && (
              <div className="px-3 py-2 border-b border-border/40">
                <div className="flex items-center gap-2 px-2.5 py-1.5 bg-base/60 border border-border/60 rounded-lg">
                  <svg className="w-3.5 h-3.5 text-muted/50 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path d="M21 21l-4.35-4.35M11 18a7 7 0 100-14 7 7 0 000 14z" />
                  </svg>
                  <input
                    value={query}
                    onChange={e => setQuery(e.target.value)}
                    placeholder="Filter files…"
                    className="flex-1 bg-transparent text-[12px] text-ink placeholder:text-muted/50 outline-none"
                  />
                  {query && (
                    <button onClick={() => setQuery('')} className="text-muted/50 hover:text-ink text-[12px] shrink-0">✕</button>
                  )}
                </div>
              </div>
            )}

            {/* File list — height-capped + scrollable so the chat never moves */}
            <div
              className="divide-y divide-border/40 overflow-y-auto max-h-72"
              style={{ scrollbarWidth: 'thin', scrollbarColor: 'rgba(120,120,140,0.3) transparent' }}
            >
              {visibleFiles.length === 0 && (
                <div className="px-3.5 py-4 text-center text-[12px] text-muted/60">No files match “{query}”.</div>
              )}
              {visibleFiles.map(file => {
                const status = syncStatus(file)
                const isModified = isFileEdited(file)
                const timestamp = isModified ? file.updated_at : file.created_at
                const isDownloading = downloading === file.id

                return (
                  <div
                    key={file.id}
                    className="flex items-center gap-3 px-3.5 py-2 hover:bg-overlay/40 transition-colors group"
                  >
                    <FileKindGlyph file={file} size={16} />

                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="text-[13px] font-medium text-ink truncate max-w-[200px]" title={file.filename}>
                          {file.filename}
                        </span>
                        {isCreatedFile(file) && <NewBadge compact />}
                        <span className={`text-[9px] font-medium px-1.5 py-0.5 border rounded-md ${langColor(file.language)}`}>
                          {file.language || 'text'}
                        </span>
                        {file.github_meta && status === 'synced' && (
                          <span className="flex items-center gap-1 text-[9px] font-medium px-1.5 py-0.5 bg-accent/10 text-accent border border-accent/20 rounded-md">
                            <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block" />
                            Synced · {relativeTime(file.github_pushed_at)}
                          </span>
                        )}
                        {file.github_meta && status === 'modified' && (
                          <span className="flex items-center gap-1 text-[9px] font-semibold px-1.5 py-0.5 bg-warning/10 text-warning border border-warning/30 rounded-md">
                            <span className="w-1.5 h-1.5 rounded-full bg-warning animate-pulse inline-block" />
                            Modified · not pushed
                          </span>
                        )}
                        {isModified && (
                          <span className="flex items-center gap-1 text-[9px] font-medium px-1.5 py-0.5 bg-success/10 text-success border border-success/20 rounded-md">
                            <span className="w-1.5 h-1.5 rounded-full bg-success inline-block" />
                            AI-Edited
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className="text-[11px] text-muted/60">{file.lines?.toLocaleString()} lines</span>
                        {file.symbol_count > 0 && (
                          <span className="text-[11px] text-muted/40">· {file.symbol_count} symbols</span>
                        )}
                        <span className="text-[11px] text-muted/40">·</span>
                        <span className="text-[11px] text-muted/60" title={timestamp}>
                          {isModified ? 'Last edit ' : 'Uploaded '}{relativeTime(timestamp)}
                        </span>
                      </div>
                    </div>

                    {/* Download */}
                    <button
                      onClick={() => handleDownload(file)}
                      disabled={isDownloading}
                      className="shrink-0 flex items-center justify-center w-7 h-7 rounded-lg text-muted
                        hover:bg-accent/10 hover:text-accent disabled:opacity-50 disabled:cursor-not-allowed
                        opacity-0 group-hover:opacity-100 transition-all"
                      title={`Download ${file.filename}`}
                    >
                      {isDownloading ? (
                        <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" />
                          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                        </svg>
                      ) : (
                        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                        </svg>
                      )}
                    </button>

                    {/* Remove */}
                    {onRemove && (
                      <button
                        onClick={() => onRemove(file.id)}
                        className="shrink-0 flex items-center justify-center w-7 h-7 rounded-lg text-muted/60
                          hover:bg-danger/10 hover:text-danger opacity-0 group-hover:opacity-100 transition-all"
                        title={`Remove ${file.filename}`}
                      >
                        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                    )}
                  </div>
                )
              })}
            </div>

            {/* Footer */}
            {(onAddFiles || onRemove) && (
              <div className="flex items-center justify-between px-3.5 py-2 border-t border-border/40 bg-base/30">
                {onAddFiles ? (
                  <button onClick={onAddFiles} className="text-[11px] font-medium text-accent hover:underline">
                    + Add files
                  </button>
                ) : <span />}
                {onRemove && (
                  <button onClick={handleClearAll} className="text-[11px] text-muted/60 hover:text-danger transition-colors">
                    Clear all
                  </button>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {showCommitModal && (
        <GitHubCommitModal
          sessionFiles={sessionFiles}
          onClose={() => setShowCommitModal(false)}
          onSuccess={() => {
            setShowCommitModal(false)
            api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
          }}
        />
      )}
    </>
  )
}
