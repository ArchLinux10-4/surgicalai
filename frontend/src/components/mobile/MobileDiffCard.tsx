/**
 * MobileDiffCard — mobile-first diff card for SurgicalAI.
 * Used exclusively in MobileChat. Desktop InlineDiffCard is untouched.
 *
 * Layout goals:
 *  • Badge row wraps — never overflows the screen
 *  • Apply = full-width 48px touch target (green pill)
 *  • Skip + Download = two-column secondary row
 *  • Preview = full-width below buttons, only for visual files
 *  • Diff viewer = horizontally scrollable, compact font, colour-coded
 */
import React, { useState, useEffect } from 'react'
import {
  FileCode, ChevronDown, ChevronUp, CheckCircle, XCircle,
  Download, RotateCcw, Eye, EyeOff, Sparkles,
} from 'lucide-react'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'
import { useAppStore } from '../../stores/appStore'
import { useThemeStore } from '../../stores/themeStore'
import { LivePreview, isVisualFile } from '../LivePreview'
import type { SmartResult, QAResult } from '../../types'

// ─── localStorage helpers (same keys as desktop so Apply state is shared) ────
const appliedKey = (s: string, c: string) => `sai-applied:${s}:${c}`
const skippedKey = (s: string, c: string) => `sai-skipped:${s}:${c}`

const loadApplied = (sessionId: string, ids: string[]) => {
  const out: Record<string, boolean> = {}
  for (const id of ids) {
    if (localStorage.getItem(appliedKey(sessionId, id)) === '1') out[id] = true
  }
  return out
}

const loadSkipped = (sessionId: string, ids: string[]) => {
  const out: Record<string, boolean> = {}
  for (const id of ids) {
    if (localStorage.getItem(skippedKey(sessionId, id)) === '1') out[id] = true
  }
  return out
}

const saveApplied = (sessionId: string, changeId: string) => {
  try {
    localStorage.setItem(appliedKey(sessionId, changeId), '1')
    localStorage.removeItem(skippedKey(sessionId, changeId))
  } catch {}
}

const saveSkipped = (sessionId: string, changeId: string) => {
  try { localStorage.setItem(skippedKey(sessionId, changeId), '1') } catch {}
}

// ─── Compact coloured diff renderer (mobile-optimised) ────────────────────
function MobileDiffView({ diff }: { diff: string }) {
  const { theme } = useThemeStore()
  const isLight = theme === 'light'

  const lines = diff.split('\n')
  return (
    <div style={{ overflowX: 'auto', WebkitOverflowScrolling: 'touch' }}>
      <div style={{
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: '11.5px',
        lineHeight: '1.6',
        minWidth: 0,
      }}>
        {lines.map((line, i) => {
          // Skip file header lines
          if (line.startsWith('---') || line.startsWith('+++')) return null

          if (line.startsWith('@@')) {
            return (
              <div key={i} style={{
                padding: '2px 10px',
                background: isLight ? 'rgba(9,105,218,0.07)' : 'rgba(96,165,250,0.07)',
                color: isLight ? '#0969da' : '#60a5fa',
                whiteSpace: 'pre',
              }}>{line}</div>
            )
          }
          if (line.startsWith('+')) {
            return (
              <div key={i} style={{
                padding: '1px 10px',
                background: isLight ? 'rgba(26,127,55,0.10)' : 'rgba(74,222,128,0.09)',
                color: isLight ? '#14532d' : '#bbf7d0',
                whiteSpace: 'pre',
                display: 'flex',
                gap: 8,
              }}>
                <span style={{ color: isLight ? '#16a34a' : '#4ade80', userSelect: 'none', flexShrink: 0 }}>+</span>
                <span>{line.slice(1)}</span>
              </div>
            )
          }
          if (line.startsWith('-')) {
            return (
              <div key={i} style={{
                padding: '1px 10px',
                background: isLight ? 'rgba(207,34,46,0.09)' : 'rgba(248,113,113,0.09)',
                color: isLight ? '#7f1d1d' : '#fecaca',
                whiteSpace: 'pre',
                display: 'flex',
                gap: 8,
              }}>
                <span style={{ color: isLight ? '#dc2626' : '#f87171', userSelect: 'none', flexShrink: 0 }}>−</span>
                <span>{line.slice(1)}</span>
              </div>
            )
          }
          return (
            <div key={i} style={{
              padding: '1px 10px',
              color: isLight ? '#475569' : '#94a3b8',
              whiteSpace: 'pre',
            }}>{line}</div>
          )
        })}
      </div>
    </div>
  )
}

