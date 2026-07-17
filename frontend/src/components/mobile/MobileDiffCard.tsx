/**
 * MobileDiffCard — touch-optimised diff viewer for mobile.
 * Simplified vs desktop InlineDiffCard: no line-level checkboxes,
 * large touch targets, swipeable before/after view, single Apply All tap.
 * Uses same API calls as InlineDiffCard — no new backend needed.
 * Apply state persisted to both localStorage AND backend DB (survives refresh).
 */
import React, { useState, useEffect } from 'react'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'
import type { SmartResult, SessionFile } from '../../types'

interface Props {
  result: SmartResult
  sessionId: string
  sessionFiles: SessionFile[]
  setSessionFiles: (f: SessionFile[]) => void
}

// ── localStorage helpers — mirrors InlineDiffCard exactly ────────────────────
const appliedKey  = (sid: string, cid: string) => `sai-applied:${sid}:${cid}`
const saveApplied = (sid: string, cid: string) => {
  try { localStorage.setItem(appliedKey(sid, cid), '1') } catch {}
}
const loadApplied = (sid: string, ids: string[]): Record<string, boolean> => {
  const out: Record<string, boolean> = {}
  for (const id of ids) {
    try { if (localStorage.getItem(appliedKey(sid, id)) === '1') out[id] = true } catch {}
  }
  return out
}

// ── Mini diff renderer — shows +/- lines with color ──────────────────────────
function DiffPreview({ diff }: { diff: string }) {
  if (!diff) return null
  const lines = diff.split('\n').slice(0, 60) // cap for mobile
  return (
    <div className="overflow-x-auto rounded-lg bg-[#0d0d0d] border border-border/50">
      <pre className="text-[10px] font-mono p-3 leading-5 min-w-0">
        {lines.map((line, i) => {
          const isAdd = line.startsWith('+') && !line.startsWith('+++')
          const isDel = line.startsWith('-') && !line.startsWith('---')
          const isHead = line.startsWith('@@') || line.startsWith('---') || line.startsWith('+++')
          return (
            <div
              key={i}
              className={
                isAdd  ? 'text-emerald-400 bg-emerald-400/5' :
                isDel  ? 'text-red-400 bg-red-400/5'         :
                isHead ? 'text-blue-400/60'                   :
                'text-ink/50'
              }
            >
              {line || ' '}
            </div>
          )
        })}
        {diff.split('\n').length > 60 && (
          <div className="text-muted/40 mt-1">... {diff.split('\n').length - 60} more lines</div>
        )}
      </pre>
    </div>
  )
}

