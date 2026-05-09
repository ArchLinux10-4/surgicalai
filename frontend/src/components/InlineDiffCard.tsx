import React, { useState, useEffect } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vscDarkPlus, oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { useThemeStore } from '../stores/themeStore'
import { CheckCircle, XCircle, Download, ChevronDown, ChevronUp, AlertTriangle, FileCode, RotateCcw, SkipForward } from 'lucide-react'
import { api } from '../api/client'
import { TestRunnerPanel } from './TestRunnerPanel'
import { toast } from '../lib/toast'
import type { SmartResult, QAResult } from '../types'
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


function QABadge({ qa }: { qa: QAResult }) {
  const [expanded, setExpanded] = useState(false)
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
  const issues = [
    ...qa.import_issues,
    ...qa.downstream_risks,
    ...qa.type_errors,
    ...(qa.plan_deviation ? [qa.plan_deviation] : []),
  ].filter(Boolean)
  return (
    <span className="relative">
      <button
        onClick={() => setExpanded(e => !e)}
        className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border cursor-pointer ${color}`}
        title={qa.summary}
      >
        QA {icon}{score}
      </button>
      {expanded && (
        <div className="absolute left-0 top-6 z-50 w-72 bg-[var(--color-surface)] border border-[var(--color-border)] rounded-lg shadow-xl p-3 text-[12px]">
          <div className="flex items-center justify-between mb-2">
            <span className="font-semibold">QA Report</span>
            <button onClick={() => setExpanded(false)} className="text-muted hover:text-ink">✕</button>
          </div>
          <p className="text-muted mb-2">{qa.summary || 'No summary'}</p>
          {issues.length > 0 && (
            <ul className="space-y-1">
              {issues.map((issue, i) => (
                <li key={i} className="flex gap-1.5 text-warning">
                  <span>•</span><span>{issue}</span>
                </li>
              ))}
            </ul>
          )}
          {qa.skipped_reason && (
            <p className="text-muted mt-1 italic">Skipped: {qa.skipped_reason}</p>
          )}
        </div>
      )}
    </span>
  )
}

function BlastRadius({ change }: { change: any }) {
  const [open, setOpen] = useState(false)
  const risks: string[] = [
    ...(change.qa_result?.downstream_risks || []),
    ...(change.qa_result?.import_issues || []),
    ...(change.qa_result?.type_errors || []),
  ].filter(Boolean)
  if (!risks.length) return null
  return (
    <span className="relative">
      <button
        onClick={() => setOpen(o => !o)}
        className="text-[11px] font-semibold px-2 py-0.5 rounded-full border text-amber-400 bg-amber-500/10 border-amber-500/30 cursor-pointer"
        title="Blast radius"
      >
        🎯 {risks.length} risk{risks.length !== 1 ? 's' : ''}
      </button>
      {open && (
        <div className="absolute left-0 top-6 z-50 w-72 bg-[var(--color-surface)] border border-[var(--color-border)] rounded-lg shadow-xl p-3 text-[12px]">
          <div className="flex items-center justify-between mb-2">
            <span className="font-semibold text-amber-400">🎯 Blast Radius</span>
            <button onClick={() => setOpen(false)} className="text-muted hover:text-ink">×</button>
          </div>
          <ul className="space-y-1">
            {risks.map((r, i) => (
              <li key={i} className="flex gap-1.5 text-amber-300/90 text-[11px]">
                <span>•</span><span>{r}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </span>
  )
}

function ConfidenceBadge({ score }: { score: number }) {
  const color = score >= 8 ? 'text-success bg-success/15 border-success/30'
    : score >= 6 ? 'text-warning bg-warning/15 border-warning/30'
    : 'text-danger bg-danger/15 border-danger/30'
  const label = score >= 8 ? 'High' : score >= 6 ? 'Medium' : 'Low'
  return (
    <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border ${color}`}>
      {label} {score}/10
    </span>
  )
}

/** Parse the first @@ hunk header to get the starting line number in the new file */
function parseDiffStartLine(diff: string): number | null {
  const match = diff.match(/@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/)
  return match ? parseInt(match[1], 10) : null
}

