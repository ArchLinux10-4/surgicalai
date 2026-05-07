import React, { useState, useEffect } from 'react'
import { CheckCircle, XCircle, Download, ChevronDown, ChevronUp, AlertTriangle, FileCode } from 'lucide-react'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import type { SmartResult } from '../types'
import { LivePreview, isVisualFile } from './LivePreview'

interface Props {
  result: SmartResult
  sessionId: string
  onApplied?: (filename: string, modifiedContent: string) => void
}

// --- localStorage helpers for persisting applied state across logins ---
const appliedKey = (sessionId: string, changeId: string) =>
  `sai-applied:${sessionId}:${changeId}`

const loadApplied = (sessionId: string, changeIds: string[]): Record<string, boolean> => {
  const out: Record<string, boolean> = {}
  for (const id of changeIds) {
    if (localStorage.getItem(appliedKey(sessionId, id)) === '1') out[id] = true
  }
  return out
}

const saveApplied = (sessionId: string, changeId: string) => {
  try { localStorage.setItem(appliedKey(sessionId, changeId), '1') } catch {}
}
// -----------------------------------------------------------------------

function ConfidenceBadge({ score }: { score: number }) {
  const color = score >= 8 ? 'text-green-400 bg-green-400/10 border-green-400/30'
    : score >= 6 ? 'text-yellow-400 bg-yellow-400/10 border-yellow-400/30'
    : 'text-red-400 bg-red-400/10 border-red-400/30'
  const label = score >= 8 ? 'High' : score >= 6 ? 'Medium' : 'Low'
  return (
    <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border ${color}`}>
      {label} {score}/10
    </span>
  )
}

function DiffLine({ line }: { line: string }) {
  const isAdd = line.startsWith('+') && !line.startsWith('+++')
  const isRemove = line.startsWith('-') && !line.startsWith('---')
  const isHeader = line.startsWith('@@')
  return (
    <div className={`font-mono text-[12px] px-3 py-0.5 leading-relaxed whitespace-pre-wrap break-all ${
      isAdd ? 'bg-green-500/10 text-green-300' :
      isRemove ? 'bg-red-500/10 text-red-300' :
      isHeader ? 'bg-blue-500/10 text-blue-300' :
      'text-muted'
    }`}>
      {line || ' '}
    </div>
  )
}

function FileChangeCard({ filename, fileData, sessionId, onApplied, onChangeApplied }: {
  filename: string
  fileData: { filename: string; file_id: string; changes: any[] }
  sessionId: string
  onApplied?: (filename: string, content: string) => void
  onChangeApplied?: () => void
}) {
  const [expanded, setExpanded] = useState(true)
  const [applying, setApplying] = useState<Record<string, boolean>>({})

  // Rehydrate applied state from localStorage on mount
  const changeIds = fileData.changes.map((c: any) => c.id)
  const [applied, setApplied] = useState<Record<string, boolean>>(() =>
    loadApplied(sessionId, changeIds)
  )
  const [skipped, setSkipped] = useState<Record<string, boolean>>({})
  const [originalCode, setOriginalCode] = useState<string>('')
  const [modifiedCode, setModifiedCode] = useState<string | undefined>(undefined)

  // Pre-fetch original file content so Preview works before Apply
  useEffect(() => {
    if (!isVisualFile(filename)) return
    api.sessionFiles.get(sessionId, fileData.file_id)
      .then(f => setOriginalCode(f.content))
      .catch(() => {})
  }, [sessionId, fileData.file_id, filename])

  // Filter out ghost diffs (0 added + 0 removed) on the frontend side
  const realChanges = fileData.changes.filter((c: any) => {
    if (!c.diff) return false
    const lines = c.diff.split('\n')
    const hasAdds = lines.some((l: string) => l.startsWith('+') && !l.startsWith('+++'))
    const hasRemoves = lines.some((l: string) => l.startsWith('-') && !l.startsWith('---'))
    return hasAdds || hasRemoves
  })

  // Reconstruct proposed file by swapping the changed symbol — no API call needed
  const getProposedCode = (change: any): string => {
    if (!originalCode) return '// Loading preview...'
    const orig = change.original_code
    const next = change.new_code
    if (!orig || !next) return originalCode
    const idx = originalCode.indexOf(orig)
    if (idx === -1) return originalCode // symbol not found verbatim — show original
    return originalCode.slice(0, idx) + next + originalCode.slice(idx + orig.length)
  }

  const markApplied = (changeId: string) => {
    saveApplied(sessionId, changeId)
    setApplied(p => ({ ...p, [changeId]: true }))
    onChangeApplied?.()
  }

  const handleApply = async (change: any) => {
    setApplying(p => ({ ...p, [change.id]: true }))
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, fileData.file_id)
      if (!originalCode) setOriginalCode(fileData2.content)
      const result = await api.surgical.apply({
        file_path: filename,
        changes: [change],
        file_content: fileData2.content,
      })

      if (result.cloud_mode || result.modified_content) {
        // Cloud mode: trigger download
        const blob = new Blob([result.modified_content], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = filename
        a.click()
        URL.revokeObjectURL(url)
        toast.success(`Downloaded modified ${filename}`)
        setModifiedCode(result.modified_content)
        onApplied?.(filename, result.modified_content)
      } else {
        toast.success(`Applied change to ${filename}`)
        setModifiedCode(result.modified_content || '')
        onApplied?.(filename, result.modified_content || '')
      }
      markApplied(change.id)
    } catch (e: any) {
      toast.error(e.message || 'Apply failed')
    } finally {
      setApplying(p => ({ ...p, [change.id]: false }))
    }
  }

  const handleDownload = async (change: any) => {
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, fileData.file_id)
      const result = await api.surgical.apply({
        file_path: filename,
        changes: [change],
        file_content: fileData2.content,
      })
      const content = result.modified_content || fileData2.content
      const blob = new Blob([content], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
      toast.success(`Downloaded ${filename}`)
    } catch (e: any) {
      toast.error(e.message || 'Download failed')
    }
  }

  const handleApplyAll = async () => {
    const unapplied = realChanges.filter(c => !applied[c.id] && !skipped[c.id])
    if (unapplied.length === 0) return

    // Single apply for single change — reuse existing logic
    if (unapplied.length === 1) {
      await handleApply(unapplied[0])
      return
    }

    // Multiple changes: fetch file ONCE, send ALL changes, ONE download
    setApplying(Object.fromEntries(unapplied.map(c => [c.id, true])))
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, fileData.file_id)
      if (!originalCode) setOriginalCode(fileData2.content)
      const result = await api.surgical.applyAll({
        file_path: filename,
        changes: unapplied,
        file_content: fileData2.content,
      })

      if (result.cloud_mode || result.modified_content) {
        const blob = new Blob([result.modified_content], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = filename
        a.click()
        URL.revokeObjectURL(url)
        toast.success(`Downloaded ${filename} with all ${unapplied.length} changes applied`)
        setModifiedCode(result.modified_content)
        onApplied?.(filename, result.modified_content)
      } else {
        toast.success(`Applied all ${unapplied.length} changes to ${filename}`)
        setModifiedCode(result.modified_content || '')
        onApplied?.(filename, result.modified_content || '')
      }
      // Mark all as applied
      for (const change of unapplied) {
        markApplied(change.id)
      }
    } catch (e: any) {
      toast.error(e.message || 'Apply All failed')
    } finally {
      setApplying({})
    }
  }

  // If all changes are ghosts, don't render the card at all
  if (realChanges.length === 0) return null
  const displayData = { ...fileData, changes: realChanges }

  return (
    <div className="border border-border rounded-xl overflow-hidden mb-3">
      {/* File header */}
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center gap-2.5 px-4 py-2.5 bg-surface/80 hover:bg-surface transition-colors text-left"
      >
        <FileCode size={14} className="text-blue-400 flex-shrink-0" />
        <span className="text-sm font-semibold text-ink">{filename}</span>
        <span className="text-[11px] text-muted/70 ml-1">{displayData.changes.length} change{displayData.changes.length !== 1 ? 's' : ''}</span>
        <div className="ml-auto flex items-center gap-2">
          {displayData.changes.length > 1 && !Object.keys(applied).length && (
            <button
              onClick={e => { e.stopPropagation(); handleApplyAll() }}
              className="text-[11px] px-2.5 py-1 bg-green-500/20 text-green-400 border border-green-500/30 rounded-lg hover:bg-green-500/30 transition-colors font-semibold"
            >
              Apply All
            </button>
          )}
          {expanded ? <ChevronUp size={13} className="text-muted/70" /> : <ChevronDown size={13} className="text-muted/70" />}
        </div>
      </button>

      {/* Changes */}
      {expanded && displayData.changes.map((change, idx) => (
        <div key={change.id} className="border-t border-border/60">
          {/* Change header */}
          <div className="flex items-start gap-3 px-4 py-2.5 bg-base/60">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <code className="text-[12px] text-blue-300 font-mono">{change.symbol?.full_path || change.symbol?.name || 'unknown'}</code>
                <ConfidenceBadge score={change.confidence} />
                {change.confidence < 7 && (
                  <span className="flex items-center gap-1 text-[11px] text-yellow-400">
                    <AlertTriangle size={10} /> Review carefully
                  </span>
                )}
              </div>
              <p className="text-[12px] text-muted mt-0.5">{change.description}</p>
            </div>
          </div>

          {/* Diff Preview */}
          <div className="border-t border-border">
            <div className="flex items-center gap-2 px-4 py-1.5 bg-base border-b border-border/60">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-muted/70">Diff Preview</span>
              <span className="text-[10px] text-faint ml-auto">
                {((change.diff || '').split('\n').filter((l: string) => l.startsWith('+') && !l.startsWith('+++'))).length} added{' · '}
                {((change.diff || '').split('\n').filter((l: string) => l.startsWith('-') && !l.startsWith('---'))).length} removed
              </span>
            </div>
            <div className="bg-base max-h-96 overflow-y-auto">
              {(change.diff || '').split('\n').map((line: string, i: number) => (
                <DiffLine key={i} line={line} />
              ))}
            </div>
          </div>

          {/* Action buttons */}
          <div className="flex items-center gap-2 px-4 py-2.5 bg-base/40 border-t border-border flex-wrap">
            {/* Left: status or apply/skip */}
            {applied[change.id] ? (
              <span className="flex items-center gap-1.5 text-[12px] text-green-400 font-semibold">
                <CheckCircle size={13} /> Applied
              </span>
            ) : skipped[change.id] ? (
              <span className="flex items-center gap-1.5 text-[12px] text-muted/70">
                <XCircle size={13} /> Skipped
              </span>
            ) : (
              <>
                <button
                  onClick={() => handleApply(change)}
                  disabled={applying[change.id]}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-green-500/20 text-green-400 border border-green-500/30 rounded-lg text-[12px] font-semibold hover:bg-green-500/30 transition-colors disabled:opacity-50"
                >
                  <CheckCircle size={12} />
                  {applying[change.id] ? 'Applying...' : 'Apply'}
                </button>
                <button
                  onClick={() => setSkipped(p => ({ ...p, [change.id]: true }))}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-surface text-muted border border-border rounded-lg text-[12px] font-semibold hover:bg-overlay transition-colors"
                >
                  <XCircle size={12} /> Skip
                </button>
              </>
            )}
            {/* Right: Preview (always visible for visual files) + Download */}
            <div className="ml-auto flex items-center gap-2 pr-1">
              {isVisualFile(filename) && (
                <LivePreview
                  code={originalCode || '// Loading...'}
                  filename={filename}
                  modifiedCode={
                    applied[change.id]
                      ? modifiedCode                       // post-apply: show result of apply
                      : getProposedCode(change)            // pre-apply: show proposed change
                  }
                />
              )}
              {!applied[change.id] && !skipped[change.id] && (
                <button
                  onClick={() => handleDownload(change)}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-surface text-muted border border-border rounded-lg text-[12px] font-semibold hover:bg-overlay transition-colors"
                  title="Download modified file"
                >
                  <Download size={12} /> Download
                </button>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

export function InlineDiffCard({ result, sessionId, onApplied }: Props) {
  const fileEntries = Object.entries(result.changes_by_file)
  const totalChanges = fileEntries.reduce((sum, [, v]) => sum + v.changes.length, 0)

  // Track how many changes have been applied to hide the risks alert
  const [appliedCount, setAppliedCount] = useState(0)
  const allApplied = appliedCount >= totalChanges

  return (
    <div className="mt-2">
      {/* Summary header */}
      <div className="flex items-center gap-2 mb-3 pb-2 border-b border-border/50">
        <span className="text-sm font-semibold text-ink">✂️ {result.summary || `${totalChanges} change${totalChanges !== 1 ? 's' : ''} ready`}</span>
      </div>

      {result.reasoning && (
        <p className="text-[12px] text-muted/70 mb-3 italic">{result.reasoning}</p>
      )}

      {/* Per-file cards */}
      {fileEntries.map(([filename, fileData]) => (
        <FileChangeCard
          key={filename}
          filename={filename}
          fileData={fileData}
          sessionId={sessionId}
          onApplied={onApplied}
          onChangeApplied={() => setAppliedCount(n => n + 1)}
        />
      ))}

      {/* Risks alert — hidden (not removed) once all changes applied */}
      {result.risks && result.risks.length > 0 && (
        <div className={`mt-2 flex items-start gap-2 px-3 py-2 bg-yellow-500/10 border border-yellow-500/20 rounded-lg transition-opacity duration-300 ${allApplied ? 'hidden' : ''}`}>
          <AlertTriangle size={13} className="text-yellow-400 mt-0.5 flex-shrink-0" />
          <div className="text-[12px] text-yellow-300">
            <strong>Risks:</strong>
            <ul className="mt-1 space-y-0.5 list-none">
              {result.risks.map((r: string, i: number) => (
                <li key={i} className="flex items-start gap-1.5">
                  <span className="mt-0.5 text-yellow-500">•</span>
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  )
}
