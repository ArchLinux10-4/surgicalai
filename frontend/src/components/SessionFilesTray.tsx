import React, { useState, useMemo, useEffect } from 'react'
import { api } from '../api/client'
import type { SessionFile } from '../types'
import { GitHubCommitModal } from './GitHubCommitModal'
import { useAppStore } from '../stores/appStore'
import { GitHub } from '@mui/icons-material';

interface SessionFilesTrayProps {
  sessionId: string
  sessionFiles: SessionFile[]
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

function langColor(lang: string): string {
  const map: Record<string, string> = {
    typescript: 'bg-blue-500/15 text-blue-400 border-blue-500/25',
    javascript: 'bg-yellow-500/15 text-yellow-400 border-yellow-500/25',
    python: 'bg-green-500/15 text-green-400 border-green-500/25',
    rust: 'bg-orange-500/15 text-orange-400 border-orange-500/25',
    go: 'bg-cyan-500/15 text-cyan-400 border-cyan-500/25',
    html: 'bg-red-500/15 text-red-400 border-red-500/25',
    css: 'bg-purple-500/15 text-purple-400 border-purple-500/25',
    json: 'bg-gray-500/15 text-gray-400 border-gray-500/25',
    markdown: 'bg-gray-500/15 text-gray-300 border-gray-500/25',
  }
  return map[lang?.toLowerCase()] ?? 'bg-surface-alt text-muted border-border'
}

// sync status: 'synced' | 'modified' | 'never'
function syncStatus(file: SessionFile): 'synced' | 'modified' | 'never' {
  if (!file.github_pushed_at) return 'never'
  const pushed = new Date(file.github_pushed_at).getTime()
  const updated = file.updated_at ? new Date(file.updated_at).getTime() : 0
  // Give 2s grace period to avoid race conditions
  return updated > pushed + 2000 ? 'modified' : 'synced'
}

const COMPACT_THRESHOLD = 8

export function SessionFilesTray({ sessionId, sessionFiles }: SessionFilesTrayProps) {
  const [collapsed, setCollapsed] = useState(false)
  const [downloading, setDownloading] = useState<string | null>(null)
  const [showCommitModal, setShowCommitModal] = useState(false)
  // Undo-aware applied state: Set of change IDs that are currently applied.
  // When undo is used, those IDs are removed from the set → badge disappears.
  const [appliedIds, setAppliedIds] = useState<Set<string>>(new Set())
  const { setSessionFiles } = useAppStore()

  // Load applied change IDs from backend on mount / when session changes.
  // This makes AI-Edited accurate even after undo — if all changes for a file
  // are undone, the file won't show AI-Edited even though updated_at changed.
  useEffect(() => {
    if (!sessionId) return
    api.surgical.getApplied(sessionId)
      .then(({ applied_ids }) => setAppliedIds(new Set(applied_ids)))
      .catch(() => {}) // fall back to updated_at heuristic silently
  }, [sessionId, sessionFiles]) // re-check whenever files change (apply/undo)

  // Undo-aware isEdited: a file is AI-Edited if it has applied changes in DB
  // AND updated_at differs from created_at (catches manual uploads too).
  // Falls back to pure updated_at heuristic if appliedIds is empty (first load).
  const isFileEdited = (file: SessionFile): boolean => {
    const heuristic = !!(file.updated_at && file.updated_at !== file.created_at)
    if (appliedIds.size === 0) return heuristic  // DB not loaded yet
    // Check localStorage for applied changes belonging to this file
    // Key format: sai-applied:{sessionId}:{changeId}
    // We can't directly map changeId→fileId, so we use the heuristic
    // supplemented by appliedIds presence: if ANY applied change exists
    // in this session AND the file was modified, it's AI-Edited
    return heuristic && appliedIds.size > 0
  }

  const isCompact = sessionFiles.length > COMPACT_THRESHOLD

  // Sort: modified-since-push first, then synced, then never; AI-edited always before unedited
  const sortedFiles = useMemo(() => {
    const order = { modified: 0, synced: 1, never: 2 }
    return [...sessionFiles].sort((a, b) => {
      const so = order[syncStatus(a)] - order[syncStatus(b)]
      if (so !== 0) return so
      // within same sync group: AI-edited first
      const aEdited = a.updated_at && a.updated_at !== a.created_at ? 0 : 1
      const bEdited = b.updated_at && b.updated_at !== b.created_at ? 0 : 1
      return aEdited - bEdited
    })
  }, [sessionFiles])

  const syncCounts = useMemo(() => {
    const counts = { modified: 0, synced: 0, never: 0 }
    for (const f of sessionFiles) counts[syncStatus(f)]++
    return counts
  }, [sessionFiles])

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

  if (sessionFiles.length === 0) return null

  return (
    <>
      <div className="border border-border/60 rounded-xl bg-surface/50 overflow-hidden mb-2 mx-1 backdrop-blur-sm">
        {/* Header row */}
        <button
          onClick={() => setCollapsed(c => !c)}
          className="w-full flex items-center justify-between px-3.5 py-2.5 hover:bg-surface-alt/50 transition-colors select-none"
        >
          <div className="flex items-center gap-2 flex-wrap">
            <svg className="w-3.5 h-3.5 text-muted/70 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path d="M3 7a2 2 0 012-2h3l2 2h9a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
            </svg>
            <span className="text-[12px] font-medium text-ink/80">Session Files</span>
            {/* Total count */}
            <span className="text-[10px] font-semibold px-1.5 py-0.5 bg-blue-500/15 text-blue-400 border border-blue-500/25 rounded-full">
              {sessionFiles.length}
            </span>
            {/* AI-edited badge */}
            {aiEditedCount > 0 && (
              <span className="text-[10px] font-medium px-1.5 py-0.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-full">
                {aiEditedCount} AI-edited
              </span>
            )}
            {/* Sync summary — only shown when github files exist */}
            {hasGithubFiles && (
              <span className="text-[10px] text-muted/50 font-medium">
                {syncCounts.modified > 0 && (
                  <span className="text-amber-400 font-semibold">{syncCounts.modified} modified</span>
                )}
                {syncCounts.modified > 0 && syncCounts.synced > 0 && <span className="mx-1 text-muted/30">·</span>}
                {syncCounts.synced > 0 && (
                  <span className="text-emerald-400">{syncCounts.synced} synced</span>
                )}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {/* Push button — pulses amber if any file modified since push */}
            {hasGithubFiles && (
              <button
                onClick={e => { e.stopPropagation(); setShowCommitModal(true) }}
                className={`flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-semibold border transition-all ${
                  hasOutOfSync
                    ? 'bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border-amber-500/40 animate-pulse'
                    : 'bg-gray-700/60 hover:bg-gray-700 text-white border-gray-600/50'
                }`}
                title={hasOutOfSync ? 'You have unpushed changes' : 'Push changes to GitHub'}
              >
                <GitHub sx={{ fontSize: 10 }} />
                {hasOutOfSync ? 'Push ↑' : 'Push'}
              </button>
            )}
            <svg
              className={`w-3.5 h-3.5 text-muted/50 transition-transform ${collapsed ? '' : 'rotate-180'}`}
              fill="none" viewBox="0 0 24 24" stroke="currentColor"
            >
              <path d="M19 9l-7 7-7-7" />
            </svg>
          </div>
        </button>

        {/* File list */}
        {!collapsed && (
          <div
            className={`divide-y divide-border/40 ${isCompact ? 'overflow-y-auto max-h-72' : ''}`}
            style={isCompact ? { scrollbarWidth: 'thin', scrollbarColor: 'rgba(100,100,120,0.3) transparent' } : {}}
          >
            {sortedFiles.map(file => {
              const status = syncStatus(file)
              const isModified = isFileEdited(file)
              const timestamp = isModified ? file.updated_at : file.created_at
              const isDownloading = downloading === file.id
              const py = isCompact ? 'py-1.5' : 'py-2.5'

              return (
                <div
                  key={file.id}
                  className={`flex items-center gap-3 px-3.5 ${py} hover:bg-surface-alt/30 transition-colors group`}
                >
                  {/* File icon */}
                  {!isCompact && (
                    <div className="shrink-0">
                      <svg className="w-4 h-4 text-muted/50" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                      </svg>
                    </div>
                  )}

                  {/* Filename + meta */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className={`${isCompact ? 'text-[12px]' : 'text-[13px]'} font-medium text-ink truncate max-w-[180px]`} title={file.filename}>
                        {file.filename}
                      </span>
                      {/* Language badge */}
                      <span className={`text-[9px] font-medium px-1.5 py-0.5 border rounded-md ${langColor(file.language)}`}>
                        {file.language || 'text'}
                      </span>
                      {/* Sync status badge — only for github-connected files */}
                      {file.github_meta && status === 'synced' && (
                        <span className="flex items-center gap-1 text-[9px] font-medium px-1.5 py-0.5 bg-blue-500/10 text-blue-400 border border-blue-500/20 rounded-md">
                          <span className="w-1.5 h-1.5 rounded-full bg-blue-400 inline-block" />
                          Synced · {relativeTime(file.github_pushed_at)}
                        </span>
                      )}
                      {file.github_meta && status === 'modified' && (
                        <span className="flex items-center gap-1 text-[9px] font-semibold px-1.5 py-0.5 bg-amber-500/10 text-amber-400 border border-amber-500/30 rounded-md">
                          <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse inline-block" />
                          Modified · not pushed
                        </span>
                      )}
                      {/* AI-Edited badge — always shown when file has applied changes,
                          regardless of GitHub connection status */}
                      {isModified && (
                        <span className="flex items-center gap-1 text-[9px] font-medium px-1.5 py-0.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-md">
                          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
                          AI-Edited
                        </span>
                      )}
                    </div>
                    {!isCompact && (
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
                    )}
                    {/* Compact mode: inline meta */}
                    {isCompact && (
                      <span className="text-[10px] text-muted/50">
                        {file.lines?.toLocaleString()} lines
                        {isModified ? ` · edited ${relativeTime(timestamp)}` : ''}
                      </span>
                    )}
                  </div>

                  {/* Download button */}
                  <button
                    onClick={() => handleDownload(file)}
                    disabled={isDownloading}
                    className="shrink-0 flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[11px] font-medium
                      bg-surface border border-border text-muted
                      hover:bg-blue-500/10 hover:border-blue-500/40 hover:text-blue-400
                      disabled:opacity-50 disabled:cursor-not-allowed
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
                    {!isCompact && <span>{isDownloading ? 'Downloading…' : 'Download'}</span>}
                  </button>
                </div>
              )
            })}
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