// ── Language detection from file extension ─────────────────────────────────
const EXT_TO_LANG: Record<string, string> = {
  py: 'python', js: 'javascript', jsx: 'jsx', ts: 'typescript', tsx: 'tsx',
  go: 'go', rs: 'rust', java: 'java', cpp: 'cpp', cc: 'cpp', c: 'c',
  cs: 'csharp', rb: 'ruby', php: 'php', swift: 'swift', kt: 'kotlin',
  sh: 'bash', bash: 'bash', zsh: 'bash', json: 'json', yaml: 'yaml',
  yml: 'yaml', toml: 'toml', md: 'markdown', html: 'markup', htm: 'markup', css: 'css',
  scss: 'scss', sql: 'sql', xml: 'xml',
}

function getLangFromFilename(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  return EXT_TO_LANG[ext] || 'text'
}

// ── collapseDiff: trim context lines to +-CONTEXT around changes ─────────────
const DIFF_CONTEXT_LINES = 5

function collapseDiff(diff: string): { display: string; hiddenCount: number } {
  const lines = diff.split('\n')
  const changed = new Set<number>()
  lines.forEach((line, i) => {
    const isAdd    = line.startsWith('+') && !line.startsWith('+++')
    const isRemove = line.startsWith('-') && !line.startsWith('---')
    if (isAdd || isRemove) changed.add(i)
  })
  if (changed.size === 0) return { display: diff, hiddenCount: 0 }

  // Build visible set
  const visible = new Set<number>()
  lines.forEach((line, i) => {
    const isMeta = line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')
    const isChange = (line.startsWith('+') && !line.startsWith('+++')) ||
                     (line.startsWith('-') && !line.startsWith('---'))
    if (isMeta || isChange) { visible.add(i); return }
    for (const ci of changed) {
      if (Math.abs(i - ci) <= DIFF_CONTEXT_LINES) { visible.add(i); break }
    }
  })

  const hiddenCount = lines.length - visible.size
  if (hiddenCount <= DIFF_CONTEXT_LINES) return { display: diff, hiddenCount: 0 }

  // Build collapsed string — runs of visible lines separated by @@ ...N lines... @@
  const result: string[] = []
  let i = 0
  while (i < lines.length) {
    if (visible.has(i)) {
      result.push(lines[i]); i++
    } else {
      let gapStart = i
      while (i < lines.length && !visible.has(i)) i++
      result.push(`@@ ...${i - gapStart} unchanged lines... @@`)
    }
  }
  return { display: result.join('\n'), hiddenCount }
}

// ── DiffBlock: GitHub-style dual-gutter diff renderer ─────────────────────
type DiffRowType = 'add' | 'remove' | 'context' | 'hunk' | 'header'
interface DiffRow {
  type: DiffRowType
  oldLine: number | null
  newLine: number | null
  code: string
}

function parseDiffRows(raw: string): DiffRow[] {
  const rows: DiffRow[] = []
  let oldLine = 0
  let newLine = 0
  for (const line of raw.split('\n')) {
    if (line.startsWith('---') || line.startsWith('+++')) {
      rows.push({ type: 'header', oldLine: null, newLine: null, code: line })
    } else if (line.startsWith('@@')) {
      const m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/)
      if (m) { oldLine = parseInt(m[1], 10); newLine = parseInt(m[2], 10) }
      rows.push({ type: 'hunk', oldLine: null, newLine: null, code: line })
    } else if (line.startsWith('+')) {
      rows.push({ type: 'add', oldLine: null, newLine: newLine, code: line.slice(1) })
      newLine++
    } else if (line.startsWith('-')) {
      rows.push({ type: 'remove', oldLine: oldLine, newLine: null, code: line.slice(1) })
      oldLine++
    } else {
      if (line === '' && rows.length > 0 && rows[rows.length - 1].code === '') continue
      rows.push({ type: 'context', oldLine: oldLine, newLine: newLine, code: line })
      oldLine++; newLine++
    }
  }
  return rows
}

