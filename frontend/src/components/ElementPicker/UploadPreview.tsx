/**
 * UploadPreview — Element Picker for uploaded visual files (.tsx/.jsx/.html).
 * Opens as a modal overlay when the user clicks the trigger button.
 *
 * - Default OFF — just a small button appears when visual files are uploaded
 * - Searchable dropdown for file selection (handles 100+ files)
 * - Fullscreen toggle
 * - Single-file stub mode (no sessionId/fileId passed) to avoid import errors
 *
 * New file — zero changes to LivePreview or any existing component.
 */
import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useAppStore } from '../../stores/appStore'
import { useElementPickerStore } from '../../stores/elementPickerStore'
import { PickablePreview } from './PickablePreview'
import { ElementPickerToolbar } from './ElementPickerToolbar'
import { isVisualFile } from '../LivePreview'
import { api } from '../../api/client'
import {
  Fullscreen, FullscreenExit, Close, Search, TouchApp,
} from '@mui/icons-material'
import type { SessionFile } from '../../types'

/* ── Main component ─────────────────────────────────────────────── */
export function UploadPreview() {
  const sessionFiles = useAppStore(s => s.sessionFiles)
  const activeSessions = useAppStore(s => s.activeSessions)

  // Filter to visual files only
  const visualFiles = useMemo(
    () => sessionFiles.filter(f => isVisualFile(f.filename)),
    [sessionFiles],
  )

  // Modal state — default OFF
  const [open, setOpen] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [activeFileId, setActiveFileId] = useState<string | null>(null)
  const [fileContent, setFileContent] = useState<string>('')
  const [loading, setLoading] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  // Pick mode from store
  const pickMode = useElementPickerStore(s => s.pickMode)
  const setPickMode = useElementPickerStore(s => s.setPickMode)

  // Auto-select first visual file
  useEffect(() => {
    if (visualFiles.length > 0 && !activeFileId) {
      setActiveFileId(visualFiles[0].id)
    }
    if (activeFileId && !visualFiles.find(f => f.id === activeFileId)) {
      setActiveFileId(visualFiles.length > 0 ? visualFiles[0].id : null)
    }
  }, [visualFiles.length]) // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch file content when active file changes
  useEffect(() => {
    if (!activeFileId || !activeSessions) {
      setFileContent('')
      return
    }
    const file = visualFiles.find(f => f.id === activeFileId)
    if (!file) return

    if (file.content) {
      setFileContent(file.content)
      return
    }

    // Fetch from API (upload response doesn't include content)
    setLoading(true)
    api.sessionFiles.get(activeSessions, activeFileId)
      .then((f: any) => setFileContent(f?.content || ''))
      .catch((err: any) => {
        console.error('[UploadPreview] fetch failed:', err)
        setFileContent('')
      })
      .finally(() => setLoading(false))
  }, [activeFileId, activeSessions]) // eslint-disable-line react-hooks/exhaustive-deps

  // Close dropdown on outside click
  useEffect(() => {
    if (!dropdownOpen) return
    function onClickOutside(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [dropdownOpen])

  // ESC closes modal
  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false)
        setFullscreen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // Filtered file list for search
  const filteredFiles = useMemo(() => {
    if (!searchQuery.trim()) return visualFiles
    const q = searchQuery.toLowerCase()
    return visualFiles.filter(f => f.filename.toLowerCase().includes(q))
  }, [visualFiles, searchQuery])

  // Nothing to show
  if (visualFiles.length === 0) return null

  const activeFile = visualFiles.find(f => f.id === activeFileId)
  const filename = activeFile?.filename || ''

  // ── Trigger button (default state — modal closed) ────────────
  if (!open) {
    return (
      <div className="border-b border-border/60 bg-surface/30">
        <button
          onClick={() => setOpen(true)}
          className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-overlay/40 transition-colors group"
        >
          <TouchApp sx={{ fontSize: 16 }} className="text-accent" />
          <span className="text-[12px] font-medium text-ink/80">
            Element Picker
          </span>
          <span className="text-[11px] text-muted/60">
            ({visualFiles.length} visual file{visualFiles.length !== 1 ? 's' : ''})
          </span>
          <span className="ml-auto text-[10px] text-muted/40 opacity-0 group-hover:opacity-100 transition-opacity">
            click to open
          </span>
        </button>
      </div>
    )
  }

  // ── Modal overlay ────────────────────────────────────────────
  const modalClasses = fullscreen
    ? 'fixed inset-0 z-[9999] bg-surface flex flex-col'
    : 'fixed inset-4 z-[9999] bg-surface rounded-2xl shadow-2xl border border-border/60 flex flex-col overflow-hidden'

  return (
    <div className={modalClasses}>
      {/* Backdrop */}
      {!fullscreen && (
        <div
          className="fixed inset-0 z-[9998] bg-black/40 backdrop-blur-sm"
          onClick={() => { setOpen(false); setFullscreen(false) }}
        />
      )}

      {/* Header */}
      <div className="relative z-[9999] flex items-center gap-3 px-4 py-3 border-b border-border/60 bg-surface shrink-0">
        <TouchApp sx={{ fontSize: 18 }} className="text-accent" />
        <span className="text-[13px] font-semibold text-ink">Element Picker</span>

        {/* File selector dropdown */}
        <div className="relative ml-3" ref={dropdownRef}>
          <button
            onClick={() => setDropdownOpen(o => !o)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-overlay/50 hover:bg-overlay/70 border border-border/40 text-[12px] font-mono text-ink/80 transition-colors min-w-[180px]"
          >
            <span className="truncate max-w-[200px]">{filename || 'Select file…'}</span>
            <span className="text-muted/50 ml-auto text-[10px]">▾</span>
          </button>

          {/* Dropdown */}
          {dropdownOpen && (
            <div className="absolute top-full left-0 mt-1 w-[320px] max-h-[400px] bg-surface border border-border/60 rounded-xl shadow-xl overflow-hidden z-[10000]">
              {/* Search */}
              {visualFiles.length > 5 && (
                <div className="flex items-center gap-2 px-3 py-2 border-b border-border/40">
                  <Search sx={{ fontSize: 14 }} className="text-muted/50" />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={e => setSearchQuery(e.target.value)}
                    placeholder="Search files…"
                    autoFocus
                    className="flex-1 bg-transparent text-[12px] text-ink outline-none placeholder:text-muted/40"
                  />
                </div>
              )}

              {/* File list */}
              <div className="overflow-y-auto max-h-[350px]">
                {filteredFiles.length === 0 ? (
                  <div className="px-3 py-4 text-[11px] text-muted/50 text-center">
                    No matching files
                  </div>
                ) : (
                  filteredFiles.map(f => (
                    <button
                      key={f.id}
                      onClick={() => {
                        setActiveFileId(f.id)
                        setDropdownOpen(false)
                        setSearchQuery('')
                      }}
                      className={`w-full text-left px-3 py-2 text-[12px] font-mono transition-colors ${
                        f.id === activeFileId
                          ? 'bg-accent/10 text-accent border-l-2 border-accent'
                          : 'text-ink/70 hover:bg-overlay/40 border-l-2 border-transparent'
                      }`}
                    >
                      {f.filename}
                    </button>
                  ))
                )}
              </div>
            </div>
          )}
        </div>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Fullscreen toggle */}
        <button
          onClick={() => setFullscreen(f => !f)}
          className="p-1.5 rounded-lg hover:bg-overlay/50 transition-colors text-muted/60 hover:text-ink"
          title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
        >
          {fullscreen
            ? <FullscreenExit sx={{ fontSize: 18 }} />
            : <Fullscreen sx={{ fontSize: 18 }} />
          }
        </button>

        {/* Close */}
        <button
          onClick={() => { setOpen(false); setFullscreen(false) }}
          className="p-1.5 rounded-lg hover:bg-overlay/50 transition-colors text-muted/60 hover:text-ink"
          title="Close (ESC)"
        >
          <Close sx={{ fontSize: 18 }} />
        </button>
      </div>

      {/* Preview area */}
      <div className="flex-1 min-h-0 relative z-[9999]">
        {loading ? (
          <div className="flex items-center justify-center h-full text-[13px] text-muted/60">
            Loading preview…
          </div>
        ) : fileContent ? (
          <div className="h-full bg-white">
            {/* No sessionId/fileId — forces single-file stub mode, avoids import resolution errors */}
            <PickablePreview
              code={fileContent}
              filename={filename}
            />
          </div>
        ) : (
          <div className="flex items-center justify-center h-full text-[13px] text-muted/60">
            {activeFileId ? 'No content available' : 'Select a file to preview'}
          </div>
        )}
      </div>

      {/* Element Picker toolbar — fixed at bottom */}
      <div className="shrink-0 border-t border-border/60 bg-surface relative z-[9999]">
        <ElementPickerToolbar />
      </div>
    </div>
  )
}
