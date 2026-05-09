import React, { useState, useEffect, useCallback } from 'react'
import { Github } from 'lucide-react'
import { api } from '../api/client'
import type { SessionFile } from '../types'
import { GitHubCommitModal } from './GitHubCommitModal'

interface SessionFilesTrayProps {
  sessionId: string
  sessionFiles: SessionFile[]  // live list from parent — always fresh
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

export function SessionFilesTray({ sessionId, sessionFiles }: SessionFilesTrayProps) {
  const [collapsed, setCollapsed] = useState(false)
  const [downloading, setDownloading] = useState<string | null>(null)
  const [showCommitModal, setShowCommitModal] = useState(false)

  const handleDownload = useCallback(async (file: SessionFile) => {
    setDownloading(file.id)
    try {
      await api.sessionFiles.download(sessionId, file.id, file.filename)
    } catch {
      // silently fail — toast would require import
    } finally {
      setDownloading(null)
    }
  }, [sessionId])

  if (sessionFiles.length === 0) return null

  const modifiedFiles = sessionFiles.filter(f => f.updated_at && f.updated_at !== f.created_at)

  return (
    <>
    <div className="border border-border/60 rounded-xl bg-surface/50 overflow-hidden mb-2 mx-1 backdrop-blur-sm">
      {/* Header row */}
      <button
        onClick={() => setCollapsed(c => !c)}
        className="w-full flex items-center justify-between px-3.5 py-2.5 hover:bg-surface-alt/50 transition-colors select-none"
      >
        <div className="flex items-center gap-2">
          <svg className="w-3.5 h-3.5 text-muted/70" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 7a2 2 0 012-2h3l2 2h9a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
          </svg>
          <span className="text-[12px] font-medium text-ink/80">Session Files</span>
          <span className="text-[10px] font-semibold px-1.5 py-0.5 bg-blue-500/15 text-blue-400 border border-blue-500/25 rounded-full">
            {sessionFiles.length}
          </span>
          {modifiedFiles.length > 0 && (
            <span className="text-[10px] font-medium px-1.5 py-0.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-full">
              {modifiedFiles.length} AI-edited
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {modifiedFiles.some(f => f.github_meta) && (
            <button
              onClick={e => { e.stopPropagation(); setShowCommitModal(true) }}
              className="flex items-center gap-1 px-2 py-0.5 rounded-md bg-gray-700/60 hover:bg-gray-700 text-white text-[10px] font-semibold border border-gray-600/50 transition-colors"
              title="Push changes to GitHub"
            >
              <Github size={10} />
              Push
            </button>
          )}
          <span className="text-[10px] text-muted/50">Download any file below</span>
          <svg
            className={`w-3.5 h-3.5 text-muted/50 transition-transform ${collapsed ? '' : 'rotate-180'}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </button>

      {/* File list */}
      {!collapsed && (
        <div className="divide-y divide-border/40">
          {sessionFiles.map(file => {
            const isModified = file.updated_at && file.updated_at !== file.created_at
            const timestamp = isModified ? file.updated_at : file.created_at
            const isDownloading = downloading === file.id

            return (
              <div
                key={file.id}
                className="flex items-center gap-3 px-3.5 py-2.5 hover:bg-surface-alt/30 transition-colors group"
              >
                {/* File icon */}
                <div className="shrink-0">
                  <svg className="w-4 h-4 text-muted/50" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                </div>

                {/* Filename + meta */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[13px] font-medium text-ink truncate max-w-[200px]" title={file.filename}>
                      {file.filename}
                    </span>
                    {/* Language badge */}
                    <span className={`text-[10px] font-medium px-1.5 py-0.5 border rounded-md ${langColor(file.language)}`}>
                      {file.language || 'text'}
                    </span>
                    {/* Modified badge */}
                    {isModified && (
                      <span className="flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-md">
                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
                        AI edited
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
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                  ) : (
                    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                    </svg>
                  )}
                  <span>{isDownloading ? 'Downloading…' : 'Download'}</span>
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
          onSuccess={() => setShowCommitModal(false)}
        />
      )}
    </>
  )
}