function DiffBlock({ diff, language: _language }: { diff: string; language: string }) {
  const { theme } = useThemeStore()
  const isLight = theme === 'light'
  const [showAll, setShowAll] = useState(false)

  const { display: displayDiff, hiddenCount: hiddenLineCount } = showAll
    ? { display: diff, hiddenCount: 0 }
    : collapseDiff(diff)

  const rows = parseDiffRows(displayDiff)

  const tk = {
    add: {
      rowBg:    isLight ? 'rgba(26,127,55,0.10)'   : 'rgba(74,222,128,0.09)',
      gutterBg: isLight ? 'rgba(26,127,55,0.18)'   : 'rgba(74,222,128,0.16)',
      numClr:   isLight ? '#16a34a'                : '#4ade80',
      codeClr:  isLight ? '#14532d'                : '#bbf7d0',
      marker:   isLight ? '#16a34a'                : '#4ade80',
    },
    remove: {
      rowBg:    isLight ? 'rgba(207,34,46,0.09)'   : 'rgba(248,113,113,0.09)',
      gutterBg: isLight ? 'rgba(207,34,46,0.17)'   : 'rgba(248,113,113,0.16)',
      numClr:   isLight ? '#dc2626'                : '#f87171',
      codeClr:  isLight ? '#7f1d1d'                : '#fecaca',
      marker:   isLight ? '#dc2626'                : '#f87171',
    },
    hunk: {
      rowBg:    isLight ? 'rgba(9,105,218,0.07)'   : 'rgba(96,165,250,0.07)',
      gutterBg: isLight ? 'rgba(9,105,218,0.13)'   : 'rgba(96,165,250,0.13)',
      numClr:   isLight ? '#0969da'                : '#60a5fa',
      codeClr:  isLight ? '#0969da'                : '#93c5fd',
      marker:   '' as string,
    },
    context: {
      rowBg:    'transparent' as string,
      gutterBg: isLight ? 'rgba(0,0,0,0.03)'       : 'rgba(255,255,255,0.03)',
      numClr:   isLight ? '#94a3b8'                : '#475569',
      codeClr:  isLight ? '#1e293b'                : '#e2e8f0',
      marker:   'transparent' as string,
    },
    header: {
      rowBg:    isLight ? 'rgba(0,0,0,0.04)'       : 'rgba(255,255,255,0.04)',
      gutterBg: isLight ? 'rgba(0,0,0,0.04)'       : 'rgba(255,255,255,0.04)',
      numClr:   isLight ? '#94a3b8'                : '#475569',
      codeClr:  isLight ? '#64748b'                : '#94a3b8',
      marker:   'transparent' as string,
    },
  }

  const mono: React.CSSProperties = {
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
    fontSize: '12px',
    lineHeight: '1.65',
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <div style={{ ...mono, minWidth: 0 }}>
        {rows.map((row, i) => {
          const t = tk[row.type]
          const marker = row.type === 'add' ? '+' : row.type === 'remove' ? '-' : ' '

          if (row.type === 'hunk') {
            const ctxMatch = row.code.match(/@@ [^@]+ @@ (.+)/)
            const ctx = ctxMatch?.[1]?.trim()
            return (
              <div key={i} style={{ display: 'flex', backgroundColor: t.rowBg }}>
                <span style={{ width: 44, minWidth: 44, textAlign: 'right', padding: '0 8px', backgroundColor: t.gutterBg, color: t.numClr, userSelect: 'none' }}>...</span>
                <span style={{ width: 44, minWidth: 44, textAlign: 'right', padding: '0 8px', backgroundColor: t.gutterBg, color: t.numClr, userSelect: 'none' }}>...</span>
                <span style={{ width: 18, minWidth: 18, textAlign: 'center', padding: '0 2px', userSelect: 'none' }} />
                <span style={{ flex: 1, padding: '0 10px', color: t.codeClr, whiteSpace: 'pre' }}>
                  {row.code}
                  {ctx && <span style={{ color: isLight ? '#94a3b8' : '#64748b', fontStyle: 'italic', marginLeft: 8 }}>{ctx}</span>}
                </span>
              </div>
            )
          }

          if (row.type === 'header') {
            return (
              <div key={i} style={{ display: 'flex', backgroundColor: t.rowBg }}>
                <span style={{ width: 44, minWidth: 44, backgroundColor: t.gutterBg }} />
                <span style={{ width: 44, minWidth: 44, backgroundColor: t.gutterBg }} />
                <span style={{ width: 18, minWidth: 18 }} />
                <span style={{ flex: 1, padding: '0 10px', color: t.codeClr, whiteSpace: 'pre', fontStyle: 'italic' }}>{row.code}</span>
              </div>
            )
          }

          return (
            <div key={i} style={{ display: 'flex', backgroundColor: t.rowBg }}>
              <span style={{ width: 44, minWidth: 44, textAlign: 'right', padding: '0 8px', backgroundColor: t.gutterBg, color: t.numClr, userSelect: 'none' }}>
                {row.oldLine !== null ? row.oldLine : ''}
              </span>
              <span style={{ width: 44, minWidth: 44, textAlign: 'right', padding: '0 8px', backgroundColor: t.gutterBg, color: t.numClr, userSelect: 'none' }}>
                {row.newLine !== null ? row.newLine : ''}
              </span>
              <span style={{ width: 18, minWidth: 18, textAlign: 'center', padding: '0 2px', color: t.marker, userSelect: 'none', fontWeight: 700 }}>
                {marker}
              </span>
              <span style={{ flex: 1, padding: '0 10px', color: t.codeClr, whiteSpace: 'pre' }}>
                {row.code}
              </span>
            </div>
          )
        })}
      </div>

      {!showAll && hiddenLineCount > DIFF_CONTEXT_LINES && (
        <button
          onClick={() => setShowAll(true)}
          className="w-full text-center text-xs py-1 text-muted hover:text-foreground border-t border-border/30 hover:bg-surface/50 transition-colors"
        >
          Show {hiddenLineCount} unchanged lines &#x2193;
        </button>
      )}
      {showAll && hiddenLineCount > DIFF_CONTEXT_LINES && (
        <button
          onClick={() => setShowAll(false)}
          className="w-full text-center text-xs py-1 text-muted hover:text-foreground border-t border-border/30 hover:bg-surface/50 transition-colors"
        >
          Collapse unchanged lines &#x2191;
        </button>
      )}
    </div>
  )
}