// ── Single file change card ───────────────────────────────────────────────────
function FileCard({
  filename, fileData, sessionId, onApplied,
}: {
  filename: string
  fileData: { filename: string; file_id: string; changes: any[] }
  sessionId: string
  onApplied: () => void
}) {
  const [expanded, setExpanded]     = useState(false)
  const [applying, setApplying]     = useState(false)
  const [undoing, setUndoing]       = useState(false)
  const [activeChange, setActive]   = useState(0)
  const [confOpen, setConfOpen]     = useState(false)
  // Per-change applied state — keyed by change.id, same as InlineDiffCard
  const [appliedMap, setAppliedMap] = useState<Record<string, boolean>>({})

  const changes    = fileData.changes || []
  const changeIds  = changes.map((c: any) => c.id).filter(Boolean)
  const allApplied = changeIds.length > 0 && changeIds.every((id: string) => appliedMap[id])

  const currentChange = changes[activeChange]
  const diff = currentChange?.diff || ''

  // Load applied state from localStorage + backend DB on mount
  useEffect(() => {
    if (!sessionId || !changeIds.length) return
    // 1. localStorage (instant, no network)
    const local = loadApplied(sessionId, changeIds)
    if (Object.keys(local).length > 0) setAppliedMap(prev => ({ ...local, ...prev }))
    // 2. Backend DB (authoritative — survives clearing localStorage, cross-browser)
    api.surgical.getApplied(sessionId)
      .then(({ applied_ids }) => {
        const fromDB: Record<string, boolean> = {}
        for (const id of applied_ids) {
          if (changeIds.includes(id)) fromDB[id] = true
        }
        if (Object.keys(fromDB).length > 0) {
          setAppliedMap(prev => ({ ...fromDB, ...prev }))
        }
      })
      .catch(() => {})
  }, [sessionId]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleApply = async () => {
    if (applying || allApplied) return
    // Only apply QA-clean changes; QA-blocked ones must be reviewed individually.
    const applyChanges = changes.filter((c: any) => c?.qa_result?.verdict !== 'blocked')
    if (applyChanges.length === 0) {
      toast.error('All changes here are QA-flagged — review them individually before applying')
      return
    }
    setApplying(true)
    try {
      const current = await api.sessionFiles.get(sessionId, fileData.file_id)
      const result  = await api.surgical.applyAll({
        file_path: filename,
        changes: applyChanges,
        file_content: current.content,
      })
      if (result.modified_content) {
        await api.sessionFiles.update(sessionId, fileData.file_id, result.modified_content)
      }
      // Mark every applied change in localStorage + backend DB
      const newApplied: Record<string, boolean> = {}
      for (const ch of applyChanges) {
        if (ch?.id) {
          saveApplied(sessionId, ch.id)
          api.surgical.markApplied(sessionId, ch.id).catch(() => {})
          newApplied[ch.id] = true
        }
      }
      setAppliedMap(prev => ({ ...prev, ...newApplied }))
      const flagged = changes.length - applyChanges.length
      toast.success(
        `Applied ${applyChanges.length} change${applyChanges.length !== 1 ? 's' : ''} to ${filename}` +
        (flagged > 0 ? ` (${flagged} QA-flagged, skipped)` : '')
      )
      onApplied()
    } catch (e: any) {
      const msg = e?.message || 'Apply failed'
      // If already applied (code not found), auto-mark as applied
      if (msg.includes("Couldn't find") || msg.includes("could not find") || msg.includes("exact code")) {
        const newApplied: Record<string, boolean> = {}
        for (const ch of changes) {
          if (ch?.id) { saveApplied(sessionId, ch.id); newApplied[ch.id] = true }
        }
        setAppliedMap(prev => ({ ...prev, ...newApplied }))
        toast.success('Changes appear to already be applied ✓')
      } else {
        toast.error(msg)
      }
    } finally {
      setApplying(false)
    }
  }

  const handleUndo = async () => {
    if (undoing) return
    setUndoing(true)
    try {
      await api.sessionFiles.undo(sessionId, fileData.file_id)
      // Unmark all changes for this file
      for (const ch of changes) {
        if (ch?.id) {
          try { localStorage.removeItem(appliedKey(sessionId, ch.id)) } catch {}
          api.surgical.unmarkApplied(sessionId, ch.id).catch(() => {})
        }
      }
      setAppliedMap({})
      toast.success(`Undone changes to ${filename}`)
      onApplied()
    } catch (e: any) {
      toast.error(e?.message || 'Undo failed')
    } finally {
      setUndoing(false)
    }
  }

  return (
    <div className={`rounded-xl border overflow-hidden mb-2 ${
      allApplied ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-border bg-surface/60'
    }`}>
      {/* Header row */}
      <button
        className="w-full flex items-center gap-2 px-3 py-2.5 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="text-[11px] font-mono text-ink/80 truncate flex-1">{filename}</span>
        <span className="text-[10px] text-muted/60 flex-shrink-0">{changes.length} change{changes.length !== 1 ? 's' : ''}</span>
        {allApplied && <span className="text-emerald-400 text-[10px]">✓</span>}
        <span className={`text-[11px] transition-transform ${expanded ? 'rotate-90' : ''}`}>▶</span>
      </button>

      {expanded && (
        <div className="px-3 pb-3 border-t border-border/50">
          {/* Change selector if multiple */}
          {changes.length > 1 && (
            <div className="flex gap-1.5 mt-2 mb-2 flex-wrap">
              {changes.map((_: any, i: number) => (
                <button
                  key={i}
                  onClick={() => { setActive(i); setConfOpen(false) }}
                  className={`px-2 py-1 rounded-lg text-[10px] border transition-colors ${
                    i === activeChange
                      ? 'bg-[rgba(74,222,128,0.12)] border-[rgba(74,222,128,0.35)] text-[#4ade80]'
                      : 'bg-surface border-border text-muted/60'
                  }`}
                >
                  {changes[i]?.symbol?.name || `Change ${i + 1}`}
                  {appliedMap[changes[i]?.id] && <span className="ml-1 text-emerald-400">✓</span>}
                </button>
              ))}
            </div>
          )}

          {/* Change description */}
          {currentChange?.description && (
            <p className="text-[11px] text-muted/70 mt-2 mb-2">{currentChange.description}</p>
          )}

          {/* Confidence chip + expandable detail (parity with desktop ConfidenceBadge) */}
          {typeof currentChange?.confidence === 'number' && (() => {
            const score: number = currentChange.confidence
            const label = score >= 8 ? 'High' : score >= 6 ? 'Medium' : 'Low'
            const chipColor =
              score >= 8 ? 'bg-emerald-500/10 border-emerald-500/25 text-emerald-400' :
              score >= 6 ? 'bg-amber-500/10 border-amber-500/25 text-amber-400'       :
              'bg-red-500/10 border-red-500/25 text-red-400'
            const meaning = score >= 8
              ? 'High — the editor is confident this change is correct and complete.'
              : score >= 6
                ? 'Medium — the change looks right but a quick review is recommended before applying.'
                : 'Low — review carefully before applying; the editor is not certain this is fully correct.'
            const desc: string = (currentChange?.description || '').trim()
            const notes: string[] = Array.isArray(currentChange?.surgeon_notes)
              ? currentChange.surgeon_notes.filter(Boolean) : []
            return (
              <div className="mb-2">
                <button
                  onClick={() => setConfOpen(o => !o)}
                  className={`w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg border text-[11px] ${chipColor}`}
                >
                  <span>🎯</span>
                  <span className="flex-1 text-left font-medium">Confidence: {label}</span>
                  <span className="font-medium">{score}/10</span>
                  <span className={`transition-transform ${confOpen ? 'rotate-90' : ''}`}>▶</span>
                </button>
                {confOpen && (
                  <div className="mt-1 px-2.5 py-2 rounded-lg border border-border bg-surface/60 max-h-[40vh] overflow-y-auto text-[11px]">
                    <p className="text-muted mb-1.5">{meaning}</p>
                    <p className="text-muted/70 italic mb-2">
                      The editor's self-assessment of how certain it is about this specific edit —
                      separate from the QA score below.
                    </p>
                    {desc && (
                      <div className="mb-2">
                        <span className="font-semibold text-ink block mb-0.5">Why this change</span>
                        <p className="text-muted">{desc}</p>
                      </div>
                    )}
                    {notes.length > 0 && (
                      <div>
                        <span className="font-semibold text-ink block mb-0.5">Editor notes</span>
                        <ul className="space-y-1">
                          {notes.map((n, i) => (
                            <li key={i} className="flex gap-1.5 text-muted"><span>•</span><span>{n}</span></li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {score < 7 && (
                      <p className="text-amber-400 mt-1.5">
                        Below the auto-apply comfort threshold (7/10) — flagged for review.
                      </p>
                    )}
                  </div>
                )}
              </div>
            )
          })()}

          {/* QA badge */}
          {currentChange?.qa_result && (() => {
            const qa = currentChange.qa_result
            const verdict = qa?.verdict
            const score   = qa?.qa_score
            const summary = qa?.summary || ''
            return (
              <div className={`flex items-center gap-2 px-2.5 py-1.5 rounded-lg border mb-2 text-[11px] ${
                verdict === 'safe'    ? 'bg-emerald-500/10 border-emerald-500/25 text-emerald-400' :
                verdict === 'warning' ? 'bg-amber-500/10 border-amber-500/25 text-amber-400'       :
                verdict === 'blocked' ? 'bg-red-500/10 border-red-500/25 text-red-400'             :
                'bg-surface border-border text-muted/60'
              }`}>
                <span>{verdict === 'safe' ? '✅' : verdict === 'warning' ? '⚠️' : '🚫'}</span>
                <span className="flex-1">{summary}</span>
                {score && <span className="font-medium">{score}/10</span>}
              </div>
            )
          })()}

          {/* Diff preview */}
          <DiffPreview diff={diff} />
        </div>
      )}

      {/* Apply / Undo buttons */}
      <div className="px-3 pb-3 pt-1 flex gap-2">
        {!allApplied ? (
          <button
            onClick={handleApply}
            disabled={applying || currentChange?.qa_result?.verdict === 'blocked'}
            className={`flex-1 py-2.5 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2 ${
              currentChange?.qa_result?.verdict === 'blocked'
                ? 'bg-red-500/10 border border-red-500/25 text-red-400/60 cursor-not-allowed'
                : applying
                  ? 'bg-[rgba(74,222,128,0.12)] border border-[rgba(74,222,128,0.25)] text-[#4ade80]/60 cursor-wait'
                  : 'bg-[rgba(74,222,128,0.12)] border border-[rgba(74,222,128,0.35)] text-[#4ade80] hover:bg-[rgba(74,222,128,0.2)] active:scale-95'
            }`}
          >
            {applying ? (
              <>
                <span className="w-3.5 h-3.5 border-2 border-[rgba(74,222,128,0.35)] border-t-orange rounded-full animate-spin" />
                Applying...
              </>
            ) : currentChange?.qa_result?.verdict === 'blocked' ? (
              <>🚫 Blocked by QA</>
            ) : (
              <>✓ Apply {changes.length > 1 ? `All ${changes.length} Changes` : 'Change'}</>
            )}
          </button>
        ) : (
          <>
            <div className="flex-1 py-2.5 rounded-xl text-sm font-semibold text-center text-emerald-400 bg-emerald-500/10 border border-emerald-500/25">
              ✓ Applied
            </div>
            <button
              onClick={handleUndo}
              disabled={undoing}
              className="px-3 py-2.5 rounded-xl text-[12px] font-medium border border-border text-muted/60 hover:text-ink hover:border-border/80 transition-colors active:scale-95 disabled:opacity-50"
            >
              {undoing ? (
                <span className="w-3 h-3 border-2 border-muted/40 border-t-muted rounded-full animate-spin block" />
              ) : (
                'Undo'
              )}
            </button>
          </>
        )}
      </div>

      {!allApplied && currentChange?.qa_result?.verdict === 'blocked' && (
        <p className="text-[10px] text-red-400/60 text-center pb-2">
          QA blocked this change. Review on desktop for details.
        </p>
      )}
    </div>
  )
}

// ── New file card ─────────────────────────────────────────────────────────────
function NewFileMobileCard({ file, sessionId, onAdded }: { file: any; sessionId: string; onAdded: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const [saving, setSaving] = useState(false)
  const savedKey = `sai-added:${sessionId}:${file.filename}`
  const [saved, setSaved] = useState(() => {
    try { return localStorage.getItem(savedKey) === '1' } catch { return false }
  })

  const handleDownload = () => {
    const blob = new Blob([file.content || ''], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = (file.filename || 'file').split('/').pop()
    a.click()
    URL.revokeObjectURL(url)
  }

  const handleAdd = async () => {
    if (saved || saving) return
    setSaving(true)
    try {
      await api.sessionFiles.upload(sessionId, {
        filename: file.filename,
        content: file.content,
        language: file.language,
        origin: 'created',
      })
      setSaved(true)
      try { localStorage.setItem(savedKey, '1') } catch {}
      onAdded()
      toast.success(`${file.filename} added to session`)
    } catch (e: any) {
      toast.error(`Failed to add: ${e?.message || 'error'}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="rounded-xl border border-purple/25 bg-purple/5 mb-2 overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-3 py-2.5 text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="text-purple text-[11px] font-semibold">+ New</span>
        <span className="text-[11px] font-mono text-ink/80 truncate flex-1">{file.filename}</span>
        <span className={`text-[11px] transition-transform ${expanded ? 'rotate-90' : ''}`}>▶</span>
      </button>
      {expanded && (
        <div className="px-3 pb-3 border-t border-border/50">
          {file.summary && <p className="text-[11px] text-muted/70 mt-2 mb-2">{file.summary}</p>}
          <div className="overflow-x-auto rounded-lg bg-[#0d0d0d] border border-border/50 max-h-48 overflow-y-auto">
            <pre className="text-[10px] font-mono p-3 leading-5 text-ink/70">
              {(file.content || '').slice(0, 2000)}
              {(file.content || '').length > 2000 && '\n... (truncated — download to see full file)'}
            </pre>
          </div>
          <div className="flex items-center gap-2 mt-2.5">
            <button
              onClick={handleAdd}
              disabled={saving || saved}
              className={`flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg text-[12px] font-semibold transition-colors ${
                saved
                  ? 'bg-success/15 text-success border border-success/30'
                  : 'bg-accent/15 text-accent border border-accent/30 hover:bg-accent/25 disabled:opacity-60'
              }`}
            >
              {saved ? '✓ Added to session' : saving ? 'Adding…' : '+ Add to session'}
            </button>
            <button
              onClick={handleDownload}
              className="flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-[12px] font-medium bg-overlay text-ink border border-border hover:bg-overlay/80 transition-colors"
            >
              ↓ Download
            </button>
          </div>
          {/* Footer collapse — parity with desktop so the card can be closed from the bottom */}
          <button
            onClick={() => setExpanded(false)}
            className="w-full mt-2 py-1.5 text-[11px] text-muted/70 hover:text-ink flex items-center justify-center gap-1 transition-colors"
          >
            ▲ Collapse
          </button>
        </div>
      )}
    </div>
  )
}

// ── Main export ───────────────────────────────────────────────────────────────
export function MobileDiffCard({ result, sessionId, sessionFiles, setSessionFiles }: Props) {
  const [allApplied, setAllApplied] = useState(false)

  const files      = Object.entries(result.changes_by_file || {})
  const newFiles   = result.new_files || []
  const totalFiles = files.length + newFiles.length

  if (totalFiles === 0) return null

  const handleApplied = async () => {
    // Refresh session files after any apply
    try {
      const fresh = await api.sessionFiles.list(sessionId)
      setSessionFiles(fresh)
    } catch {}
  }

  return (
    <div className="w-full mt-1">
      {/* Summary header */}
      <div className="flex items-center gap-2 mb-2">
        <span className="text-[11px] font-medium text-ink/60">
          ✂️ {files.length} file{files.length !== 1 ? 's' : ''} changed
          {newFiles.length > 0 && `, ${newFiles.length} new`}
        </span>
      </div>

      {/* File cards */}
      {files.map(([filename, fileData]) => (
        <FileCard
          key={filename}
          filename={filename}
          fileData={fileData as any}
          sessionId={sessionId}
          onApplied={handleApplied}
        />
      ))}

      {/* New file cards */}
      {newFiles.map((f: any, i: number) => (
        <NewFileMobileCard key={i} file={f} sessionId={sessionId} onAdded={handleApplied} />
      ))}
    </div>
  )
}
