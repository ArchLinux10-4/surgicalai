/**
 * MobileFilesPanel — session files with status badges.
 * Shows AI-Edited and GitHub sync status per file.
 * GitHub section only renders when the file has github_meta (conditional).
 */
import React, { useEffect, useState } from 'react'
import { useAppStore } from '../../stores/appStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'
import type { SessionFile } from '../../types'
import { FileFilterTabs, NewBadge, matchesFileFilter, fileCounts, isCreatedFile } from '../../lib/fileClassify'

const EXT_COLORS: Record<string, string> = {
  ts: '#3178c6', tsx: '#3178c6', js: '#f7df1e', jsx: '#61dafb',
  py: '#3572a5', html: '#e34c26', css: '#563d7c', json: '#292929',
  md: '#083fa1',
}

function FileIcon({ ext }: { ext: string }) {
  const color = EXT_COLORS[ext] || '#94a3b8'
  return (
    <div className="w-9 h-9 rounded-lg flex items-center justify-center text-[9px] font-bold font-mono flex-shrink-0"
      style={{ background: `${color}20`, border: `1px solid ${color}40`, color }}>
      {ext.toUpperCase().slice(0, 3)}
    </div>
  )
}

// Same sync logic as desktop SessionFilesTray
function syncStatus(file: SessionFile): 'synced' | 'modified' | 'never' {
  if (!file.github_pushed_at) return 'never'
  const pushed  = new Date(file.github_pushed_at).getTime()
  const updated = file.updated_at ? new Date(file.updated_at).getTime() : 0
  return updated > pushed + 2000 ? 'modified' : 'synced'
}

function relativeTime(dateStr?: string): string {
  if (!dateStr) return ''
  const diff = Date.now() - new Date(dateStr).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1)  return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