function FileChangeCard({ filename, fileData, sessionId, onApplied, onChangeApplied }: {
  filename: string
  fileData: { filename: string; file_id: string; changes: any[] }
  sessionId: string
  onApplied?: (filename: string, content: string) => void
  onChangeApplied?: (delta?: number) => void
}) {
  // Filter ghost diffs first — do this before any state so IDs are stable
  const realChanges = fileData.changes.filter((c: any) => {
    if (!c.diff) return false
    const lines = c.diff.split('\n')
    const hasAdds = lines.some((l: string) => l.startsWith('+') && !l.startsWith('+++'))
    const hasRemoves = lines.some((l: string) => l.startsWith('-') && !l.startsWith('---'))
    return hasAdds || hasRemoves
  })

  const changeIds = realChanges.map((c: any) => c.id)

  // Checkbox: all checked by default except QA-blocked ones
  const [checked, setChecked] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(
      realChanges.map((c: any) => [c.id, c.qa_result?.verdict !== 'blocked'])
    )
  )

  // Diff expand/collapse per change — collapsed by default
  const [diffExpanded, setDiffExpanded] = useState<Record<string, boolean>>({})

  const [applying, setApplying] = useState(false)
  const [undoing, setUndoing] = useState<Record<string, boolean>>({})
  const [applied, setApplied] = useState<Record<string, boolean>>(() =>
    loadApplied(sessionId, changeIds)
  )
  const [originalCode, setOriginalCode] = useState<string>('')
  const [modifiedCode, setModifiedCode] = useState<string | undefined>(undefined)

  // Pre-fetch original file content so Preview works before Apply
  useEffect(() => {
    if (!isVisualFile(filename)) return
    api.sessionFiles.get(sessionId, fileData.file_id)
      .then(f => setOriginalCode(f.content))
      .catch(() => {})
  }, [sessionId, fileData.file_id, filename])

  const langFromFilename = getLangFromFilename(filename)

  if (realChanges.length === 0) return null

  // Pending = not yet applied
  const pendingChanges = realChanges.filter((c: any) => !applied[c.id])
  // Selected = pending + checked
  const selectedChanges = pendingChanges.filter((c: any) => checked[c.id])
  const allApplied = realChanges.every((c: any) => applied[c.id])

  const getProposedCode = (change: any): string => {
    if (!originalCode) return '// Loading preview...'
    const orig = change.original_code
    const next = change.new_code
    if (!orig || !next) return originalCode
    const idx = originalCode.indexOf(orig)
    if (idx === -1) return originalCode
    return originalCode.slice(0, idx) + next + originalCode.slice(idx + orig.length)
  }

  // Cmd+Y: apply selected
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'y') {
        e.preventDefault()
        const btn = document.querySelector<HTMLButtonElement>('[data-apply-btn]')
        if (btn && !btn.disabled) btn.click()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const markApplied = (changeId: string) => {
    saveApplied(sessionId, changeId)
    setApplied(p => ({ ...p, [changeId]: true }))
    onChangeApplied?.()
  }

  // Apply all selected changes in one call
  const handleApplySelected = async () => {
    if (selectedChanges.length === 0) return
    setApplying(true)
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, fileData.file_id)
      if (!originalCode) setOriginalCode(fileData2.content)

      let result: any
      if (selectedChanges.length === 1) {
        result = await api.surgical.apply({
          file_path: filename,
          changes: selectedChanges,
          file_content: fileData2.content,
        })
      } else {
        result = await api.surgical.applyAll({
          file_path: filename,
          changes: selectedChanges,
          file_content: fileData2.content,
        })
      }

      const newContent = result.modified_content || ''
      if (newContent) {
        try { await api.sessionFiles.update(sessionId, fileData.file_id, newContent) } catch {}
      }

      if (result.cloud_mode || result.modified_content) {
        const blob = new Blob([newContent], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url; a.download = filename; a.click()
        URL.revokeObjectURL(url)
        const label = selectedChanges.length > 1
          ? `Applied & downloaded ${filename} (${selectedChanges.length} changes)`
          : `Applied & downloaded ${filename}`
        toast.success(`✅ ${label}`)
        setModifiedCode(newContent)
        onApplied?.(filename, newContent)
      } else {
        toast.success(`Applied ${selectedChanges.length} change${selectedChanges.length !== 1 ? 's' : ''} to ${filename}`)
        setModifiedCode(newContent)
        onApplied?.(filename, newContent)
      }
      for (const change of selectedChanges) markApplied(change.id)
    } catch (e: any) {
      toast.error(e.message || 'Apply failed')
    } finally {
      setApplying(false)
    }
  }

  const handleUndo = async (change: any) => {
    setUndoing(p => ({ ...p, [change.id]: true }))
    try {
      const result = await api.sessionFiles.undo(sessionId, fileData.file_id)
      try { localStorage.removeItem(appliedKey(sessionId, change.id)) } catch {}
      setApplied(p => { const next = { ...p }; delete next[change.id]; return next })
      setModifiedCode(undefined)
      onApplied?.(filename, result.content)
      toast.success(`↩ Reverted ${filename} to previous version`)
      onChangeApplied?.(-1)
    } catch (e: any) {
      toast.error(e.message || 'Undo failed')
    } finally {
      setUndoing(p => ({ ...p, [change.id]: false }))
    }
  }

  const handleDownload = async () => {
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, fileData.file_id)
      const changesToApply = selectedChanges.length > 0
        ? selectedChanges
        : pendingChanges
      let result: any
      if (changesToApply.length === 0) {
        // Nothing pending — download current file as-is
        const blob = new Blob([fileData2.content], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url; a.download = filename; a.click()
        URL.revokeObjectURL(url)
        toast.success(`Downloaded ${filename}`)
        return
      }
      if (changesToApply.length === 1) {
        result = await api.surgical.apply({ file_path: filename, changes: changesToApply, file_content: fileData2.content })
      } else {
        result = await api.surgical.applyAll({ file_path: filename, changes: changesToApply, file_content: fileData2.content })
      }
      const content = result.modified_content || fileData2.content
      const blob = new Blob([content], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = filename; a.click()
      URL.revokeObjectURL(url)
      toast.success(`Downloaded ${filename}`)
    } catch (e: any) {
      toast.error(e.message || 'Download failed')
    }
  }

  const toggleCheck = (id: string) => setChecked(p => ({ ...p, [id]: !p[id] }))
  const toggleDiff = (id: string) => setDiffExpanded(p => ({ ...p, [id]: !p[id] }))

  const skipAll = () =>
    setChecked(Object.fromEntries(pendingChanges.map((c: any) => [c.id, false])))

  return (
    <div className="border border-border rounded-xl overflow-hidden mb-3">
      {/* ── File header ─────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2.5 px-4 py-2.5 bg-surface/80">
        <FileCode size={14} className="text-accent flex-shrink-0" />
        <span className="text-sm font-semibold text-ink">{filename}</span>
        <span className="text-[11px] text-muted/70 ml-1">
          {realChanges.length} change{realChanges.length !== 1 ? 's' : ''}
        </span>
        {allApplied && (
          <span className="ml-auto flex items-center gap-1.5 text-[12px] text-success font-semibold">
            <CheckCircle size={13} /> All applied
          </span>
        )}
      </div>

      {/* ── Change rows ─────────────────────────────────────────────────── */}
      {realChanges.map((change: any, idx: number) => {
        const isBlocked = change.qa_result?.verdict === 'blocked'
        const isApplied = applied[change.id]
        const isChecked = checked[change.id] && !isBlocked
        const isExpanded = diffExpanded[change.id]

        const addCount = (change.diff || '').split('\n')
          .filter((l: string) => l.startsWith('+') && !l.startsWith('+++')).length
        const removeCount = (change.diff || '').split('\n')
          .filter((l: string) => l.startsWith('-') && !l.startsWith('---')).length

        return (
          <div key={change.id} className={`border-t border-border/60 ${isApplied ? 'opacity-70' : ''}`}>
            {/* Row header — this IS the apply decision row */}
            <div className={`flex items-center gap-3 px-4 py-3 ${isApplied ? 'bg-success/5' : 'bg-base/60'}`}>

              {/* Checkbox OR applied checkmark */}
              <div className="flex-shrink-0 w-5 flex items-center justify-center">
                {isApplied ? (
                  <CheckCircle size={16} className="text-success" />
                ) : (
                  <input
                    type="checkbox"
                    checked={isChecked}
                    disabled={isBlocked}
                    onChange={() => toggleCheck(change.id)}
                    className="w-4 h-4 cursor-pointer accent-emerald-500 disabled:opacity-40"
                    title={isBlocked ? 'Blocked by QA — cannot apply' : isChecked ? 'Uncheck to skip this change' : 'Check to include this change'}
                  />
                )}
              </div>

              {/* Number badge */}
              <span className="w-5 h-5 flex items-center justify-center text-[11px] font-bold bg-accent/15 text-accent rounded-full flex-shrink-0">
                {idx + 1}
              </span>

              {/* Symbol name + description */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <code className="text-[12px] text-accent font-mono truncate max-w-[180px]">
                    {change.symbol?.full_path || change.symbol?.name || 'unknown'}
                  </code>
                  {change.symbol?.start_line && (
                    <span className="text-[10px] px-1.5 py-0.5 bg-accent/10 text-accent border border-accent/20 rounded font-mono">
                      L{change.symbol.start_line}
                      {change.symbol.end_line && change.symbol.end_line !== change.symbol.start_line
                        ? `–${change.symbol.end_line}` : ''}
                    </span>
                  )}
                </div>
                <p className="text-[12px] text-muted mt-0.5 truncate">{change.description}</p>
              </div>

              {/* Stats + badges */}
              <div className="flex items-center gap-1.5 flex-shrink-0 flex-wrap justify-end">
                <span className="text-[11px] font-mono">
                  <span className="text-success">+{addCount}</span>
                  <span className="text-muted/40 mx-0.5">/</span>
                  <span className="text-danger">-{removeCount}</span>
                </span>
                <ConfidenceBadge score={change.confidence} />
                {change.qa_result && <QABadge qa={change.qa_result} />}
                {change.qa_result && <BlastRadius change={{ qa: change.qa_result }} />}
                {change.confidence < 7 && !isBlocked && (
                  <span className="flex items-center gap-1 text-[10px] text-warning">
                    <AlertTriangle size={10} /> Review
                  </span>
                )}
              </div>

              {/* Right controls: Undo (if applied) + Preview + Diff toggle */}
              <div className="flex items-center gap-1.5 flex-shrink-0">
                {isApplied && (
                  <button
                    onClick={() => handleUndo(change)}
                    disabled={undoing[change.id]}
                    className="flex items-center gap-1 px-2 py-1 bg-surface text-muted border border-border rounded-lg text-[11px] font-semibold hover:bg-overlay hover:text-ink transition-colors disabled:opacity-50"
                    title="Revert this change"
                  >
                    <RotateCcw size={11} />
                    {undoing[change.id] ? 'Reverting...' : 'Undo'}
                  </button>
                )}
                {isVisualFile(filename) && (
                  <LivePreview
                    code={originalCode || '// Loading...'}
                    filename={filename}
                    modifiedCode={
                      isApplied
                        ? modifiedCode
                        : getProposedCode(change)
                    }
                  />
                )}
                <button
                  onClick={() => toggleDiff(change.id)}
                  className="flex items-center gap-1 px-2.5 py-1 bg-surface text-muted border border-border rounded-lg text-[11px] font-semibold hover:bg-overlay hover:text-ink transition-colors"
                  title={isExpanded ? 'Collapse diff' : 'View diff'}
                >
                  {isExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                  <span>Diff</span>
                </button>
              </div>
            </div>

            {/* Expandable diff — only shown when toggled */}
            {isExpanded && (
              <div className="border-t border-border">
                <div className="flex items-center gap-2 px-4 py-1.5 bg-base border-b border-border/60">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-muted/70">Diff Preview</span>
                  {(() => {
                    const startLine = parseDiffStartLine(change.diff || '')
                    return startLine ? (
                      <span className="text-[10px] text-accent/70 font-mono">@ line {startLine}</span>
                    ) : null
                  })()}
                  <span className="text-[10px] text-faint ml-auto">
                    <span className="text-success">+{addCount}</span>
                    {' · '}
                    <span className="text-danger">-{removeCount}</span>
                  </span>
                </div>
                <div className="bg-base max-h-96 overflow-y-auto">
                  <DiffBlock diff={change.diff || ''} language={langFromFilename} />
                </div>
              </div>
            )}
          </div>
        )
      })}

      {/* ── Single action bar ──────────────────────────────────────────── */}
      {!allApplied && (
        <div className="flex items-center gap-2 px-4 py-3 bg-surface/60 border-t border-border">
          {/* Download selected changes */}
          <button
            onClick={handleDownload}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-surface text-muted border border-border rounded-lg text-[12px] font-semibold hover:bg-overlay transition-colors"
            title="Download file with selected changes applied"
          >
            <Download size={12} /> Download
          </button>

          <div className="ml-auto flex items-center gap-2">
            {/* Skip all = uncheck all pending */}
            {selectedChanges.length > 0 && (
              <button
                onClick={skipAll}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-surface text-muted border border-border rounded-lg text-[12px] font-semibold hover:bg-overlay transition-colors"
                title="Uncheck all pending changes"
              >
                <XCircle size={12} /> Skip All
              </button>
            )}

            {/* Apply Selected */}
            <button
              onClick={handleApplySelected}
              data-apply-btn
              disabled={selectedChanges.length === 0 || applying}
              className="flex items-center gap-1.5 px-4 py-1.5 bg-success/15 text-success border border-success/30 rounded-lg text-[12px] font-semibold hover:bg-success/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              title={selectedChanges.length === 0 ? 'No changes selected — check at least one above' : `Apply ${selectedChanges.length} selected change${selectedChanges.length !== 1 ? 's' : ''}`}
            >
              <CheckCircle size={12} />
              {applying
                ? 'Applying...'
                : selectedChanges.length === 0
                  ? 'Nothing selected'
                  : `Apply Selected (${selectedChanges.length})`
              }
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export function InlineDiffCard({ result, sessionId, onApplied }: Props) {
  const [showTestRunner, setShowTestRunner] = useState(false)
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
          onChangeApplied={(delta = 1) => { setAppliedCount(n => Math.max(0, n + delta)); setShowTestRunner(true) }}
        />
      ))}

      {/* Skipped changes notice */}
      {result.skipped_changes && result.skipped_changes.length > 0 && (
        <div className="mt-2 flex items-start gap-2 px-3 py-2 bg-surface/60 border border-border/50 rounded-lg">
          <SkipForward size={13} className="text-muted/60 mt-0.5 flex-shrink-0" />
          <div className="text-[12px] text-muted/80">
            <strong className="text-ink/60">Skipped {result.skipped_changes.length} symbol{result.skipped_changes.length !== 1 ? 's' : ''}:</strong>{' '}
            {result.skipped_changes.map((s: any, i: number) => (
              <span key={i}>
                <code className="text-[11px] text-accent/70">{s.symbol}</code>
                {s.reason === 'already_matches'
                  ? ' — code already matches'
                  : ' — no visible diff produced'}
                {i < (result.skipped_changes?.length ?? 0) - 1 ? '; ' : ''}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Test runner — shown after apply */}
      {showTestRunner && (
        <TestRunnerPanel inline={true} onClose={() => setShowTestRunner(false)} />
      )}

      {/* Risks alert — hidden (not removed) once all changes applied */}
      {result.risks && result.risks.length > 0 && (() => {
        // Merge risk_verdicts from all changes in this file
        const allVerdicts: Record<string, { status: string; reason: string }> = {}
        Object.values(result.changes_by_file || {}).forEach((fd: any) => {
          (fd.changes || []).forEach((ch: any) => {
            (ch.qa_result?.risk_verdicts || []).forEach((rv: any) => {
              allVerdicts[rv.risk] = { status: rv.status, reason: rv.reason }
            })
          })
        })
        const hasVerdicts = Object.keys(allVerdicts).length > 0
        const resolvedCount = hasVerdicts
          ? result.risks.filter((r: string) => allVerdicts[r]?.status === 'verified_safe').length
          : 0
        const allResolved = hasVerdicts && resolvedCount === result.risks.length
        const statusIcon: Record<string, string> = {
          verified_safe: '✅',
          warning: '⚠️',
          blocked: '🚫',
        }
        const statusColor: Record<string, string> = {
          verified_safe: 'text-success',
          warning: 'text-warning',
          blocked: 'text-danger',
        }
        return (
          <div className={`mt-2 px-3 py-2 bg-warning/10 border border-warning/25 rounded-lg transition-opacity duration-300 ${allApplied ? 'hidden' : ''}`}>
            <div className="flex items-center gap-2 mb-1.5">
              <AlertTriangle size={13} className="text-warning flex-shrink-0" />
              <span className="text-[12px] font-semibold text-warning">
                {hasVerdicts
                  ? allResolved
                    ? `✅ All ${result.risks.length} risks reviewed — safe to apply`
                    : `Risks: ${resolvedCount}/${result.risks.length} verified safe`
                  : `Risks:`}
              </span>
            </div>
            <ul className="space-y-1.5 list-none">
              {result.risks.map((r: string, i: number) => {
                const verdict = allVerdicts[r]
                return (
                  <li key={i} className="text-[11.5px]">
                    {verdict ? (
                      <div className="flex flex-col gap-0.5">
                        <div className={`flex items-start gap-1.5 font-medium ${statusColor[verdict.status] || 'text-warning'}`}>
                          <span className="flex-shrink-0">{statusIcon[verdict.status] || '•'}</span>
                          <span className={verdict.status === 'verified_safe' ? 'line-through opacity-60' : ''}>{r}</span>
                        </div>
                        <div className="ml-5 text-[11px] text-muted italic">{verdict.reason}</div>
                      </div>
                    ) : (
                      <div className="flex items-start gap-1.5 text-warning">
                        <span className="mt-0.5 text-warning/70">•</span>
                        <span>{r}</span>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          </div>
        )
      })()}
    </div>
  )
}