// ─── QA badge (tap to see summary) ───────────────────────────────────────
function QABadge({ qa }: { qa: QAResult }) {
  const [open, setOpen] = useState(false)
  const styles: Record<string, string> = {
    safe:    'text-success bg-success/15 border-success/30',
    warning: 'text-warning bg-warning/15 border-warning/30',
    blocked: 'text-danger bg-danger/15 border-danger/30',
    skipped: 'text-muted bg-muted/10 border-muted/30',
  }
  const icons: Record<string, string> = { safe: '✅', warning: '⚠️', blocked: '🚫', skipped: '⏭' }
  const color = styles[qa.verdict] || styles.skipped
  const icon = icons[qa.verdict] || '⏭'
  const score = qa.qa_score !== null ? ` ${qa.qa_score}/10` : ''

  return (
    <span className="relative">
      <button
        onClick={() => setOpen(o => !o)}
        className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border ${color}`}
      >
        QA {icon}{score}
      </button>
      {open && (
        <div
          className="absolute top-full left-0 mt-1.5 z-50 w-[260px] bg-surface border border-border rounded-xl shadow-2xl p-3 text-[12px]"
          style={{ background: 'rgb(var(--c-surface))' }}
        >
          <div className="flex items-center justify-between mb-1.5">
            <span className="font-semibold text-ink">QA Report</span>
            <button onClick={() => setOpen(false)} className="text-muted text-lg leading-none">×</button>
          </div>
          <p className="text-muted leading-snug">{qa.summary || 'No summary'}</p>
          {[...qa.import_issues, ...qa.downstream_risks, ...qa.type_errors].filter(Boolean).map((r, i) => (
            <p key={i} className="mt-1 text-warning flex gap-1.5"><span>•</span><span>{r}</span></p>
          ))}
        </div>
      )}
    </span>
  )
}

// ─── Confidence badge ─────────────────────────────────────────────────────
function ConfBadge({ score }: { score: number }) {
  const color = score >= 8 ? 'text-success bg-success/15 border-success/30'
    : score >= 6 ? 'text-warning bg-warning/15 border-warning/30'
    : 'text-danger bg-danger/15 border-danger/30'
  const label = score >= 8 ? 'High' : score >= 6 ? 'Med' : 'Low'
  return (
    <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border ${color}`}>
      {label} {score}/10
    </span>
  )
}