export function MobileFilesPanel() {
  const { sessionFiles, setSessionFiles, activeSessions, fileFilter } = useAppStore()
  const counts = fileCounts(sessionFiles)
  const visibleFiles = sessionFiles.filter(f => matchesFileFilter(f, fileFilter))
  // Track which files have applied changes in DB (for AI-Edited badge accuracy)
  const [appliedFileIds, setAppliedFileIds] = useState<Set<string>>(new Set())


  // Load applied change state from backend on mount / session change
  useEffect(() => {
    if (!activeSessions) return
    // Get all applied change IDs for this session, then cross-reference to files
    // We check localStorage keys matching sai-applied:{sessionId}: for file-level detection
    // Also use updated_at heuristic as primary signal (matches desktop logic)
    try {
      const keys = Object.keys(localStorage)
      const prefix = `sai-applied:${activeSessions}:`
      const hasApplied = new Set<string>()
      keys.forEach(k => {
        if (k.startsWith(prefix) && localStorage.getItem(k) === '1') {
          // Key format: sai-applied:{sessionId}:{changeId}
          // We can't directly map changeId → fileId from localStorage alone,
          // so we use updated_at heuristic (same as desktop) supplemented by
          // the backend getApplied call
          hasApplied.add(k)
        }
      })
      setAppliedFileIds(hasApplied)
    } catch {}
  }, [activeSessions, sessionFiles])

  const removeFile = async (fileId: string, filename: string) => {
    if (!activeSessions) return
    try {
      await api.sessionFiles.delete(activeSessions, fileId)
      setSessionFiles(sessionFiles.filter(f => f.id !== fileId))
      toast.success(`Removed ${filename}`)
    } catch { toast.error('Remove failed') }
  }

  const downloadFile = async (file: SessionFile) => {
    if (!activeSessions) return
    try {
      const f = await api.sessionFiles.get(activeSessions, file.id)
      const blob = new Blob([f.content], { type: 'text/plain' })
      const url  = URL.createObjectURL(blob)
      const a    = document.createElement('a')
      a.href = url; a.download = file.filename; a.click()
      URL.revokeObjectURL(url)
    } catch { toast.error('Download failed') }
  }

  // Counts for header summary
  const aiEditedCount  = sessionFiles.filter(f => f.updated_at && f.updated_at !== f.created_at).length
  const hasGithub      = sessionFiles.some(f => f.github_meta)
  const syncedCount    = sessionFiles.filter(f => syncStatus(f) === 'synced').length
  const modifiedCount  = sessionFiles.filter(f => syncStatus(f) === 'modified').length

  return (
    <div className="flex flex-col h-full bg-base">

      {/* Summary header strip */}
      {sessionFiles.length > 0 && (
        <div className="flex-shrink-0 flex items-center gap-2 px-4 py-2.5 border-b border-border/50 flex-wrap">
          <span className="text-[11px] text-muted/60 font-medium">{sessionFiles.length} file{sessionFiles.length !== 1 ? 's' : ''}</span>
          {aiEditedCount > 0 && (
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-medium">
              {aiEditedCount} AI-Edited
            </span>
          )}
          {hasGithub && syncedCount > 0 && (
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-medium">
              {syncedCount} Synced
            </span>
          )}
          {hasGithub && modifiedCount > 0 && (
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/25 font-medium">
              {modifiedCount} Modified
            </span>
          )}
        </div>
      )}

      {/* Current / New / All segmented filter — synced with desktop */}
      {sessionFiles.length > 0 && (
        <div className="flex-shrink-0 px-4 py-2 border-b border-border/50">
          <FileFilterTabs counts={counts} size="sm" />
        </div>
      )}

      {/* List */}
      <div className="flex-1 overflow-y-auto">
        {sessionFiles.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 px-6 text-center">
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none"
              stroke="rgba(148,163,184,0.3)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
              <polyline points="13 2 13 9 20 9"/>
            </svg>
            <div>
              <p className="text-sm text-muted/60 mb-1">No files uploaded yet</p>
              <p className="text-xs text-muted/40">Upload files from the Chat tab to get started</p>
            </div>
          </div>
        ) : visibleFiles.length === 0 ? (
          <div className="px-6 py-8 text-center text-[12px] text-muted/50">
            No {fileFilter === 'new' ? 'new' : 'current'} files
          </div>
        ) : (
          <div className="py-2">
            {visibleFiles.map(file => {
              const ext       = file.filename.split('.').pop()?.toLowerCase() || ''
              const isEdited  = !!(file.updated_at && file.updated_at !== file.created_at)
              const status    = syncStatus(file)
              const hasGH     = !!file.github_meta

              return (
                <div key={file.id}
                  className="px-4 py-3 border-b border-border/30">

                  {/* Row 1: icon + name + actions */}
                  <div className="flex items-center gap-3">
                    <FileIcon ext={ext} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <p className="text-sm font-medium text-ink truncate">{file.filename}</p>
                        {isCreatedFile(file) && <NewBadge compact />}
                      </div>
                      <p className="text-[11px] text-muted/50 mt-0.5">
                        {(file.lines || 0).toLocaleString()}L
                        {file.symbol_count > 0 && ` · ${file.symbol_count} sym`}
                        {isEdited && ` · edited ${relativeTime(file.updated_at)}`}
                      </p>
                    </div>
                    <div className="flex items-center gap-1 flex-shrink-0">
                      <button onClick={() => downloadFile(file)}
                        className="w-8 h-8 flex items-center justify-center rounded-lg text-muted/50 hover:text-blue-400 hover:bg-blue-400/10 transition-colors">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                          <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
                        </svg>
                      </button>
                      <button onClick={() => removeFile(file.id, file.filename)}
                        className="w-8 h-8 flex items-center justify-center rounded-lg text-muted/40 hover:text-red-400 hover:bg-red-400/10 transition-colors">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <polyline points="3 6 5 6 21 6"/>
                          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
                        </svg>
                      </button>
                    </div>
                  </div>

                  {/* Row 2: status badges — only shown when there's something to show */}
                  {(isEdited || hasGH) && (
                    <div className="flex items-center gap-1.5 mt-2 ml-12 flex-wrap">
                      {/* AI-Edited badge */}
                      {isEdited && (
                        <span className="flex items-center gap-1 text-[9px] font-medium px-2 py-0.5
                          bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-md">
                          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
                          AI-Edited
                        </span>
                      )}
                      {/* GitHub sync badges — only if file has github_meta */}
                      {hasGH && status === 'synced' && (
                        <span className="flex items-center gap-1 text-[9px] font-medium px-2 py-0.5
                          bg-blue-500/10 text-blue-400 border border-blue-500/20 rounded-md">
                          <svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor">
                            <path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.54-1.38-1.33-1.75-1.33-1.75-1.09-.74.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.17 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 3-.4c1.02.005 2.04.138 3 .4 2.28-1.55 3.29-1.23 3.29-1.23.66 1.65.24 2.87.12 3.17.77.84 1.23 1.91 1.23 3.22 0 4.61-2.81 5.63-5.48 5.92.43.37.82 1.1.82 2.22v3.29c0 .32.21.7.83.58C20.57 21.8 24 17.3 24 12c0-6.63-5.37-12-12-12z"/>
                          </svg>
                          Synced · {relativeTime(file.github_pushed_at)}
                        </span>
                      )}
                      {hasGH && status === 'modified' && (
                        <span className="flex items-center gap-1 text-[9px] font-semibold px-2 py-0.5
                          bg-amber-500/10 text-amber-400 border border-amber-500/25 rounded-md">
                          <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse inline-block" />
                          Modified · not pushed
                        </span>
                      )}
                      {hasGH && status === 'never' && (
                        <span className="flex items-center gap-1 text-[9px] font-medium px-2 py-0.5
                          bg-surface text-muted/50 border border-border/50 rounded-md">
                          Not pushed to GitHub
                        </span>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="flex-shrink-0 px-4 py-3 border-t border-border/50">
        <p className="text-[10px] text-muted/40 text-center">
          Files are scoped to the current session · Download to save changes
        </p>
      </div>

    </div>
  )
}
