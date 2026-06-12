/**
 * UploadPreview — renders a live preview of uploaded visual files (.tsx/.jsx/.html)
 * directly in the chat panel, BEFORE any AI response. Users can immediately
 * pick elements and then type their prompt with element context attached.
 *
 * New file — zero changes to LivePreview or any existing component.
 */
import React, { useState, useEffect, useCallback } from 'react'
import { useAppStore } from '../../stores/appStore'
import { useElementPickerStore } from '../../stores/elementPickerStore'
import { PickablePreview } from './PickablePreview'
import { ElementPickerToolbar } from './ElementPickerToolbar'
import { isVisualFile } from '../LivePreview'
import { api } from '../../api/client'
import { Visibility, VisibilityOff, UnfoldMore, UnfoldLess } from '@mui/icons-material'
import type { SessionFile } from '../../types'

export function UploadPreview() {
  const sessionFiles = useAppStore(s => s.sessionFiles)
  const activeSessions = useAppStore(s => s.activeSessions)

  // Filter to visual files only
  const visualFiles = sessionFiles.filter(f => isVisualFile(f.filename))

  // Panel state
  const [expanded, setExpanded] = useState(false)
  const [activeFileId, setActiveFileId] = useState<string | null>(null)
  const [fileContent, setFileContent] = useState<string>('')
  const [loading, setLoading] = useState(false)

  // Pick mode from store
  const pickMode = useElementPickerStore(s => s.pickMode)
  const setPickMode = useElementPickerStore(s => s.setPickMode)

  // Auto-select first visual file when one appears
  useEffect(() => {
    if (visualFiles.length > 0 && !activeFileId) {
      setActiveFileId(visualFiles[0].id)
    }
    // Clear if the active file was removed
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

    // Use local content if available (sidebar upload path)
    if (file.content) {
      setFileContent(file.content)
      return
    }

    // Otherwise fetch from API
    setLoading(true)
    api.sessionFiles.get(activeSessions, activeFileId)
      .then((f: any) => setFileContent(f.content || ''))
      .catch(() => setFileContent(''))
      .finally(() => setLoading(false))
  }, [activeFileId, activeSessions]) // eslint-disable-line react-hooks/exhaustive-deps

  // Nothing to show
  if (visualFiles.length === 0) return null

  const activeFile = visualFiles.find(f => f.id === activeFileId)
  const filename = activeFile?.filename || ''

  return (
    <div className="border-b border-border/60 bg-surface/30">
      {/* Toggle bar */}
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-overlay/40 transition-colors"
      >
        {expanded
          ? <VisibilityOff sx={{ fontSize: 14 }} className="text-muted/70" />
          : <Visibility sx={{ fontSize: 14 }} className="text-accent" />
        }
        <span className="text-[12px] font-medium text-ink/80">
          {expanded ? 'Hide Preview' : 'Preview'}
        </span>

        {/* File tabs — only when multiple visual files */}
        {visualFiles.length > 1 && (
          <div className="flex items-center gap-1 ml-2">
            {visualFiles.map(f => (
              <span
                key={f.id}
                onClick={(e) => {
                  e.stopPropagation()
                  setActiveFileId(f.id)
                  if (!expanded) setExpanded(true)
                }}
                className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                  f.id === activeFileId
                    ? 'bg-accent/15 text-accent border border-accent/30'
                    : 'bg-overlay/40 text-muted hover:text-ink hover:bg-overlay/60'
                }`}
              >
                {f.filename}
              </span>
            ))}
          </div>
        )}

        {visualFiles.length === 1 && (
          <span className="text-[11px] font-mono text-muted/60">{filename}</span>
        )}

        <span className="ml-auto">
          {expanded
            ? <UnfoldLess sx={{ fontSize: 14 }} className="text-muted/50" />
            : <UnfoldMore sx={{ fontSize: 14 }} className="text-muted/50" />
          }
        </span>
      </button>

      {/* Preview panel */}
      {expanded && (
        <div className="px-3 pb-3">
          {loading ? (
            <div className="flex items-center justify-center h-[200px] text-[12px] text-muted/60">
              Loading preview…
            </div>
          ) : fileContent ? (
            <div className="rounded-xl overflow-hidden border border-border/60 bg-white">
              <PickablePreview
                code={fileContent}
                filename={filename}
                sessionId={activeSessions || ''}
                fileId={activeFileId || ''}
                pickMode={pickMode}
              />
            </div>
          ) : (
            <div className="flex items-center justify-center h-[200px] text-[12px] text-muted/60">
              No content available
            </div>
          )}

          {/* Picker toolbar */}
          <ElementPickerToolbar />
        </div>
      )}
    </div>
  )
}
