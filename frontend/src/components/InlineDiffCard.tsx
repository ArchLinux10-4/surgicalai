import React, { useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vscDarkPlus, oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { useThemeStore } from '../stores/themeStore'
import { api } from '../api/client'
import { TestRunnerPanel } from './TestRunnerPanel'
import { toast } from '../lib/toast'
import type { SmartResult, QAResult } from '../types'
import { LivePreview, isVisualFile } from './LivePreview'
import { useAppStore } from '../stores/appStore'
import { recordDiffStats, revertDiffStats } from '../lib/fileClassify'
import { acquireApplyLock, releaseApplyLock } from '../lib/fileApplyLock'
import { Cancel, CheckCircle, Close, Description, FileDownload, History, KeyboardArrowDown, KeyboardArrowRight, KeyboardArrowUp, Replay, SkipNext, Visibility, Warning } from '@mui/icons-material'
import { ApplyProgressStrip } from './ApplyProgressStrip'
import type { ApplyProgress } from '../lib/applyProgress'
import { applyStageLabel } from '../lib/applyProgress'
import { clientLog } from '../lib/clientLog';
interface Props {
  result: SmartResult
  sessionId: string
  onApplied?: (filename: string, modifiedContent: string) => void
  // onRetryWithQA: automates what the user was previously doing by hand —
  // copying a failed QA report and pasting it back into chat to trigger a
  // fresh correction attempt (proven in trace 414dfaef to reliably yield a
  // better fix — that session went 2/10 -> 9/10 this way). Wired by
  // ChatPanel to its real send pathway so this is a genuine resend, not a
  // cosmetic mock.
  onRetryWithQA?: (reportText: string) => void
}

// Builds the same information a user would have copy/pasted back into chat
// after seeing a failed QA report — filename, symbol, verdict/score, and
// every concrete issue QA found — formatted as plain text so it reads
// naturally as a chat message asking for a fix.
function buildQARetryReportText(filename: string, change: any): string {
  const qa = change.qa_result || {}
  const symbol = change.symbol?.full_path || change.symbol?.name || 'this change'
  const lines: string[] = []
  lines.push(`QA failed for ${symbol} in ${filename} (verdict: ${qa.verdict || 'blocked'}, score: ${qa.qa_score ?? 'n/a'}/10). Please fix it.`)
  if (qa.summary) lines.push(`Summary: ${qa.summary}`)
  const bucket = (label: string, items?: string[]) => {
    if (items && items.length) lines.push(`${label}:\n` + items.map(i => `- ${i}`).join('\n'))
  }
  bucket('Type/compile errors', qa.type_errors)
  bucket('Import issues', qa.import_issues)
  bucket('Logic errors', qa.logic_errors)
  bucket('Downstream risks', qa.downstream_risks)
  if (qa.plan_deviation) lines.push(`Plan deviation: ${qa.plan_deviation}`)
  return lines.join('\n\n')
}

// --- localStorage helpers for persisting applied/skipped state across refresh ---
const appliedKey = (sessionId: string, changeId: string) =>
  `sai-applied:${sessionId}:${changeId}`
const skippedKey = (sessionId: string, changeId: string) =>
  `sai-skipped:${sessionId}:${changeId}`

const loadApplied = (sessionId: string, changeIds: string[]): Record<string, boolean> => {
  const out: Record<string, boolean> = {}
  for (const id of changeIds) {
    if (localStorage.getItem(appliedKey(sessionId, id)) === '1') out[id] = true
  }
  return out
}

const loadSkipped = (sessionId: string, changeIds: string[]): Record<string, boolean> => {
  const out: Record<string, boolean> = {}
  for (const id of changeIds) {
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

/** Count added/removed lines in a unified diff — used to track per-file diff stats. */
function diffLineCounts(diff: string): { added: number; removed: number } {
  const lines = (diff || '').split('\n')
  const added = lines.filter(l => l.startsWith('+') && !l.startsWith('+++')).length
  const removed = lines.filter(l => l.startsWith('-') && !l.startsWith('---')).length
  return { added, removed }
}
// ------------------------------------------------------------------------------------------------------------------------------------------


function QABadge({ qa }: { qa: QAResult }) {
  const [expanded, setExpanded] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popupRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ top: 0, left: 0 })
  const styles: Record<string, string> = {
    safe:    'text-success bg-success/15 border-success/30',
    warning: 'text-warning bg-warning/15 border-warning/30',
    blocked: 'text-danger bg-danger/15 border-danger/30',
    skipped: 'text-muted bg-muted/10 border-muted/30',
  }
  const icons: Record<string, string> = { safe: '✅', warning: '⚠️', blocked: '🚫', skipped: '⏭' }
  // hard_blocked (trace 414dfaef): a real blocked verdict that survived
  // every auto-retry gets a visually distinct badge from a routine
  // warning/skipped/borderline advisory, so it doesn't blend in as "just
  // another small yellow badge" the way the 2/10 case did.
  const color = qa.hard_blocked ? 'text-danger bg-danger/25 border-danger/50 font-bold' : (styles[qa.verdict] || styles.skipped)
  const icon = qa.hard_blocked ? '⛔' : (icons[qa.verdict] || '⏭')
  const score = qa.qa_score !== null ? ` ${qa.qa_score}/10` : ''
  const issues = [
    ...qa.import_issues,
    ...qa.downstream_risks,
    ...qa.type_errors,
    ...(qa.logic_errors || []),
    ...(qa.plan_deviation ? [qa.plan_deviation] : []),
  ].filter(Boolean)

  const handleToggle = () => {
    if (!expanded && btnRef.current) {
      const rect = btnRef.current.getBoundingClientRect()
      const POPUP_W = 420
      const MARGIN = 12
      // Right-align popup with button's right edge; clamp so it never leaves viewport
      const rawLeft = rect.right - POPUP_W
      const clampedLeft = Math.min(rawLeft, window.innerWidth - POPUP_W - MARGIN)
      const left = Math.max(MARGIN, clampedLeft)
      setPos({ top: rect.bottom + 6, left })
    }
    setExpanded(e => !e)
  }

  useEffect(() => {
    if (!expanded) return
    const handler = (e: MouseEvent) => {
      if (
        btnRef.current && !btnRef.current.contains(e.target as Node) &&
        popupRef.current && !popupRef.current.contains(e.target as Node)
      ) setExpanded(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [expanded])

  return (
    <span>
      <button
        ref={btnRef}
        onClick={handleToggle}
        className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border cursor-pointer ${color}`}
        title={qa.summary}
      >
        QA {icon}{score}
      </button>
      {expanded && createPortal(
        <div
          ref={popupRef}
          style={{ position: 'fixed', top: pos.top, left: pos.left, zIndex: 9999, background: 'rgb(var(--c-surface))', width: 420, maxHeight: `calc(100vh - ${pos.top + 16}px)`, overflowY: 'scroll' }}
          className="border border-border rounded-lg shadow-2xl p-4 text-[12px]"
        >
          <div className="flex items-center justify-between mb-2">
            <span className="font-semibold text-ink">QA Report</span>
            <button onClick={() => setExpanded(false)} className="text-muted hover:text-ink">✕</button>
          </div>
          <p className="text-muted mb-2">{qa.summary || 'No summary'}</p>
          {qa.hard_blocked && (
            <p className="text-danger font-semibold mb-2">
              ⛔ Still blocked after every auto-fix attempt — this diff was NOT applied.
              {qa.regression_detected && ' An earlier attempt made it worse and was reverted automatically.'}
            </p>
          )}
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
        </div>,
        document.body
      )}
    </span>
  )
}

function BlastRadius({ change }: { change: any }) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popupRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ top: 0, left: 0 })
  const risks: string[] = [
    ...(change.qa_result?.downstream_risks || []),
    ...(change.qa_result?.import_issues || []),
    ...(change.qa_result?.type_errors || []),
  ].filter(Boolean)
  if (!risks.length) return null

  const handleToggle = () => {
    if (!open && btnRef.current) {
      const rect = btnRef.current.getBoundingClientRect()
      const POPUP_W = 420
      const MARGIN = 12
      // Right-align popup with button's right edge; clamp so it never leaves viewport
      const rawLeft = rect.right - POPUP_W
      const clampedLeft = Math.min(rawLeft, window.innerWidth - POPUP_W - MARGIN)
      const left = Math.max(MARGIN, clampedLeft)
      setPos({ top: rect.bottom + 6, left })
    }
    setOpen(o => !o)
  }

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (
        btnRef.current && !btnRef.current.contains(e.target as Node) &&
        popupRef.current && !popupRef.current.contains(e.target as Node)
      ) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <span>
      <button
        ref={btnRef}
        onClick={handleToggle}
        className="text-[11px] font-semibold px-2 py-0.5 rounded-full border text-warning bg-warning/10 border-warning/30 cursor-pointer"
        title="Blast radius"
      >
        🎯 {risks.length} risk{risks.length !== 1 ? 's' : ''}
      </button>
      {open && createPortal(
        <div
          ref={popupRef}
          style={{ position: 'fixed', top: pos.top, left: pos.left, zIndex: 9999, background: 'rgb(var(--c-surface))', width: 420, maxHeight: `calc(100vh - ${pos.top + 16}px)`, overflowY: 'scroll' }}
          className="border border-border rounded-lg shadow-2xl p-4 text-[12px]"
        >
          <div className="flex items-center justify-between mb-2">
            <span className="font-semibold text-warning">🎯 Blast Radius</span>
            <button onClick={() => setOpen(false)} className="text-muted hover:text-ink">×</button>
          </div>
          <ul className="space-y-1">
            {risks.map((r, i) => (
              <li key={i} className="flex gap-1.5 text-warning text-[11px]">
                <span>•</span><span>{r}</span>
              </li>
            ))}
          </ul>
        </div>,
        document.body
      )}
    </span>
  )
}

function ConfidenceBadge({ change }: { change: any }) {
  const [expanded, setExpanded] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popupRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ top: 0, left: 0 })

  const score: number = typeof change?.confidence === 'number' ? change.confidence : 0
  const color = score >= 8 ? 'text-success bg-success/15 border-success/30'
    : score >= 6 ? 'text-warning bg-warning/15 border-warning/30'
    : 'text-danger bg-danger/15 border-danger/30'
  const label = score >= 8 ? 'High' : score >= 6 ? 'Medium' : 'Low'

  const meaning = score >= 8
    ? 'High — the editor is confident this change is correct and complete.'
    : score >= 6
      ? 'Medium — the change looks right but a quick review is recommended before applying.'
      : 'Low — review carefully before applying; the editor is not certain this is fully correct.'

  const description: string = (change?.description || '').trim()
  const notes: string[] = Array.isArray(change?.surgeon_notes)
    ? change.surgeon_notes.filter(Boolean)
    : []

  const handleToggle = () => {
    if (!expanded && btnRef.current) {
      const rect = btnRef.current.getBoundingClientRect()
      const POPUP_W = 420
      const MARGIN = 12
      // Right-align popup with button's right edge; clamp so it never leaves viewport
      const rawLeft = rect.right - POPUP_W
      const clampedLeft = Math.min(rawLeft, window.innerWidth - POPUP_W - MARGIN)
      const left = Math.max(MARGIN, clampedLeft)
      setPos({ top: rect.bottom + 6, left })
    }
    setExpanded(e => !e)
  }

  useEffect(() => {
    if (!expanded) return
    const handler = (e: MouseEvent) => {
      if (
        btnRef.current && !btnRef.current.contains(e.target as Node) &&
        popupRef.current && !popupRef.current.contains(e.target as Node)
      ) setExpanded(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [expanded])

  return (
    <span>
      <button
        ref={btnRef}
        onClick={handleToggle}
        className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border cursor-pointer ${color}`}
        title="How confident the editor is in this change — click for details"
      >
        {label} {score}/10
      </button>
      {expanded && createPortal(
        <div
          ref={popupRef}
          style={{ position: 'fixed', top: pos.top, left: pos.left, zIndex: 9999, background: 'rgb(var(--c-surface))', width: 420, maxHeight: `calc(100vh - ${pos.top + 16}px)`, overflowY: 'scroll' }}
          className="border border-border rounded-lg shadow-2xl p-4 text-[12px]"
        >
          <div className="flex items-center justify-between mb-2">
            <span className="font-semibold text-ink">Confidence — {label} ({score}/10)</span>
            <button onClick={() => setExpanded(false)} className="text-muted hover:text-ink">✕</button>
          </div>
          <p className="text-muted mb-2">{meaning}</p>
          <p className="text-muted/80 mb-2 text-[11px] italic">
            This is the AI editor's self-assessment of how certain it is that this specific edit is
            correct and complete. It is separate from the QA score, which is an independent reviewer's verdict.
          </p>
          {description && (
            <div className="mb-2">
              <span className="font-semibold text-ink block mb-1">Why this change</span>
              <p className="text-muted">{description}</p>
            </div>
          )}
          {notes.length > 0 && (
            <div className="mb-1">
              <span className="font-semibold text-ink block mb-1">Editor notes</span>
              <ul className="space-y-1">
                {notes.map((n, i) => (
                  <li key={i} className="flex gap-1.5 text-muted">
                    <span>•</span><span>{n}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {score < 7 && (
            <p className="text-warning mt-1 text-[11px]">
              Below the auto-apply comfort threshold (7/10) — flagged for review.
            </p>
          )}
        </div>,
        document.body
      )}
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

function FileChangeCard({ filename, fileData, sessionId, onApplied, onChangeApplied, onRetryWithQA }: {
  filename: string
  fileData: { filename: string; file_id: string; changes: any[] }
  sessionId: string
  onApplied?: (filename: string, content: string) => void
  onChangeApplied?: (delta?: number) => void
  onRetryWithQA?: (reportText: string) => void
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

  // File body collapsed by default (Cursor-like dense row). Expand to review
  // change list / diffs; Apply stays available on the collapsed header.
  const [fileExpanded, setFileExpanded] = useState(false)

  // Preview toggle — one per file, not per change (Live Preview renders the whole file)
  const [showFilePreview, setShowFilePreview] = useState(false)

  const { setSessionFiles } = useAppStore()
  // ── file_id recovery net ────────────────────────────────────────────────
  // Every apply/undo/version call is keyed on this id. If the pipeline emitted
  // an empty file_id (a producer that never persisted its content to
  // session_files), recover it by filename from the live session file list.
  // Without this, the request URL collapses to `/chat/{sid}/files/` and the
  // apply silently does nothing — proven in session d021ff07, where two
  // QA-clean edits were lost to `GET /files/ 307` with no PUT ever sent.
  const sessionFilesForId = useAppStore(s => s.sessionFiles)
  const effectiveFileId: string =
    fileData.file_id ||
    sessionFilesForId.find(f => f.filename === (fileData.filename || filename))?.id ||
    ''
  const [applying, setApplying] = useState(false)
  const [applyProgress, setApplyProgress] = useState<ApplyProgress | null>(null)
  const [applyStartedAt, setApplyStartedAt] = useState(0)
  const [undoing, setUndoing] = useState<Record<string, boolean>>({})
  const [applied, setApplied] = useState<Record<string, boolean>>(() =>
    loadApplied(sessionId, changeIds)
  )
  const [skipped, setSkipped] = useState<Record<string, boolean>>(() =>
    loadSkipped(sessionId, changeIds)
  )
  const [originalCode, setOriginalCode] = useState<string>('')
  const [modifiedCode, setModifiedCode] = useState<string | undefined>(undefined)
  const [previewKey, setPreviewKey] = useState(0)

  // Pre-fetch original file content so Preview works before Apply
  useEffect(() => {
    if (!isVisualFile(filename)) return
    api.sessionFiles.get(sessionId, effectiveFileId)
      .then(f => setOriginalCode(f.content))
      .catch(() => {})
  }, [sessionId, effectiveFileId, filename])

  // Load applied state from backend DB on mount (survives page refresh, cross-browser)
  useEffect(() => {
    if (!sessionId) return
    api.surgical.getApplied(sessionId)
      .then(({ applied_ids }) => {
        const fromDB: Record<string, boolean> = {}
        for (const id of applied_ids) {
          if (changeIds.includes(id)) fromDB[id] = true
        }
        if (Object.keys(fromDB).length > 0) {
          setApplied(prev => ({ ...fromDB, ...prev }))
        }
      })
      .catch(() => {})
  }, [sessionId]) // eslint-disable-line react-hooks/exhaustive-deps


  // Re-sync applied state when Apply All fires from ChatPanel
  useEffect(() => {
    const refresh = () => {
      if (!sessionId) return
      api.surgical.getApplied(sessionId)
        .then(({ applied_ids }) => {
          const fromDB: Record<string, boolean> = {}
          for (const id of applied_ids) {
            if (changeIds.includes(id)) fromDB[id] = true
          }
          if (Object.keys(fromDB).length > 0) setApplied(prev => ({ ...fromDB, ...prev }))
        })
        .catch(() => {})
    }
    window.addEventListener('sai-applied-refresh', refresh)
    return () => window.removeEventListener('sai-applied-refresh', refresh)
  }, [sessionId]) // eslint-disable-line react-hooks/exhaustive-deps
  const langFromFilename = getLangFromFilename(filename)

  if (realChanges.length === 0) return null

  // Pending = not yet applied AND not skipped
  const pendingChanges = realChanges.filter((c: any) => !applied[c.id] && !skipped[c.id])
  // Selected = pending + checked
  const selectedChanges = pendingChanges.filter((c: any) => checked[c.id])
  const allApplied = realChanges.every((c: any) => applied[c.id] || skipped[c.id])

  // Aggregate +/- for the collapsed file row (screenshot clutter was open rows
  // with per-change stats always visible).
  let fileAddCount = 0
  let fileRemoveCount = 0
  for (const c of realChanges) {
    for (const l of (c.diff || '').split('\n')) {
      if (l.startsWith('+') && !l.startsWith('+++')) fileAddCount++
      else if (l.startsWith('-') && !l.startsWith('---')) fileRemoveCount++
    }
  }

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

  const markApplied = (changeId: string, diff?: string) => {
    saveApplied(sessionId, changeId)
    setApplied(p => ({ ...p, [changeId]: true }))
    setSkipped(p => { const n = { ...p }; delete n[changeId]; return n })
    // Persist to backend DB so applied state survives page refresh. Returns the
    // promise so callers can await it before telling ApplyAllButton to re-check —
    // otherwise the re-check GET can race ahead of this POST and read stale
    // (unapplied) state, leaving "Apply All" stuck visible forever.
    const persisted = sessionId
      ? api.surgical.markApplied(sessionId, changeId).catch(() => {})
      : Promise.resolve()
    // Track lines added/removed for this file — powers the diff-stats badge in the file drawer
    if (diff) {
      const { added, removed } = diffLineCounts(diff)
      recordDiffStats(effectiveFileId, added, removed)
    }
    onChangeApplied?.()
    return persisted
  }

  // Apply all selected changes in one call
  const handleApplySelected = async () => {
    if (selectedChanges.length === 0) return
    // Session d021ff07: empty file_id made apply URL collapse and silently fail.
    if (!effectiveFileId) {
      clientLog('diff_apply_missing_file_id', {
        filename,
        changeCount: selectedChanges.length,
        fileExpanded,
      }, sessionId)
      toast.error('Cannot apply — file id missing. Refresh the session and try again.')
      return
    }
    // Mutual exclusion with the global "Apply All" bar (and other diff cards
    // for the same file) — see fileApplyLock.ts for the proven race this
    // closes. When nothing else is applying this file (the normal case),
    // this acquires instantly and every line below runs exactly as before.
    if (!acquireApplyLock(effectiveFileId)) {
      clientLog('diff_apply_lock_busy', {
        filename,
        fileId: effectiveFileId,
        changeCount: selectedChanges.length,
        fileExpanded,
      }, sessionId)
      toast.error('This file is being applied elsewhere right now — please wait a moment and try again')
      return
    }
    const n = selectedChanges.length
    const started = Date.now()
    clientLog('diff_apply_started', {
      filename,
      fileId: effectiveFileId,
      changeCount: n,
      fileExpanded,
      fromCollapsedHeader: !fileExpanded,
    }, sessionId)
    setApplyStartedAt(started)
    setApplying(true)
    setApplyProgress({
      stage: 'reading',
      label: applyStageLabel('reading'),
      detail: `Reading ${filename}…`,
    })
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, effectiveFileId)
      if (!originalCode) setOriginalCode(fileData2.content)

      setApplyProgress({
        stage: 'applying',
        label: applyStageLabel('applying'),
        detail: `Applying ${n} change${n !== 1 ? 's' : ''} to ${filename}…`,
      })
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
        try {
          setApplyProgress({
            stage: 'saving',
            label: applyStageLabel('saving'),
            detail: `Saving updated ${filename}…`,
          })
          const changeLabel = selectedChanges.length === 1
            ? `Applied: ${selectedChanges[0]?.symbol?.full_path || selectedChanges[0]?.symbol?.name || 'change'}`
            : `Applied ${selectedChanges.length} changes`
          await api.sessionFiles.update(sessionId, effectiveFileId, newContent, changeLabel)
          console.debug('[InlineDiffCard] DB updated OK, re-syncing originalCode from DB')
          // Re-fetch originalCode from DB — replicates what page refresh does on mount.
          // This ensures the next round of edits starts from the true DB state.
          try {
            setApplyProgress({
              stage: 'syncing',
              label: applyStageLabel('syncing'),
              detail: `Refreshing ${filename} from session…`,
            })
            const freshFile = await api.sessionFiles.get(sessionId, effectiveFileId)
            if (freshFile?.content) {
              setOriginalCode(freshFile.content)
              console.debug('[InlineDiffCard] originalCode re-synced, len=', freshFile.content.length)
            }
          } catch (refetchErr: any) {
            // Non-fatal: originalCode may be stale but DB is correct.
            // Next refresh will fix it. Log for diagnosis.
            console.warn('[InlineDiffCard] originalCode re-fetch failed (non-fatal):', refetchErr?.message)
          }
          // Refresh session files list so GitHub sync status reflects new updated_at
          api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
        } catch (err: any) {
          console.error('[InlineDiffCard] sessionFiles.update FAILED:', err?.message || err)
          toast.error('Changes applied but failed to save to session — try refreshing')
        }
      }

      // ── Per-change failure surfacing (v3.4) ──────────────────────────
      // The backend now applies what it can and reports what it couldn't.
      // Never silently mark a dropped change as applied.
      const allFailedChanges: any[] = Array.isArray(result?.failed_changes) ? result.failed_changes : []
      // Split "already applied elsewhere" (structural idempotency check done
      // server-side — not a real failure, see surgical_editor.py) from
      // changes that genuinely could not be applied and still need action.
      const alreadyAppliedIds = new Set(
        allFailedChanges.filter((f: any) => f.already_applied).map((f: any) => f.change_id).filter(Boolean)
      )
      const failedChanges = allFailedChanges.filter((f: any) => !f.already_applied)
      const failedIds = new Set(failedChanges.map((f: any) => f.change_id).filter(Boolean))
      const okCount = selectedChanges.length - failedChanges.length - alreadyAppliedIds.size

      if (failedChanges.length > 0) {
        console.warn('[InlineDiffCard] apply reported failed changes:', failedChanges)
        clientLog('diff_apply_partial_failures', {
          filename,
          fileId: effectiveFileId,
          failedCount: failedChanges.length,
          okCount,
          symbols: failedChanges.map((f: any) => String(f.symbol || '?')).slice(0, 8),
          elapsedMs: Date.now() - started,
        }, sessionId)
        const names = failedChanges.map((f: any) => f.symbol || '?').slice(0, 3).join(', ')
        toast.error(
          `${failedChanges.length} change${failedChanges.length !== 1 ? 's' : ''} could not be applied (${names})`,
          'The file changed since these edits were generated. Re-run the request to regenerate them.'
        )
      }
      if (okCount > 0) {
        clientLog('diff_apply_succeeded', {
          filename,
          fileId: effectiveFileId,
          okCount,
          failedCount: failedChanges.length,
          elapsedMs: Date.now() - started,
        }, sessionId)
        toast.success(`Applied ${okCount} change${okCount !== 1 ? 's' : ''} to ${filename}`)
      }
      setModifiedCode(undefined)  // clear so LivePreview falls back to originalCode (fresh from DB)
      setPreviewKey(k => k + 1)     // force full remount — replicates page refresh
      onApplied?.(filename, newContent)
      setApplyProgress({
        stage: 'marking',
        label: applyStageLabel('marking'),
        detail: 'Recording applied status…',
      })
      const markPromises: Promise<any>[] = []
      for (const change of selectedChanges) {
        // Already-applied changes are marked done too (no error, no retry
        // prompt) — they just weren't reported as a fresh "applied_count"
        // by the backend because there was nothing new to write.
        if (!failedIds.has(change.id)) markPromises.push(Promise.resolve(markApplied(change.id, change.diff)))
      }
      // Wait for the backend DB writes to land before telling ApplyAllButton
      // (and other cards) to re-check applied state.
      await Promise.all(markPromises)
      window.dispatchEvent(new CustomEvent('sai-applied-refresh'))
    } catch (e: any) {
      const errMsg: string = e?.message || 'Apply failed'
      // If code couldn't be found, it was likely already applied — auto-mark it
      if (errMsg.includes("Couldn't find the exact code") || errMsg.includes("could not find") || errMsg.includes("exact code")) {
        const markPromises: Promise<any>[] = []
        for (const change of selectedChanges) {
          saveApplied(sessionId, change.id)
          setApplied(p => ({ ...p, [change.id]: true }))
          if (change.diff) {
            const { added, removed } = diffLineCounts(change.diff)
            recordDiffStats(effectiveFileId, added, removed)
          }
          // Persist to backend DB too — without this, ApplyAllButton (which
          // reads applied state ONLY from the DB via api.surgical.getApplied)
          // never learns this change is done, even though this card now
          // correctly shows "All applied". That left the sticky "Apply All"
          // bar visible forever for changes that hit this fallback path.
          if (sessionId) markPromises.push(api.surgical.markApplied(sessionId, change.id).catch(() => {}))
        }
        await Promise.all(markPromises)
        // Re-sync originalCode from DB — the file is already modified,
        // so our in-memory originalCode is stale. Replicate refresh.
        try {
          const freshFile = await api.sessionFiles.get(sessionId, effectiveFileId)
          if (freshFile?.content) {
            setOriginalCode(freshFile.content)
            console.debug('[InlineDiffCard] originalCode re-synced after auto-mark, len=', freshFile.content.length)
          }
        } catch (refetchErr: any) {
          console.warn('[InlineDiffCard] originalCode re-fetch failed (non-fatal):', refetchErr?.message)
        }
        clientLog('diff_apply_already_applied_fallback', {
          filename,
          fileId: effectiveFileId,
          changeCount: selectedChanges.length,
          elapsedMs: Date.now() - started,
        }, sessionId)
        toast.success('These changes appear to already be applied ✓')
        window.dispatchEvent(new CustomEvent('sai-applied-refresh'))
      } else {
        clientLog('diff_apply_failed', {
          filename,
          fileId: effectiveFileId,
          changeCount: selectedChanges.length,
          error: String(errMsg).slice(0, 240),
          elapsedMs: Date.now() - started,
          fileExpanded,
        }, sessionId)
        toast.error(errMsg)
      }
    } finally {
      setApplying(false)
      setApplyProgress(null)
      releaseApplyLock(effectiveFileId)
    }
  }

  // Undo is file-level, not per-change: the backend reverts the file's most
  // recent saved version regardless of how many changes were bundled into
  // that save. So one click clears every change's applied/skipped state for
  // this file, not just the row the click happened to be near.
  const FILE_UNDO_KEY = '__file__'
  const handleFileUndo = async () => {
    setUndoing(p => ({ ...p, [FILE_UNDO_KEY]: true }))
    try {
      const result = await api.sessionFiles.undo(sessionId, effectiveFileId)
      let revertedAdded = 0, revertedRemoved = 0
      for (const c of realChanges) {
        if (!applied[c.id]) continue
        try { localStorage.removeItem(appliedKey(sessionId, c.id)) } catch {}
        try { localStorage.removeItem(skippedKey(sessionId, c.id)) } catch {}
        if (sessionId) api.surgical.unmarkApplied(sessionId, c.id).catch(() => {})
        if (c.diff) {
          const { added, removed } = diffLineCounts(c.diff)
          revertedAdded += added
          revertedRemoved += removed
        }
      }
      if (revertedAdded || revertedRemoved) revertDiffStats(effectiveFileId, revertedAdded, revertedRemoved)
      setApplied({})
      setSkipped({})
      setModifiedCode(undefined)
      // Sync originalCode to reverted content — replicates refresh behavior
      if (result.content) setOriginalCode(result.content)
      onApplied?.(filename, result.content)
      toast.success(`↩ Reverted ${filename} to previous version`)
      onChangeApplied?.(-1)
    } catch (e: any) {
      toast.error(e.message || 'Undo failed')
    } finally {
      setUndoing(p => ({ ...p, [FILE_UNDO_KEY]: false }))
    }
  }

  // ── Version history (browse + restore any past saved state) ──────────────
  const [showHistory, setShowHistory] = useState(false)
  const [versions, setVersions] = useState<{ id: string; lines: number; symbol_count: number; label: string; created_at: string }[] | null>(null)
  const [loadingVersions, setLoadingVersions] = useState(false)
  const [restoringId, setRestoringId] = useState<string | null>(null)

  // ── Restore confirm countdown (purely frontend) ───────────────────────
  // Restore is destructive-feeling (silently overwrites current content), so
  // clicking it "arms" a 3s countdown with a visible Cancel button instead
  // of restoring immediately. Only fires the real restore if the user does
  // not cancel and the countdown reaches 0. Never touches the backend API
  // until the countdown completes.
  const [armedVersionId, setArmedVersionId] = useState<string | null>(null)
  const [armCountdown, setArmCountdown] = useState(0)
  const armIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const armTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const clearArmTimers = () => {
    if (armIntervalRef.current) { clearInterval(armIntervalRef.current); armIntervalRef.current = null }
    if (armTimeoutRef.current) { clearTimeout(armTimeoutRef.current); armTimeoutRef.current = null }
  }

  const cancelArmedRestore = () => {
    clearArmTimers()
    setArmedVersionId(null)
    setArmCountdown(0)
  }

  const armRestore = (versionId: string) => {
    clearArmTimers()
    setArmedVersionId(versionId)
    setArmCountdown(3)
    armIntervalRef.current = setInterval(() => {
      setArmCountdown(c => (c > 1 ? c - 1 : 0))
    }, 1000)
    armTimeoutRef.current = setTimeout(() => {
      clearArmTimers()
      setArmedVersionId(null)
      setArmCountdown(0)
      handleRestoreVersion(versionId)
    }, 3000)
  }

  // Clean up timers on unmount so a pending restore never fires after the
  // card is gone, and cancel any armed restore if the history panel closes.
  useEffect(() => () => clearArmTimers(), [])
  useEffect(() => { if (!showHistory) cancelArmedRestore() }, [showHistory])

  const openHistory = async () => {
    setShowHistory(true)
    setLoadingVersions(true)
    try {
      const v = await api.sessionFiles.listVersions(sessionId, effectiveFileId)
      setVersions(v)
    } catch (e: any) {
      toast.error(e.message || 'Failed to load history')
      setVersions([])
    } finally {
      setLoadingVersions(false)
    }
  }

  const handleRestoreVersion = async (versionId: string) => {
    setRestoringId(versionId)
    try {
      const result = await api.sessionFiles.restoreVersion(sessionId, effectiveFileId, versionId)
      // Restoring changes file content out from under the applied/skipped
      // bookkeeping (same reasoning as file-level undo above) — clear it.
      for (const c of realChanges) {
        try { localStorage.removeItem(appliedKey(sessionId, c.id)) } catch {}
        try { localStorage.removeItem(skippedKey(sessionId, c.id)) } catch {}
      }
      setApplied({})
      setSkipped({})
      setModifiedCode(undefined)
      if (result.content) setOriginalCode(result.content)
      onApplied?.(filename, result.content)
      toast.success(`Restored ${filename} to selected version`)
      onChangeApplied?.()
      setShowHistory(false)
      // Refresh the list so the freshly-created "Before restore" checkpoint shows up next time
      setVersions(null)
    } catch (e: any) {
      toast.error(e.message || 'Restore failed')
    } finally {
      setRestoringId(null)
    }
  }

  const formatVersionTime = (iso: string) => {
    try {
      const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z')
      const diffMs = Date.now() - d.getTime()
      const mins = Math.round(diffMs / 60000)
      if (mins < 1) return 'just now'
      if (mins < 60) return `${mins}m ago`
      const hrs = Math.round(mins / 60)
      if (hrs < 24) return `${hrs}h ago`
      return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    } catch { return iso }
  }

  const handleDownload = async () => {
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, effectiveFileId)
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

  const skipAll = () => {
    for (const c of pendingChanges) saveSkipped(sessionId, c.id)
    setSkipped(p => ({
      ...p,
      ...Object.fromEntries(pendingChanges.map((c: any) => [c.id, true]))
    }))
    setChecked(Object.fromEntries(pendingChanges.map((c: any) => [c.id, false])))
  }

  return (
    <div className={`border rounded-xl mb-2 overflow-hidden ${
      allApplied ? 'border-success/30 bg-success/5' : 'border-border'
    }`}>
      {/* ── Collapsed-by-default file row (Cursor-like) ─────────────────── */}
      <div className={`flex items-center gap-2 px-3 py-2 ${fileExpanded ? 'bg-surface border-b border-border/50' : 'bg-surface/80'}`}>
        <button
          type="button"
          onClick={() => {
            const next = !fileExpanded
            setFileExpanded(next)
            clientLog('diff_file_expanded_toggled', {
              filename,
              expanded: next,
              changeCount: realChanges.length,
              pendingCount: pendingChanges.length,
            }, sessionId)
          }}
          className="flex items-center gap-1.5 min-w-0 flex-1 text-left rounded-md -ml-1 pl-1 py-0.5 hover:bg-overlay/40 transition-colors"
          title={fileExpanded ? 'Collapse file details' : 'Expand to review changes'}
          aria-expanded={fileExpanded}
        >
          <KeyboardArrowRight
            sx={{ fontSize: 16 }}
            className={`text-muted flex-shrink-0 transition-transform ${fileExpanded ? 'rotate-90' : ''}`}
          />
          <Description sx={{ fontSize: 14 }} className="text-accent flex-shrink-0" />
          <span className="text-[13px] font-semibold text-ink truncate">{filename}</span>
          <span className="text-[11px] text-muted/70 flex-shrink-0">
            {realChanges.length} change{realChanges.length !== 1 ? 's' : ''}
          </span>
          <span className="text-[11px] font-mono flex-shrink-0 tabular-nums">
            <span className="text-success">+{fileAddCount}</span>
            <span className="text-muted/40 mx-0.5">/</span>
            <span className="text-danger">-{fileRemoveCount}</span>
          </span>
        </button>

        <div
          className="flex items-center gap-1.5 flex-shrink-0 relative"
          onClick={e => e.stopPropagation()}
        >
          {Object.values(applied).some(Boolean) && (
            <button
              onClick={handleFileUndo}
              disabled={undoing[FILE_UNDO_KEY]}
              className="flex items-center gap-1 px-2 py-1 bg-surface text-muted border border-border rounded-lg text-[11px] font-semibold hover:bg-overlay hover:text-ink transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              title="Revert to the version before the most recent change"
            >
              <Replay sx={{ fontSize: 11 }} />
              {undoing[FILE_UNDO_KEY] ? 'Reverting...' : 'Undo'}
            </button>
          )}

          <button
            onClick={() => (showHistory ? setShowHistory(false) : openHistory())}
            className="flex items-center gap-1 px-2 py-1 bg-surface text-muted border border-border rounded-lg text-[11px] font-semibold hover:bg-overlay hover:text-ink transition-colors"
            title="Browse and restore any previous version of this file"
          >
            <History sx={{ fontSize: 12 }} />
            History
          </button>

          {showHistory && (
            <div className="absolute right-0 top-full mt-1.5 w-72 max-h-80 overflow-y-auto bg-surface border border-border rounded-lg shadow-lg z-20">
              <div className="flex items-center justify-between px-3 py-2 border-b border-border/60 sticky top-0 bg-surface">
                <span className="text-[11px] font-semibold uppercase tracking-wide text-muted/70">Version History</span>
                <button onClick={() => setShowHistory(false)} className="text-muted hover:text-ink">
                  <Close sx={{ fontSize: 14 }} />
                </button>
              </div>
              {loadingVersions && (
                <div className="px-3 py-3 text-[11px] text-muted">Loading…</div>
              )}
              {!loadingVersions && versions && versions.length === 0 && (
                <div className="px-3 py-3 text-[11px] text-muted">No saved versions yet — this file hasn't been edited in this session.</div>
              )}
              {!loadingVersions && versions && versions.map(v => (
                <div key={v.id} className="flex items-center justify-between gap-2 px-3 py-2 border-b border-border/40 last:border-0 hover:bg-overlay">
                  <div className="min-w-0">
                    <div className="text-[11px] font-medium text-ink truncate">{v.label || 'Edit'}</div>
                    <div className="text-[10px] text-muted/70">{formatVersionTime(v.created_at)} · {v.lines} lines</div>
                  </div>
                  {armedVersionId === v.id ? (
                    <div className="flex-shrink-0 flex items-center gap-1">
                      <span className="text-[10px] font-semibold text-accent tabular-nums" title="Restoring — click Cancel to stop">
                        Restoring in {armCountdown}s…
                      </span>
                      <button
                        onClick={cancelArmedRestore}
                        className="flex items-center gap-1 px-2 py-1 bg-surface text-muted border border-border rounded-md text-[10px] font-semibold hover:bg-overlay hover:text-ink transition-colors"
                        title="Cancel restore"
                        autoFocus
                      >
                        <Cancel sx={{ fontSize: 11 }} />
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => armRestore(v.id)}
                      disabled={restoringId === v.id || (armedVersionId !== null && armedVersionId !== v.id)}
                      className="flex-shrink-0 flex items-center gap-1 px-2 py-1 bg-accent/10 text-accent border border-accent/30 rounded-md text-[10px] font-semibold hover:bg-accent/20 transition-colors disabled:opacity-50"
                      title="Restores after a 3-second countdown — you'll get a chance to cancel"
                    >
                      {restoringId === v.id ? 'Restoring…' : 'Restore'}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          {isVisualFile(filename) && (
            <button
              onClick={async () => {
                const opening = !showFilePreview
                if (opening) {
                  try {
                    const freshFile = await api.sessionFiles.get(sessionId, effectiveFileId)
                    if (freshFile?.content) setOriginalCode(freshFile.content)
                  } catch {}
                  setModifiedCode(undefined)
                  setPreviewKey(k => k + 1)
                  setFileExpanded(true)
                  clientLog('diff_file_preview_opened', { filename, fileId: effectiveFileId }, sessionId)
                }
                setShowFilePreview(opening)
              }}
              className="flex items-center gap-1 px-2 py-1 bg-surface text-muted border border-border rounded-lg text-[11px] font-semibold hover:bg-overlay hover:text-ink transition-colors"
              title={showFilePreview ? 'Hide live preview' : 'Show live preview of this file'}
            >
              <Visibility sx={{ fontSize: 11 }} />
              {showFilePreview ? 'Hide Preview' : 'Preview'}
            </button>
          )}

          {allApplied ? (
            <span className="flex items-center gap-1.5 text-[12px] text-success font-semibold px-1">
              <CheckCircle sx={{ fontSize: 13 }} /> All applied
            </span>
          ) : (
            <button
              onClick={handleApplySelected}
              data-apply-btn
              disabled={selectedChanges.length === 0 || applying}
              className="flex items-center gap-1 px-2.5 py-1 bg-success/15 text-success border border-success/30 rounded-lg text-[11px] font-semibold hover:bg-success/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              title={selectedChanges.length === 0 ? 'No changes selected — expand to review checkboxes' : `Apply ${selectedChanges.length} selected change${selectedChanges.length !== 1 ? 's' : ''}`}
            >
              <CheckCircle sx={{ fontSize: 12 }} />
              {applying
                ? (applyProgress?.label || 'Applying…')
                : selectedChanges.length === 0
                  ? 'Apply'
                  : `Apply ${selectedChanges.length}`}
            </button>
          )}
        </div>
      </div>

      {applying && applyProgress && (
        <div className="px-3 pb-2 border-t border-border/40 bg-surface/60">
          <ApplyProgressStrip progress={applyProgress} startedAt={applyStartedAt} />
        </div>
      )}

      {fileExpanded && (
        <>
          {isVisualFile(filename) && showFilePreview && (
            <div className="border-t border-border">
              <LivePreview
                key={previewKey}
                code={originalCode || '// Loading...'}
                filename={filename}
                modifiedCode={modifiedCode}
                sessionId={sessionId}
                fileId={effectiveFileId}
              />
            </div>
          )}

          {realChanges.map((change: any, idx: number) => {
            const isBlocked = change.qa_result?.verdict === 'blocked'
            const isApplied = applied[change.id]
            const isSkipped = !isApplied && skipped[change.id]
            const isChecked = checked[change.id] && !isBlocked && !isSkipped
            const isExpanded = diffExpanded[change.id]

            const addCount = (change.diff || '').split('\n')
              .filter((l: string) => l.startsWith('+') && !l.startsWith('+++')).length
            const removeCount = (change.diff || '').split('\n')
              .filter((l: string) => l.startsWith('-') && !l.startsWith('---')).length

            return (
              <div key={change.id} className={`border-t border-border/60`}>
                <div className={`flex items-center gap-3 px-4 py-3 ${isApplied ? 'bg-success/5' : 'bg-base/60'}`}>
                  <div className="flex-shrink-0 w-5 flex items-center justify-center">
                    {isApplied ? (
                      <CheckCircle sx={{ fontSize: 16 }} className="text-success" />
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

                  <span className="w-5 h-5 flex items-center justify-center text-[11px] font-bold bg-accent/15 text-accent rounded-full flex-shrink-0">
                    {idx + 1}
                  </span>

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

                  <div className="flex items-center gap-1.5 flex-shrink-0 flex-wrap justify-end">
                    <span className="text-[11px] font-mono">
                      <span className="text-success">+{addCount}</span>
                      <span className="text-muted/40 mx-0.5">/</span>
                      <span className="text-danger">-{removeCount}</span>
                    </span>
                    <ConfidenceBadge change={change} />
                    {change.qa_result && <QABadge qa={change.qa_result} />}
                    {change.qa_result && <BlastRadius change={{ qa: change.qa_result }} />}
                    {change.confidence < 7 && !isBlocked && (
                      <span className="flex items-center gap-1 text-[10px] text-warning">
                        <Warning sx={{ fontSize: 10 }} /> Review
                      </span>
                    )}
                  </div>

                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    {isSkipped && !isApplied && (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-muted/20 text-muted/70 text-[10px] font-semibold border border-border/50">
                        <Cancel sx={{ fontSize: 9 }} /> Skipped
                      </span>
                    )}
                    {change.qa_result?.hard_blocked && onRetryWithQA && (
                      <button
                        onClick={() => {
                          const reportText = buildQARetryReportText(fileData.filename || filename, change)
                          onRetryWithQA(reportText)
                          toast.success('Sent the QA report back — asking for another fix...')
                        }}
                        className="flex items-center gap-1 px-2.5 py-1 bg-danger/10 text-danger border border-danger/40 rounded-lg text-[11px] font-semibold hover:bg-danger/20 transition-colors animate-pulse"
                        title="Send this failed QA report back to the agent for another fix — the same thing as pasting it into chat yourself"
                      >
                        <Replay sx={{ fontSize: 12 }} />
                        <span>Retry with QA</span>
                      </button>
                    )}

                    <button
                      onClick={() => toggleDiff(change.id)}
                      className="flex items-center gap-1 px-2.5 py-1 bg-surface text-muted border border-border rounded-lg text-[11px] font-semibold hover:bg-overlay hover:text-ink transition-colors"
                      title={isExpanded ? 'Collapse diff' : 'View diff'}
                    >
                      {isExpanded ? <KeyboardArrowUp sx={{ fontSize: 12 }} /> : <KeyboardArrowDown sx={{ fontSize: 12 }} />}
                      <span>Diff</span>
                    </button>
                  </div>
                </div>

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

          <div className="flex flex-col gap-2 px-4 py-3 bg-surface/60 border-t border-border">
            <div className="flex items-center gap-2">
            <button
              onClick={handleDownload}
              disabled={applying}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-surface text-muted border border-border rounded-lg text-[12px] font-semibold hover:bg-overlay transition-colors disabled:opacity-40"
              title="Download file with changes applied"
            >
              <FileDownload sx={{ fontSize: 12 }} /> Download
            </button>

            {!allApplied && (
              <div className="ml-auto flex items-center gap-2">
                {selectedChanges.length > 0 && (
                  <button
                    onClick={skipAll}
                    disabled={applying}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-surface text-muted border border-border rounded-lg text-[12px] font-semibold hover:bg-overlay transition-colors disabled:opacity-40"
                    title="Uncheck all pending changes"
                  >
                    <Cancel sx={{ fontSize: 12 }} /> Skip All
                  </button>
                )}

                <button
                  onClick={handleApplySelected}
                  data-apply-btn
                  disabled={selectedChanges.length === 0 || applying}
                  className="flex items-center gap-1.5 px-4 py-1.5 bg-success/15 text-success border border-success/30 rounded-lg text-[12px] font-semibold hover:bg-success/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  title={selectedChanges.length === 0 ? 'No changes selected — check at least one above' : `Apply ${selectedChanges.length} selected change${selectedChanges.length !== 1 ? 's' : ''}`}
                >
                  <CheckCircle sx={{ fontSize: 12 }} />
                  {applying
                    ? (applyProgress?.label || 'Applying...')
                    : selectedChanges.length === 0
                      ? 'Nothing selected'
                      : `Apply Selected (${selectedChanges.length})`
                  }
                </button>
              </div>
            )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

export function InlineDiffCard({ result, sessionId, onApplied, onRetryWithQA }: Props) {
  const [showTestRunner, setShowTestRunner] = useState(false)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [risksOpen, setRisksOpen] = useState(false)
  const fileEntries = Object.entries(result.changes_by_file)
  const totalChanges = fileEntries.reduce((sum, [, v]) => sum + v.changes.length, 0)
  const fileCount = fileEntries.length

  // Track how many changes have been applied to hide the risks alert
  const [appliedCount, setAppliedCount] = useState(0)
  const allApplied = appliedCount >= totalChanges

  return (
    <div className="mt-2">
      {/* Dense one-line result strip — reasoning is opt-in; risks have their own toggle */}
      <div className="flex items-center gap-2 mb-2 min-w-0">
        <span className="text-[13px] font-semibold text-ink truncate min-w-0">
          {result.summary || `${totalChanges} change${totalChanges !== 1 ? 's' : ''} ready`}
        </span>
        <span className="text-[11px] text-muted/70 flex-shrink-0 tabular-nums">
          {fileCount} file{fileCount !== 1 ? 's' : ''} · {totalChanges} change{totalChanges !== 1 ? 's' : ''}
        </span>
        {result.reasoning && (
          <button
            type="button"
            onClick={() => {
              const next = !detailsOpen
              setDetailsOpen(next)
              clientLog('diff_result_details_toggled', {
                open: next,
                fileCount,
                totalChanges,
              }, sessionId)
            }}
            className="ml-auto flex-shrink-0 text-[11px] text-muted hover:text-ink font-semibold px-1.5 py-0.5 rounded border border-border/60 hover:bg-overlay transition-colors"
          >
            {detailsOpen ? 'Hide details' : 'Details'}
          </button>
        )}
      </div>

      {detailsOpen && result.reasoning && (
        <p className="text-[12px] text-muted/70 mb-2 italic">{result.reasoning}</p>
      )}

      {/* Per-file cards — each collapsed by default */}
      {fileEntries.map(([filename, fileData]) => (
        <FileChangeCard
          key={filename}
          filename={filename}
          fileData={fileData}
          sessionId={sessionId}
          onApplied={onApplied}
          onChangeApplied={(delta = 1) => { setAppliedCount(n => Math.max(0, n + delta)); setShowTestRunner(true) }}
          onRetryWithQA={onRetryWithQA}
        />
      ))}

      {/* Skipped changes notice */}
      {result.skipped_changes && result.skipped_changes.length > 0 && (
        <>
          {/* already_matches — quiet grey, genuinely already done */}
          {result.skipped_changes.filter((s: any) => s.reason === 'already_matches').length > 0 && (
            <div className="mt-2 flex items-start gap-2 px-3 py-2 bg-surface/60 border border-border/50 rounded-lg">
              <SkipNext sx={{ fontSize: 13 }} className="text-muted/60 mt-0.5 flex-shrink-0" />
              <div className="text-[12px] text-muted/80">
                <strong className="text-ink/60">Skipped {result.skipped_changes.filter((s: any) => s.reason === 'already_matches').length} symbol{result.skipped_changes.filter((s: any) => s.reason === 'already_matches').length !== 1 ? 's' : ''}:</strong>{' '}
                {result.skipped_changes.filter((s: any) => s.reason === 'already_matches').map((s: any, i: number, arr: any[]) => (
                  <span key={i}>
                    <code className="text-[11px] text-accent/70">{s.symbol}</code>
                    {' — code already matches'}
                    {i < arr.length - 1 ? '; ' : ''}
                  </span>
                ))}
              </div>
            </div>
          )}
          {/* no_visible_diff — yellow warning, needs manual review */}
          {result.skipped_changes.filter((s: any) => s.reason === 'no_visible_diff').length > 0 && (
            <div className="mt-2 flex items-start gap-2 px-3 py-2 bg-warning/10 border border-warning/40 rounded-lg">
              <Warning sx={{ fontSize: 13 }} className="text-warning mt-0.5 flex-shrink-0" />
              <div className="text-[12px] text-warning">
                <strong>⚠️ Unverified {result.skipped_changes.filter((s: any) => s.reason === 'no_visible_diff').length} symbol{result.skipped_changes.filter((s: any) => s.reason === 'no_visible_diff').length !== 1 ? 's' : ''}:</strong>{' '}
                {result.skipped_changes.filter((s: any) => s.reason === 'no_visible_diff').map((s: any, i: number, arr: any[]) => (
                  <span key={i}>
                    <code className="text-[11px] text-warning/80">{s.symbol}</code>
                    {' — Surgeon produced no diff. The AI may have matched a nearby pattern instead of verifying this exact function. Please inspect it manually.'}
                    {i < arr.length - 1 ? '; ' : ''}
                  </span>
                ))}
              </div>
            </div>
          )}
        </>
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
            <button
              type="button"
              onClick={() => {
                const next = !risksOpen
                setRisksOpen(next)
                clientLog('diff_risks_toggled', {
                  open: next,
                  riskCount: result.risks.length,
                }, sessionId)
              }}
              className="w-full flex items-center gap-2 text-left"
            >
              <Warning sx={{ fontSize: 13 }} className="text-warning flex-shrink-0" />
              <span className="text-[12px] font-semibold text-warning flex-1">
                {hasVerdicts
                  ? allResolved
                    ? `✅ All ${result.risks.length} risks reviewed — safe to apply`
                    : `Risks: ${resolvedCount}/${result.risks.length} verified safe`
                  : `${result.risks.length} risk${result.risks.length !== 1 ? 's' : ''}`}
              </span>
              <span className={`text-[10px] text-warning/80 transition-transform inline-flex ${risksOpen ? 'rotate-90' : ''}`}>
                <KeyboardArrowRight sx={{ fontSize: 14 }} />
              </span>
            </button>
            {risksOpen && (
            <ul className="space-y-1.5 list-none mt-1.5">
              {result.risks.map((r: string, i: number) => {
                const verdict = allVerdicts[r]
                return (
                  <li key={i} className="text-[11.5px]">
                    {verdict ? (
                      <div className="flex flex-col gap-0.5">
                        <div className={`flex items-start gap-1.5 font-medium ${statusColor[verdict.status] || 'text-warning'}`}>
                          <span className="flex-shrink-0">{statusIcon[verdict.status] || '•'}</span>
                          <span className={verdict.status === 'verified_safe' ? 'line-through opacity-80' : ''}>{r}</span>
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
            )}
          </div>
        )
      })()}
    </div>
  )
}
