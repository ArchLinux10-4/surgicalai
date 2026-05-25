/**
 * MobileFilesPanel — manage session-uploaded files on mobile.
 * Shows all files in the current session with download and remove.
 */
import React from 'react'
import { useAppStore } from '../../stores/appStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'

const EXT_COLORS: Record<string, string> = {
  ts: '#3178c6', tsx: '#3178c6', js: '#f7df1e', jsx: '#61dafb',
  py: '#3572a5', html: '#e34c26', css: '#563d7c', json: '#292929',
  md: '#083fa1',
}

function FileIcon({ ext }: { ext: string }) {
  const color = EXT_COLORS[ext] || '#94a3b8'
  return (
    <div
      className="w-9 h-9 rounded-lg flex items-center justify-center text-[9px] font-bold font-mono flex-shrink-0"
      style={{ background: `${color}20`, border: `1px solid ${color}40`, color }}
    >
      {ext.toUpperCase().slice(0, 3)}
    </div>
  )
}

export function MobileFilesPanel() {
  const { sessionFiles, setSessionFiles, activeSessions } = useAppStore()

  const removeFile = async (fileId: string, filename: string) => {
    if (!activeSessions) return
    try {
      await api.sessionFiles.delete(activeSessions, fileId)
      setSessionFiles(sessionFiles.filter(f => f.id !== fileId))
      toast.success(`Removed ${filename}`)
    } catch {
      toast.error('Remove failed')
    }
  }

  const downloadFile = async (fileId: string, filename: string) => {
    if (!activeSessions) return
    try {
      const file = await api.sessionFiles.get(activeSessions, fileId)
      const blob = new Blob([file.content], { type: 'text/plain' })
      const url  = URL.createObjectURL(blob)
      const a    = document.createElement('a')
      a.href     = url
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      toast.error('Download failed')
    }
  }

  const formatSize = (lines: number) => {
    if (lines < 100)  return `${lines}L`
    if (lines < 1000) return `${lines}L`
    return `${(lines / 1000).toFixed(1)}kL`
  }

  return (
    <div className="flex flex-col h-full bg-base">
      {/* Header */}
      <div className="flex-shrink-0 flex items-center justify-between px-4 py-3 border-b border-border bg-surface/80">
        <span className="text-sm font-semibold text-ink/80">Files in Session</span>
        <span className="text-[11px] text-muted/50">
          {sessionFiles.length} file{sessionFiles.length !== 1 ? 's' : ''}
        </span>
      </div>

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
        ) : (
          <div className="py-2">
            {sessionFiles.map(file => {
              const ext = file.filename.split('.').pop()?.toLowerCase() || ''
              return (
                <div
                  key={file.id}
                  className="flex items-center gap-3 px-4 py-3 border-b border-border/30"
                >
                  <FileIcon ext={ext} />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-ink truncate">{file.filename}</p>
                    <p className="text-[11px] text-muted/50 mt-0.5">
                      {formatSize(file.lines || 0)}
                      {file.symbol_count > 0 && ` · ${file.symbol_count} symbols`}
                    </p>
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    {/* Download */}
                    <button
                      onClick={() => downloadFile(file.id, file.filename)}
                      className="w-8 h-8 flex items-center justify-center rounded-lg text-muted/50 hover:text-blue-400 hover:bg-blue-400/10 transition-colors"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                        <polyline points="7 10 12 15 17 10"/>
                        <line x1="12" y1="15" x2="12" y2="3"/>
                      </svg>
                    </button>
                    {/* Remove */}
                    <button
                      onClick={() => removeFile(file.id, file.filename)}
                      className="w-8 h-8 flex items-center justify-center rounded-lg text-muted/40 hover:text-red-400 hover:bg-red-400/10 transition-colors"
                    >
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
                      </svg>
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Info footer */}
      <div className="flex-shrink-0 px-4 py-3 border-t border-border/50">
        <p className="text-[10px] text-muted/40 text-center">
          Files are scoped to the current session · Download to save changes
        </p>
      </div>
    </div>
  )
}