// ─── Per-file card ────────────────────────────────────────────────────────
function MobileFileCard({
  filename, fileData, sessionId,
}: {
  filename: string
  fileData: { filename: string; file_id: string; changes: any[] }
  sessionId: string
}) {
  const { setSessionFiles } = useAppStore()

  // Filter ghost diffs
  const realChanges = fileData.changes.filter((c: any) => {
    if (!c.diff) return false
    const lines = c.diff.split('\n')
    return lines.some((l: string) => l.startsWith('+') && !l.startsWith('+++')) ||
           lines.some((l: string) => l.startsWith('-') && !l.startsWith('---'))
  })
  const changeIds = realChanges.map((c: any) => c.id)

  const [checked, setChecked] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(realChanges.map((c: any) => [c.id, c.qa_result?.verdict !== 'blocked']))
  )
  const [diffExpanded, setDiffExpanded] = useState<Record<string, boolean>>({})
  const [showPreview, setShowPreview] = useState(false)
  const [applying, setApplying] = useState(false)
  const [undoing, setUndoing] = useState<Record<string, boolean>>({})
  const [applied, setApplied] = useState<Record<string, boolean>>(() => loadApplied(sessionId, changeIds))
  const [skipped, setSkipped] = useState<Record<string, boolean>>(() => loadSkipped(sessionId, changeIds))
  const [originalCode, setOriginalCode] = useState('')
  const [modifiedCode, setModifiedCode] = useState<string | undefined>(undefined)

  useEffect(() => {
    if (!isVisualFile(filename)) return
    api.sessionFiles.get(sessionId, fileData.file_id)
      .then(f => setOriginalCode(f.content)).catch(() => {})
  }, [sessionId, fileData.file_id, filename])

  if (realChanges.length === 0) return null

  const pendingChanges = realChanges.filter((c: any) => !applied[c.id] && !skipped[c.id])
  const selectedChanges = pendingChanges.filter((c: any) => checked[c.id])
  const allDone = realChanges.every((c: any) => applied[c.id] || skipped[c.id])

  const markApplied = (changeId: string) => {
    saveApplied(sessionId, changeId)
    setApplied(p => ({ ...p, [changeId]: true }))
    setSkipped(p => { const n = { ...p }; delete n[changeId]; return n })
  }

  const handleApply = async () => {
    if (selectedChanges.length === 0) return
    setApplying(true)
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, fileData.file_id)
      if (!originalCode) setOriginalCode(fileData2.content)
      let result: any
      if (selectedChanges.length === 1) {
        result = await api.surgical.apply({ file_path: filename, changes: selectedChanges, file_content: fileData2.content })
      } else {
        result = await api.surgical.applyAll({ file_path: filename, changes: selectedChanges, file_content: fileData2.content })
      }
      const newContent = result.modified_content || ''
      if (newContent) {
        try {
          await api.sessionFiles.update(sessionId, fileData.file_id, newContent)
          api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
        } catch {}
      }
      if (result.cloud_mode || result.modified_content) {
        const blob = new Blob([newContent], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url; a.download = filename; a.click()
        URL.revokeObjectURL(url)
        toast.success(`✅ Applied & downloaded ${filename}`)
        setModifiedCode(newContent)
      } else {
        toast.success(`Applied ${selectedChanges.length} change${selectedChanges.length !== 1 ? 's' : ''} to ${filename}`)
        setModifiedCode(newContent)
      }
      for (const c of selectedChanges) markApplied(c.id)
    } catch (e: any) {
      const msg: string = e?.message || 'Apply failed'
      if (msg.includes("Couldn't find") || msg.includes("could not find") || msg.includes("exact code")) {
        for (const c of selectedChanges) {
          saveApplied(sessionId, c.id)
          setApplied(p => ({ ...p, [c.id]: true }))
        }
        toast.success('Changes appear already applied ✓')
      } else {
        toast.error(msg)
      }
    } finally {
      setApplying(false)
    }
  }

  const handleUndo = async (change: any) => {
    setUndoing(p => ({ ...p, [change.id]: true }))
    try {
      await api.sessionFiles.undo(sessionId, fileData.file_id)
      localStorage.removeItem(appliedKey(sessionId, change.id))
      localStorage.removeItem(skippedKey(sessionId, change.id))
      setApplied(p => { const n = { ...p }; delete n[change.id]; return n })
      setSkipped(p => { const n = { ...p }; delete n[change.id]; return n })
      setModifiedCode(undefined)
      toast.success(`↩ Reverted ${filename}`)
    } catch (e: any) {
      toast.error(e.message || 'Undo failed')
    } finally {
      setUndoing(p => ({ ...p, [change.id]: false }))
    }
  }

  const handleDownload = async () => {
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, fileData.file_id)
      const toApply = selectedChanges.length > 0 ? selectedChanges : pendingChanges
      if (toApply.length === 0) {
        const blob = new Blob([fileData2.content], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a'); a.href = url; a.download = filename; a.click()
        URL.revokeObjectURL(url)
        toast.success(`Downloaded ${filename}`)
        return
      }
      const result: any = toApply.length === 1
        ? await api.surgical.apply({ file_path: filename, changes: toApply, file_content: fileData2.content })
        : await api.surgical.applyAll({ file_path: filename, changes: toApply, file_content: fileData2.content })
      const content = result.modified_content || fileData2.content
      const blob = new Blob([content], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url; a.download = filename; a.click()
      URL.revokeObjectURL(url)
      toast.success(`Downloaded ${filename}`)
    } catch (e: any) {
      toast.error(e.message || 'Download failed')
    }
  }

  const skipAll = () => {
    for (const c of pendingChanges) saveSkipped(sessionId, c.id)
    setSkipped(p => ({ ...p, ...Object.fromEntries(pendingChanges.map((c: any) => [c.id, true])) }))
    setChecked(Object.fromEntries(pendingChanges.map((c: any) => [c.id, false])))
  }

  const visualFile = isVisualFile(filename)

  return (
    <div className="border border-border rounded-2xl overflow-hidden mb-3 bg-surface/30">
      {/* ── File header ──────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 px-3 py-2.5 bg-surface/80 border-b border-border">
        <FileCode size={13} className="text-accent flex-shrink-0" />
        <span className="text-[13px] font-semibold text-ink flex-1 min-w-0 truncate">{filename}</span>
        <span className="text-[11px] text-muted flex-shrink-0">
          {realChanges.length} change{realChanges.length !== 1 ? 's' : ''}
        </span>
        {allDone && (
          <span className="flex items-center gap-1 text-[11px] text-success font-semibold">
            <CheckCircle size={12} /> Done
          </span>
        )}
      </div>

      {/* ── Change rows ──────────────────────────────────────────────── */}
      {realChanges.map((change: any) => {
        const isBlocked = change.qa_result?.verdict === 'blocked'
        const isApplied = applied[change.id]
        const isSkipped = !isApplied && skipped[change.id]
        const isChecked = !!checked[change.id] && !isBlocked && !isSkipped
        const isExpanded = !!diffExpanded[change.id]

        const addCount = (change.diff || '').split('\n')
          .filter((l: string) => l.startsWith('+') && !l.startsWith('+++') ).length
        const remCount = (change.diff || '').split('\n')
          .filter((l: string) => l.startsWith('-') && !l.startsWith('---')).length

        return (
          <div key={change.id} className={`border-b border-border last:border-b-0 ${isSkipped ? 'opacity-50' : ''}`}>
            {/* ── Row: checkbox + symbol + stat badges ─────────────── */}
            <div className="flex items-start gap-2.5 px-3 pt-2.5 pb-1.5">
              {/* Checkbox */}
              {!isApplied && !isSkipped && (
                <button
                  onClick={() => !isBlocked && setChecked(p => ({ ...p, [change.id]: !p[change.id] }))}
                  disabled={isBlocked}
                  className={`mt-0.5 flex-shrink-0 w-5 h-5 rounded-md border-2 flex items-center justify-center transition-colors ${
                    isChecked
                      ? 'bg-accent border-accent'
                      : isBlocked
                        ? 'border-danger/40 bg-danger/10'
                        : 'border-border bg-transparent'
                  }`}
                  aria-label={isChecked ? 'Deselect' : 'Select'}
                >
                  {isChecked && <CheckCircle size={12} className="text-white" />}
                  {isBlocked && <XCircle size={12} className="text-danger" />}
                </button>
              )}
              {isApplied && (
                <span className="mt-0.5 flex-shrink-0 w-5 h-5 rounded-md bg-success/20 border border-success/40 flex items-center justify-center">
                  <CheckCircle size={11} className="text-success" />
                </span>
              )}
              {isSkipped && (
                <span className="mt-0.5 flex-shrink-0 w-5 h-5 rounded-md bg-muted/10 border border-muted/30 flex items-center justify-center">
                  <span className="text-[8px] text-muted font-bold">—</span>
                </span>
              )}

              {/* Symbol name + stats */}
              <div className="flex-1 min-w-0">
                <p className="text-[13px] font-medium text-ink truncate leading-tight">
                  {change.symbol || change.description || 'change'}
                </p>
                {/* Badges — flex-wrap so they never overflow */}
                <div className="flex flex-wrap gap-1.5 mt-1.5">
                  {/* +/- count chips */}
                  {addCount > 0 && (
                    <span className="text-[11px] font-semibold px-1.5 py-0.5 rounded bg-success/10 text-success">
                      +{addCount}
                    </span>
                  )}
                  {remCount > 0 && (
                    <span className="text-[11px] font-semibold px-1.5 py-0.5 rounded bg-danger/10 text-danger">
                      −{remCount}
                    </span>
                  )}
                  {/* Confidence */}
                  {change.confidence !== undefined && <ConfBadge score={change.confidence} />}
                  {/* QA */}
                  {change.qa_result && <QABadge qa={change.qa_result} />}
                  {/* Applied / Skipped status badges */}
                  {isApplied && (
                    <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full border text-success bg-success/15 border-success/30">
                      Applied ✓
                    </span>
                  )}
                  {isSkipped && (
                    <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full border text-muted bg-muted/10 border-muted/30">
                      Skipped
                    </span>
                  )}
                  {isBlocked && !isApplied && !isSkipped && (
                    <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full border text-danger bg-danger/15 border-danger/30">
                      QA blocked
                    </span>
                  )}
                </div>
              </div>

              {/* Expand/collapse diff */}
              <button
                onClick={() => setDiffExpanded(p => ({ ...p, [change.id]: !p[change.id] }))}
                className="flex-shrink-0 w-8 h-8 flex items-center justify-center rounded-lg text-muted active:bg-overlay mt-0.5"
                aria-label={isExpanded ? 'Collapse diff' : 'Expand diff'}
              >
                {isExpanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
              </button>
            </div>

            {/* ── Diff viewer ──────────────────────────────────────── */}
            {isExpanded && change.diff && (
              <div className="border-t border-border/60 bg-base/60">
                <MobileDiffView diff={change.diff} />
              </div>
            )}

            {/* ── Per-change undo (shown when applied) ─────────────── */}
            {isApplied && (
              <div className="px-3 pb-2.5 pt-1">
                <button
                  onClick={() => handleUndo(change)}
                  disabled={undoing[change.id]}
                  className="flex items-center gap-1.5 text-[12px] text-muted active:text-ink h-8 px-3 rounded-lg border border-border active:bg-overlay"
                >
                  <RotateCcw size={13} className={undoing[change.id] ? 'animate-spin' : ''} />
                  {undoing[change.id] ? 'Reverting…' : 'Undo'}
                </button>
              </div>
            )}
          </div>
        )
      })}

      {/* ── Action buttons (only shown when there are pending changes) ── */}
      {!allDone && (
        <div className="px-3 py-3 space-y-2 border-t border-border bg-surface/40">
          {/* Apply Selected — full width, prominent */}
          <button
            onClick={handleApply}
            disabled={applying || selectedChanges.length === 0}
            data-apply-btn
            className={`w-full h-12 rounded-xl flex items-center justify-center gap-2 text-[14px] font-semibold transition-all active:scale-[0.98] ${
              selectedChanges.length === 0
                ? 'bg-surface border border-border text-muted cursor-not-allowed'
                : 'bg-success text-white shadow-sm active:bg-success/90'
            }`}
          >
            {applying ? (
              <><Sparkles size={15} className="animate-spin" /> Applying…</>
            ) : (
              <><CheckCircle size={15} /> Apply{selectedChanges.length > 0 ? ` (${selectedChanges.length})` : ''}</>
            )}
          </button>

          {/* Download + Skip — side by side */}
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={handleDownload}
              className="h-11 rounded-xl flex items-center justify-center gap-1.5 text-[13px] font-medium text-muted border border-border bg-surface active:bg-overlay transition-colors"
            >
              <Download size={14} />
              Download
            </button>
            <button
              onClick={skipAll}
              disabled={pendingChanges.length === 0}
              className="h-11 rounded-xl flex items-center justify-center gap-1.5 text-[13px] font-medium text-muted border border-border bg-surface active:bg-overlay transition-colors disabled:opacity-40"
            >
              <XCircle size={14} />
              Skip all
            </button>
          </div>

          {/* Preview — full width, only for visual files */}
          {visualFile && (
            <button
              onClick={() => setShowPreview(p => !p)}
              className="w-full h-11 rounded-xl flex items-center justify-center gap-1.5 text-[13px] font-medium text-ink border border-border bg-surface active:bg-overlay transition-colors"
            >
              {showPreview ? <EyeOff size={14} /> : <Eye size={14} />}
              {showPreview ? 'Hide preview' : '👁 Preview'}
            </button>
          )}
        </div>
      )}

      {/* ── Preview panel ─────────────────────────────────────────────── */}
      {visualFile && showPreview && (
        <div className="border-t border-border" style={{ height: 360 }}>
          <LivePreview
            code={modifiedCode ?? originalCode}
            modifiedCode={modifiedCode}
            filename={filename}
            sessionId={sessionId}
            fileId={fileData.file_id}
          />
        </div>
      )}
    </div>
  )
}

// ─── Root export — wraps all files in the SmartResult ─────────────────────
interface Props {
  result: SmartResult
  sessionId: string
}

export function MobileDiffCard({ result, sessionId }: Props) {
  const byFile = result.changes_by_file || {}
  const files = Object.values(byFile)

  if (files.length === 0) {
    return (
      <div className="px-3 py-3 text-[13px] text-muted rounded-xl border border-border bg-surface/30">
        No changes proposed.
      </div>
    )
  }

  return (
    <div>
      {files.map(fd => (
        <MobileFileCard
          key={fd.filename}
          filename={fd.filename}
          fileData={fd as { filename: string; file_id: string; changes: any[] }}
          sessionId={sessionId}
        />
      ))}
    </div>
  )
}
