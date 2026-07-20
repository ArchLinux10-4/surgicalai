import React, { useState, useRef, useEffect, useCallback, Component } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAppStore, type PickedElementRef } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { InlineDiffCard } from './InlineDiffCard'
import { NewFileCard } from './NewFileCard'
import { MarkdownCode } from './CodeBlock'
import { SessionFilesTray } from './SessionFilesTray'
import { PickedElementsTray } from './PickedElementsTray'
import { AgentMissionControl } from './AgentMissionControl'
import { useTaskPolling } from '../hooks/useTaskPolling'
import type { SessionFile, SmartResult } from '../types'
import { AccountTree, Add, AdsClick, AttachFile, AttachMoney, AutoFixHigh, Biotech, Bolt, BugReport, Close, Delete, Description, DoneAll, LightbulbOutlined, Lock, Psychology, Security, Send, Warning } from '@mui/icons-material';
import { VoiceButton } from './VoiceButton'
import { validateFileSize } from '../utils/fileValidation'


// ── Chat mode selector ────────────────────────────────────────────────────────
// Explicit user-selected intent (like Cursor/Copilot/Cline). Ask/Plan stream a
// plain answer with no edit pipeline; Edit/Agent are the existing code paths.
type ChatMode = 'edit' | 'ask' | 'plan' | 'agent'
const MODE_META: Record<ChatMode, { icon: typeof AutoFixHigh; label: string; desc: string }> = {
  edit:  { icon: AutoFixHigh,       label: 'Edit',  desc: 'Code edits with QA review' },
  ask:   { icon: LightbulbOutlined, label: 'Ask',   desc: 'Questions & research — no edits' },
  plan:  { icon: Description,       label: 'Plan',  desc: 'Implementation plan — no edits' },
  agent: { icon: AccountTree,       label: 'Agent', desc: 'Multi-agent task breakdown (Claude)' },
}
const CHAT_MODES: ChatMode[] = ['edit', 'ask', 'plan', 'agent']
// Per-mode accent color — all existing theme tokens (tailwind.config.js), no new
// colors introduced. Edit stays neutral (it's the baseline/default action).
// `dot` is a separate literal (not derived via string replace) so Tailwind's
// static content scanner can see every class name it needs to generate.
const MODE_COLOR: Record<ChatMode, { text: string; bg: string; border: string; dot: string }> = {
  edit:  { text: 'text-muted/70', bg: 'bg-overlay/60',  border: 'border-border/80', dot: 'bg-muted/70' },
  ask:   { text: 'text-accent',   bg: 'bg-accent/15',   border: 'border-accent/50', dot: 'bg-accent' },
  plan:  { text: 'text-purple',   bg: 'bg-purple/15',   border: 'border-purple/50', dot: 'bg-purple' },
  agent: { text: 'text-orange',   bg: 'bg-orange/15',   border: 'border-orange/50', dot: 'bg-orange' },
}

// ── Cost indicator for model picker ───────────────────────────────────────────
function ModelCostIndicator({ cost }: { cost?: number }) {
  if (!cost) return null
  const filled = cost
  const empty = 4 - cost
  return (
    <span className="inline-flex items-center ml-1.5" title={`Cost tier: ${cost}/4`}>
      {Array.from({ length: filled }, (_, i) => (
        <AttachMoney key={`f${i}`} sx={{ fontSize: 11, color: cost >= 4 ? '#ef4444' : cost >= 3 ? '#f59e0b' : '#22c55e', marginLeft: '-3px' }} />
      ))}
      {Array.from({ length: empty }, (_, i) => (
        <AttachMoney key={`e${i}`} sx={{ fontSize: 11, opacity: 0.2, marginLeft: '-3px' }} />
      ))}
    </span>
  )
}

// ── Strip internal protocol tags from model output ────────────────────────────
function stripInternalTags(text: string, streaming = false): string {
  if (!text) return text
  let result = text
    .replace(/<edit_plan>[\s\S]*?<\/edit_plan>/g, '')
    .replace(/<search_request>[\s\S]*?<\/search_request>/g, '')
  if (streaming) {
    // During streaming: hide from start of any incomplete opening tag
    result = result
      .replace(/<edit_plan>[\s\S]*$/, '')
      .replace(/<search_request>[\s\S]*$/, '')
  }
  return result.trim()
}

// ── Apply All Button — applies every unapplied change across all messages ─────
function ApplyAllButton({ messages, sessionId, sessionFiles, setSessionFiles }: {
  messages: any[]
  sessionId: string
  sessionFiles: SessionFile[]
  setSessionFiles: (files: SessionFile[]) => void
}) {
  const [applying, setApplying] = useState(false)
  const [done, setDone]         = useState(false)
  const [appliedIds, setAppliedIds] = useState<Set<string>>(new Set())

  // Fetch applied IDs from DB on mount + re-sync when individual cards apply changes
  useEffect(() => {
    const fetchApplied = () => {
      api.surgical.getApplied(sessionId)
        .then(({ applied_ids }: { applied_ids: string[] }) => setAppliedIds(new Set(applied_ids)))
        .catch(() => {})
    }
    fetchApplied()
    window.addEventListener('sai-applied-refresh', fetchApplied)
    return () => window.removeEventListener('sai-applied-refresh', fetchApplied)
  }, [sessionId])

  // Collect all messages with surgical data
  const pendingMessages = messages.filter(m =>
    (m.message_type === 'natural_result' || m.message_type === 'surgical_result') &&
    m.surgical_data
  )

  if (pendingMessages.length === 0) return null

  // Count only UNAPPLIED changes — diff cards' applied state is source of truth.
  // QA-blocked changes are NOT bulk-applyable: the per-row checkbox disables them
  // (see InlineDiffCard), so Apply All must respect the same gate. We track clean
  // (applyable) vs flagged (QA-blocked) separately so the button can show the split.
  let cleanChanges   = 0
  let flaggedChanges = 0
  const fileSet      = new Set<string>()
  for (const msg of pendingMessages) {
    try {
      const result: SmartResult = JSON.parse(msg.surgical_data)
      for (const [fname, fd] of Object.entries(result.changes_by_file || {})) {
        const changes = (fd as any)?.changes || []
        const unapplied = changes.filter((c: any) => c.id && !appliedIds.has(c.id))
        const clean   = unapplied.filter((c: any) => c.qa_result?.verdict !== 'blocked')
        cleanChanges   += clean.length
        flaggedChanges += unapplied.length - clean.length
        if (clean.length > 0) fileSet.add(fname)
      }
      // new_files are not edits — don't count them as changes to apply
    } catch {}
  }
  const totalFiles = fileSet.size

  // Nothing clean to bulk-apply → hide the button (flagged changes stay visible
  // on their individual cards for manual review).
  if (cleanChanges === 0) return null

  const handleApplyAll = async () => {
    setApplying(true)
    let appliedFiles   = 0
    let failed         = 0
    let appliedChanges = 0
    let rescuedChanges = 0
    let failedChanges  = 0
    let firstFailReason = ''

    const markPromises: Promise<any>[] = []
    try {
      for (const msg of pendingMessages) {
        let result: SmartResult
        try { result = JSON.parse(msg.surgical_data) } catch { continue }

        // Apply edits per file
        for (const [, fileData] of Object.entries(result.changes_by_file || {})) {
          const fd = fileData as any
          if (!fd?.file_id || !fd?.changes?.length) continue
          // Only bulk-apply QA-clean changes. QA-blocked changes must be reviewed
          // and applied individually — same gate the per-row checkbox enforces.
          const applyChanges = fd.changes.filter((c: any) => c?.qa_result?.verdict !== 'blocked')
          if (applyChanges.length === 0) continue
          try {
            const current = await api.sessionFiles.get(sessionId, fd.file_id)
            const applied = await api.surgical.applyAll({
              file_path: fd.filename,
              changes: applyChanges,
              file_content: current.content,
            })
            if (applied.modified_content) {
              await api.sessionFiles.update(sessionId, fd.file_id, applied.modified_content,
                `Applied ${applyChanges.length} change${applyChanges.length !== 1 ? 's' : ''}`)
              appliedFiles++
              appliedChanges += applied.applied_count ?? applyChanges.length
              rescuedChanges += applied.rescued_count ?? 0
              // Truthful accounting: the engine reports exactly which changes
              // failed — those stay UNAPPLIED so they remain visible/retryable.
              // Exception: "already_applied" entries are a structural
              // idempotency check (new_code already found in current file
              // content, done server-side — see surgical_editor.py), not a
              // real failure. Without this split, a change already applied
              // via a different diff card / a stale change.id would fail
              // every Apply All forever and keep showing as available.
              const allFailed = applied.failed_changes || []
              const alreadyAppliedIds = new Set(
                allFailed.filter((f: any) => f.already_applied).map((f: any) => f.change_id).filter(Boolean)
              )
              const realFailed = allFailed.filter((f: any) => !f.already_applied)
              const failedIds = new Set(realFailed.map((f: any) => f.change_id).filter(Boolean))
              failedChanges += realFailed.length
              if (realFailed.length > 0 && !firstFailReason) {
                firstFailReason = realFailed[0]?.reason || ''
              }
              for (const ch of applyChanges) {
                if (ch?.id && (alreadyAppliedIds.has(ch.id) || !failedIds.has(ch.id))) {
                  markPromises.push(api.surgical.markApplied(sessionId, ch.id).catch(() => {}))
                }
              }
            }
          } catch { failed++ }
        }
      }

      // Refresh file list
      const fresh = await api.sessionFiles.list(sessionId)
      setSessionFiles(fresh)

      // Wait for all DB marks to land before telling diff cards refresh
      await Promise.all(markPromises)

      const rescuedNote = rescuedChanges > 0 ? ` (${rescuedChanges} AI-rescued)` : ''
      if (failed === 0 && failedChanges === 0) {
        toast.success(
          `Applied ${appliedChanges} change${appliedChanges !== 1 ? 's' : ''} across ` +
          `${appliedFiles} file${appliedFiles !== 1 ? 's' : ''}${rescuedNote}`
        )
        window.dispatchEvent(new CustomEvent('sai-applied-refresh'))
        setDone(true)
      } else if (failedChanges > 0) {
        toast.error(
          `Applied ${appliedChanges}${rescuedNote}, but ${failedChanges} ` +
          `change${failedChanges !== 1 ? 's' : ''} could not be applied`,
          firstFailReason ? firstFailReason.slice(0, 200) : undefined
        )
        window.dispatchEvent(new CustomEvent('sai-applied-refresh'))
      } else {
        toast.error(`Applied ${appliedFiles} file(s) — ${failed} file(s) failed entirely`)
      }
    } catch (e: any) {
      toast.error(e.message || 'Apply all failed')
    } finally {
      setApplying(false)
    }
  }

  if (done) return null

  return (
    <button
      onClick={handleApplyAll}
      disabled={applying}
      className="flex items-center gap-2 px-3.5 py-2 rounded-xl border text-[12px] font-medium transition-all
                 bg-success/10 border-success/30 text-success
                 hover:bg-success/20 hover:border-success/50 hover:text-success
                 disabled:opacity-50 disabled:cursor-wait"
    >
      <DoneAll sx={{ fontSize: 14 }} />
      {applying
        ? 'Applying…'
        : flaggedChanges > 0
          ? `Apply All  ·  ${cleanChanges} clean change${cleanChanges !== 1 ? 's' : ''} ` +
            `(${flaggedChanges} QA-flagged, apply individually)`
          : `Apply All  ·  ${cleanChanges} change${cleanChanges !== 1 ? 's' : ''} across ${totalFiles} file${totalFiles !== 1 ? 's' : ''}`
      }
    </button>
  )
}

// ── Markdown component overrides ──────────────────────────
const mdComponents = {
  code: (({ className, children, ...props }: any) => {
    const lang = /language-(\w+)/.exec(className || '')?.[1] || ''
    return <MarkdownCode className={className} {...props}>{children}</MarkdownCode>
  }) as any,
  // Beautiful prose overrides
  h1: ({ children }: any) => (
    <h1 className="text-lg font-bold text-ink mt-5 mb-2 border-b border-border/50 pb-1.5">{children}</h1>
  ),
  h2: ({ children }: any) => (
    <h2 className="text-base font-bold text-ink mt-4 mb-1.5">{children}</h2>
  ),
  h3: ({ children }: any) => (
    <h3 className="text-sm font-semibold text-ink mt-3 mb-1">{children}</h3>
  ),
  p: ({ children }: any) => (
    <p className="text-sm text-ink/80 leading-7 mb-3 last:mb-0">{children}</p>
  ),
  ul: ({ children }: any) => (
    <ul className="my-2 space-y-1 pl-1">{children}</ul>
  ),
  ol: ({ children }: any) => (
    <ol className="my-2 space-y-1 pl-1 list-decimal list-inside">{children}</ol>
  ),
  li: ({ children }: any) => (
    <li className="flex items-start gap-2 text-sm text-ink/80 leading-6">
      <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-accent/70 flex-shrink-0 list-none" />
      <span>{children}</span>
    </li>
  ),
  blockquote: ({ children }: any) => (
    <blockquote className="my-3 pl-4 border-l-2 border-accent/50 text-muted italic text-sm leading-6">{children}</blockquote>
  ),
  strong: ({ children }: any) => (
    <strong className="font-semibold text-ink">{children}</strong>
  ),
  em: ({ children }: any) => (
    <em className="text-ink/80 italic">{children}</em>
  ),
  a: ({ children, href }: any) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent hover:text-accent underline underline-offset-2 transition-colors">{children}</a>
  ),
  hr: () => <hr className="my-4 border-border/50" />,
  table: ({ children }: any) => (
    <div className="my-3 overflow-x-auto rounded-lg border border-border/60">
      <table className="w-full text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }: any) => <thead className="bg-surface/80">{children}</thead>,
  th: ({ children }: any) => <th className="px-3 py-2 text-left text-xs font-semibold text-ink/80 uppercase tracking-wide">{children}</th>,
  td: ({ children }: any) => <td className="px-3 py-2 text-muted text-sm border-t border-border/40">{children}</td>,
}

// ── Helpers ───────────────────────────────────────────────
function getLanguage(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const m: Record<string, string> = {
    py: 'python', js: 'javascript', ts: 'typescript',
    tsx: 'typescriptreact', jsx: 'javascriptreact',
    go: 'go', rs: 'rust', java: 'java', cs: 'csharp',
    rb: 'ruby', php: 'php', swift: 'swift', kt: 'kotlin',
    html: 'html', css: 'css', json: 'json', md: 'markdown',
    sh: 'bash', sql: 'sql', yaml: 'yaml', yml: 'yaml', toml: 'toml',
  }
  return m[ext] || 'plaintext'
}

// ── AI avatar ─────────────────────────────────────────────
function AIAvatar() {
  return (
    <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-accent to-purple flex items-center justify-center flex-shrink-0 shadow-md shadow-accent/20">
      <Bolt sx={{ fontSize: 14 }} className="text-white" />
    </div>
  )
}

// ── Diff card error boundary ──────────────────────────────
class DiffCardBoundary extends Component<{ children: React.ReactNode }, { hasError: boolean; error: string }> {
  constructor(props: any) {
    super(props)
    this.state = { hasError: false, error: '' }
  }
  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error: error.message }
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="border border-danger/40 rounded-xl p-4 bg-danger/10 text-sm text-danger">
          <strong>Diff card error:</strong> {this.state.error}
          <br /><span className="text-xs text-danger mt-1 block">The change was planned but could not be displayed. Check the console for details.</span>
        </div>
      )
    }
    return this.props.children
  }
}

// ── Message bubble ────────────────────────────────────────
function CompactMarkerChip({ msg }: { msg: any }) {
  const [open, setOpen] = useState(false)
  const summary: string = msg.compact_summary || ''
  const count: number = msg.compact_count || 0
  const hasSummary = summary.trim().length > 0
  return (
    <div className="flex flex-col items-center py-2 px-4">
      <button
        type="button"
        onClick={() => hasSummary && setOpen(o => !o)}
        className={`flex items-center gap-1.5 px-3 py-1 bg-surface/60 border border-border/50 rounded-full ${hasSummary ? 'cursor-pointer hover:bg-surface/90' : 'cursor-default'}`}
        title={hasSummary ? 'Click to see exactly what was kept vs. condensed' : undefined}
      >
        <span className="text-[11px] text-muted/70">
          📦 Earlier conversation compacted{count ? ` (${count} messages)` : ''}
        </span>
        {hasSummary && <span className="text-[10px] text-muted/50">{open ? '▲ hide' : '▼ view summary'}</span>}
      </button>
      {open && hasSummary && (
        <div className="mt-2 max-w-[90%] w-full sm:w-[520px] text-[12px] leading-relaxed bg-surface/40 border border-border/40 rounded-lg p-3 whitespace-pre-wrap text-fg/80">
          <div className="text-[10px] uppercase tracking-wide text-muted/60 mb-1.5">
            What the model retained from the condensed history:
          </div>
          {summary}
        </div>
      )}
    </div>
  )
}

function Message({ msg, sessionId }: { msg: any; sessionId: string }) {
  const isUser = msg.role === 'user'
  const isSurgical = msg.message_type === 'surgical_result'
  const isNaturalResult = msg.message_type === 'natural_result'
  const time = msg.created_at
    ? new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : ''
  const [copied, setCopied] = useState(false)
  const handleCopy = () => {
    const text = stripInternalTags(msg.content || '')
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  // Compact marker chip — persisted (see backend __COMPACTION_EVENT__ rows),
  // so it survives reload. Clickable to reveal exactly what was kept in the
  // summary that replaced the older turns, instead of just a toast claiming
  // "compaction happened" with no way to audit what it did.
  if (msg.message_type === 'compact_marker') {
    return <CompactMarkerChip msg={msg} />
  }

  let surgicalResult: SmartResult | null = null
  if ((isSurgical || isNaturalResult) && msg.surgical_data) {
    try { surgicalResult = JSON.parse(msg.surgical_data) } catch {}
  }

  // ── User bubble (right-aligned) ──
  if (isUser) {
    const { visibleText, elements: pickedCtx } = parsePickedElementsFromContent(msg.content || '')
    return (
      <div className="flex justify-end px-4 py-3 group">
        <div className="max-w-[78%]">
          <div className="flex items-center justify-end gap-2 mb-1">
            <span className="text-[10px] text-faint opacity-0 group-hover:opacity-100 transition-opacity">{time}</span>
            <span className="text-[11px] font-medium text-muted/70">You</span>
          </div>
          {visibleText.trim() && (
            <div className="bg-overlay/60 border border-border/40 rounded-2xl rounded-tr-sm px-4 py-2.5 text-sm text-ink leading-relaxed whitespace-pre-wrap shadow-sm">
              {visibleText}
            </div>
          )}
          <PickedElementsInMessage elements={pickedCtx} />
        </div>
      </div>
    )
  }

  // ── AI bubble (left-aligned) ──
  return (
    <div className="flex items-start gap-3 px-4 py-4 group">
      <AIAvatar />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-[12px] font-semibold text-ink/80">SurgicalAI</span>
          {msg._model && <span className="text-[9px] font-mono text-muted/60 bg-overlay/50 px-1.5 py-0.5 rounded">{msg._model}</span>}
          {msg._aborted && <span className="text-[9px] font-medium text-danger/80 bg-danger/10 px-1.5 py-0.5 rounded">Stopped</span>}
          <span className="text-[10px] text-faint opacity-0 group-hover:opacity-100 transition-opacity">{time}</span>
        </div>

        {/* Persistent phase trail — PipelineTimeline for edits, PersistentSteps for text */}
        {(msg._thinking || (msg._steps && msg._steps.filter((s: string) => s !== 'Thinking...').length > 0)) && (
          <div className="mb-3 space-y-1">
            {msg._steps && (
              (isSurgical || isNaturalResult)
                ? <PipelineTimeline steps={msg._steps} />
                : <PersistentSteps steps={msg._steps} />
            )}
            {msg._thinking && <ThinkingBlock text={msg._thinking} isStreaming={false} />}
          </div>
        )}

        {isSurgical && surgicalResult ? (
          <DiffCardBoundary>
            {surgicalResult.intent === 'create' ? (
              <NewFileCard result={surgicalResult} sessionId={sessionId} />
            ) : (
              <InlineDiffCard result={surgicalResult} sessionId={sessionId} />
            )}
          </DiffCardBoundary>
        ) : isNaturalResult ? (
          /* Natural result: show markdown text first, then diff card below */
          <div className="space-y-3">
            {msg.content && (
              <div className="prose-ai">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                  {stripInternalTags(msg.content)}
                </ReactMarkdown>
              </div>
            )}
            {surgicalResult && (
              <DiffCardBoundary>
                {surgicalResult.intent === 'create' ? (
                  <NewFileCard result={surgicalResult} sessionId={sessionId} />
                ) : (
                  <InlineDiffCard result={surgicalResult} sessionId={sessionId} />
                )}
              </DiffCardBoundary>
            )}
          </div>
        ) : (
          <div className="prose-ai">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
              {stripInternalTags(msg.content)}
            </ReactMarkdown>
          </div>
        )}

        {/* Copy button — hover-reveal, bottom-right of AI bubble */}
        {!isUser && msg.content && (
          <div className="flex justify-end mt-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
            <button
              onClick={handleCopy}
              className="flex items-center gap-1 text-[11px] text-muted/60 hover:text-ink/70 transition-colors px-2 py-0.5 rounded hover:bg-overlay/50"
              title="Copy response"
            >
              {copied ? (
                <><span>✓</span><span>Copied</span></>
              ) : (
                <><span>⎘</span><span>Copy</span></>
              )}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Streaming bubble with thinking trail ──────────────────
// ── Claude Thinking Block ─────────────────────────────────
function ThinkingBlock({ text, isStreaming }: { text: string; isStreaming: boolean }) {
  const [expanded, setExpanded] = useState(isStreaming)

  useEffect(() => {
    if (isStreaming) setExpanded(true)
    else setExpanded(false)
  }, [isStreaming])

  if (!text && !isStreaming) return null

  return (
    <div className="mb-3">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-xs text-purple hover:text-purple transition-colors"
      >
        <span className={`transform transition-transform duration-200 ${expanded ? 'rotate-90' : ''}`}>▶</span>
        <Psychology sx={{ fontSize: 12 }} />
        {isStreaming ? (
          <span className="flex items-center gap-1.5">
            Thinking<span className="animate-pulse">…</span>
          </span>
        ) : (
          <span>Claude's reasoning ({text.length > 1000 ? `${Math.round(text.length / 100)} steps` : 'click to view'})</span>
        )}
      </button>
      {expanded && text && (
        <div className="mt-2 ml-5 pl-3 border-l-2 border-purple/30 text-[12px] text-muted/90 whitespace-pre-wrap max-h-80 overflow-y-auto leading-relaxed font-mono">
          {text}
          {isStreaming && <span className="inline-block w-1.5 h-3 bg-purple/60 rounded-sm ml-0.5 animate-pulse" />}
        </div>
      )}
    </div>
  )
}

// ── Persistent steps trail (shown on non-surgical completed messages) ────
function PersistentSteps({ steps }: { steps: string[] }) {
  const [expanded, setExpanded] = useState(false)
  const allSteps = steps.filter(s => s !== 'Thinking...')
  if (!allSteps.length) return null
  return (
    <div className="mb-2">
      <button
        onClick={() => setExpanded(e => !e)}
        className="text-[11px] text-muted/70 hover:text-ink/80 flex items-center gap-1 transition-colors"
      >
        <span>{expanded ? '▾' : '▸'}</span>
        <span>{allSteps.length} step{allSteps.length !== 1 ? 's' : ''} completed</span>
      </button>
      {expanded && (
        <div className="mt-1.5 pl-3 border-l-2 border-border/60 space-y-1">
          {allSteps.map((step, i) => (
            <div key={i} className="text-[11px] text-muted/70 flex items-center gap-1.5">
              <span className="text-success/80">✓</span>
              <span>{step}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Pipeline Phase Timeline ─────────────────────────────────────────────
// Structured phase indicator for surgical edits: Analyze → Code → QA → Done
// Replaces raw step list with visual pipeline. Always visible, no collapse.
function PipelineTimeline({ steps, isLive = false }: { steps: string[]; isLive?: boolean }) {
  const filtered = steps.filter(s => s !== 'Thinking...')
  if (!filtered.length) return null

  // Detect furthest phase reached from progress text
  let reached = 0  // 0=none, 1=analyze, 2=code, 3=qa
  let healed = false
  let qaScore = ''
  let qaIcon = ''

  for (const s of filtered) {
    if (/reading \d+ file|parsing file|found \d+ symbol|looking up|loaded:|claude is analyzing/i.test(s))
      reached = Math.max(reached, 1)
    if (/preparing code|narrowing:/i.test(s))
      reached = Math.max(reached, 2)
    if (/running qa|QA\s*[✅⚠️🚫⏭]/i.test(s))
      reached = Math.max(reached, 3)
    if (/🔁|surgeon retry|auto.fix|fixing blocked/i.test(s))
      healed = true
    const scoreMatch = s.match(/score:\s*(\d+)/)
    const iconMatch = s.match(/QA\s*(✅|⚠️|🚫|⏭)/)
    if (scoreMatch) qaScore = scoreMatch[1] + '/10'
    if (iconMatch) qaIcon = iconMatch[1]
  }

  // When stream is done and we reached at least code phase, mark as done
  if (!isLive && reached >= 2) reached = 4
  // If live and we have steps but nothing matched, default to analyze
  if (isLive && reached === 0 && filtered.length > 0) reached = 1

  // Build QA label
  const qaLabel = healed && isLive && reached === 3
    ? 'Auto-Healing...'
    : `${healed ? '🔁 ' : ''}QA${qaScore ? ` ${qaScore}` : ''}${qaIcon ? ` ${qaIcon}` : ''}`

  const phases = [
    { label: 'Analyze', n: 1 },
    { label: 'Code', n: 2 },
    { label: qaLabel, n: 3, isHeal: healed && isLive && reached === 3 },
    { label: 'Done', n: 4 },
  ]

  return (
    <div className="flex items-center gap-1 mb-2 py-1">
      {phases.map((p, i) => {
        const done = isLive ? p.n < reached : p.n <= reached
        const active = isLive && p.n === reached

        const textCls = done
          ? 'text-success'
          : active
            ? p.isHeal ? 'text-amber-400' : 'text-accent'
            : 'text-muted/40'

        const pillBg = done
          ? 'bg-success/8'
          : active
            ? p.isHeal
              ? 'bg-amber-500/10 shadow-sm shadow-amber-500/20'
              : 'bg-accent/10 shadow-sm shadow-accent/20'
            : ''

        const dotCls = done
          ? 'bg-success'
          : active
            ? `${p.isHeal ? 'bg-amber-500' : 'bg-accent'} animate-pulse`
            : 'bg-muted/30'

        return (
          <React.Fragment key={i}>
            {i > 0 && (
              <div className={`h-px w-3 sm:w-5 ${done || active ? 'bg-success/30' : 'bg-border/30'}`} />
            )}
            <span className={`flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded-full whitespace-nowrap ${textCls} ${pillBg}`}>
              {done && <span>✓</span>}
              {active && <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${dotCls}`} />}
              {p.label}
            </span>
          </React.Fragment>
        )
      })}
    </div>
  )
}

function StreamingBubble({ content, progress, progressHistory, thinkingText, isThinking, isBuildingEdit }: { content: string; progress: string; progressHistory: string[]; thinkingText?: string; isThinking?: boolean; isBuildingEdit?: boolean }) {
  const [elapsed, setElapsed] = useState(0)

  // Elapsed timer — ticks every second while streaming
  useEffect(() => {
    const start = Date.now()
    const interval = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000)
    return () => clearInterval(interval)
  }, [])

  const completedSteps = progressHistory.slice(0, -1)
  const hasSteps = completedSteps.length > 0

  return (
    <div className="flex items-start gap-3 px-4 py-4">
      {/* Pulsing avatar while streaming */}
      <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-accent to-purple flex items-center justify-center flex-shrink-0 shadow-md shadow-accent/20 animate-pulse">
        <Bolt sx={{ fontSize: 14 }} className="text-white" />
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-[12px] font-semibold text-ink/80">SurgicalAI</span>
          {/* Current progress badge — QA and Auto-Heal get distinct styling */}
          {progress && (() => {
            const isHeal = /🔁|surgeon retry|auto.fix|fixing blocked/i.test(progress)
            const isQA = /running qa|QA\s*[✅⚠️🚫⏭]/i.test(progress)
            const badgeCls = isHeal
              ? 'text-amber-400 bg-amber-500/10 border-amber-500/25 shadow-sm shadow-amber-500/10'
              : isQA
                ? 'text-blue-400 bg-blue-500/10 border-blue-500/25'
                : 'text-accent bg-accent/10 border-accent/20'
            const dotCls = isHeal ? 'bg-amber-400' : isQA ? 'bg-blue-400' : 'bg-accent'
            return (
              <span className={`text-[11px] flex items-center gap-1.5 px-2 py-0.5 rounded-full border font-medium ${badgeCls}`}>
                <span className={`w-1.5 h-1.5 rounded-full animate-pulse ${dotCls}`} />
                {progress}
              </span>
            )
          })()}
          {/* Elapsed timer */}
          {elapsed > 0 && !content && (
            <span className="text-[10px] text-muted/70 tabular-nums">{elapsed}s</span>
          )}
        </div>

        {/* Live pipeline phase timeline */}
        {hasSteps && !content && (
          <PipelineTimeline steps={completedSteps} isLive />
        )}

        {/* Claude thinking block */}
        {(thinkingText || isThinking) && (
          <ThinkingBlock text={thinkingText || ''} isStreaming={!!isThinking} />
        )}

        {/* Building edit indicator — shown while parsing a <surgical_edit> block */}
        {isBuildingEdit && (
          <div className="flex items-center gap-2 my-2 px-3 py-2 bg-warning/10 border border-warning/25 rounded-lg text-[12px] text-warning/90">
            <span className="w-2 h-2 rounded-full bg-warning animate-pulse flex-shrink-0" />
            Preparing code change...
          </div>
        )}

        {content ? (
          <div className="prose-ai">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
              {stripInternalTags(content, true)}
            </ReactMarkdown>
          </div>
        ) : null}

        {/* Blinking cursor */}
        <span className="inline-block w-2 h-[1.1em] bg-accent/80 rounded-sm align-text-bottom ml-0.5 animate-pulse" />
      </div>
    </div>
  )
}

// ── File type icon helper ─────────────────────────────────
function getFileIcon(file: SessionFile) {
  const ext = file.filename.split('.').pop()?.toLowerCase() || ''
  const fileType = (file as any).file_type || ''
  if (fileType === 'image' || ['png','jpg','jpeg','webp','gif','bmp'].includes(ext)) {
    return '🖼️'
  }
  if (fileType === 'pdf' || ext === 'pdf') return '📄'
  if (fileType === 'csv' || ext === 'csv') return '📊'
  if (fileType === 'excel' || ['xlsx','xls'].includes(ext)) return '📊'
  return null // use FileCode icon (existing behavior)
}

// ── File chip ─────────────────────────────────────────────
function FileChip({ file, onRemove }: { file: SessionFile; onRemove: () => void }) {
  const langColors: Record<string, string> = {
    python: 'text-accent', typescript: 'text-accent', javascript: 'text-warning',
    go: 'text-accent', rust: 'text-orange', java: 'text-danger',
  }
  const color = langColors[file.language] || 'text-muted'
  const emojiIcon = getFileIcon(file)

  return (
    <div className="flex items-center gap-1.5 px-2.5 py-1 bg-surface border border-border rounded-lg group">
      {emojiIcon ? (
        <span className="text-[11px] leading-none">{emojiIcon}</span>
      ) : (
        <Description sx={{ fontSize: 11 }} className={color} />
      )}
      <span className="text-[12px] font-medium text-ink">{file.filename}</span>
      <span className="text-[10px] text-muted/70">{file.lines}L</span>
      {file.symbol_count > 0 && (
        <span className="text-[10px] text-faint">{file.symbol_count}⚡</span>
      )}
      <button
        onClick={onRemove}
        className="ml-0.5 opacity-0 group-hover:opacity-100 transition-opacity text-muted/70 hover:text-danger"
      >
        <Close sx={{ fontSize: 10 }} />
      </button>
    </div>
  )
}

// ── Empty state ───────────────────────────────────────────
function EmptyState({ onUpload }: { onUpload: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full px-8 py-12 text-center">
      <div className="w-16 h-16 rounded-2xl bg-accent/10 border border-accent/20 flex items-center justify-center mb-5">
        <Bolt sx={{ fontSize: 28 }} className="text-accent" />
      </div>
      <h2 className="text-base font-bold text-ink mb-2">SurgicalAI</h2>
      <p className="text-sm text-muted leading-relaxed mb-6 max-w-xs">
        Upload your code files, then describe what you want to change. The AI reads all your files and figures out exactly what to edit.
      </p>

      <button
        onClick={onUpload}
        className="flex items-center gap-2 px-4 py-2.5 bg-accent/20 text-accent border border-accent/30 rounded-xl text-sm font-semibold hover:bg-accent/30 transition-colors mb-6"
      >
        <AttachFile sx={{ fontSize: 15 }} /> Upload files to get started
      </button>

      <div className="w-full space-y-2 text-left">
        {[
          { ex: 'Upload settings.py', desc: 'then ask: "Add GPT-5 as a model option"' },
          { ex: 'Upload auth.ts, api.ts', desc: 'then ask: "Add rate limiting to all endpoints"' },
          { ex: 'Upload any code file', desc: 'then ask anything — edit, explain, review' },
        ].map(({ ex, desc }) => (
          <div key={ex} className="flex items-start gap-2.5 px-3 py-2 bg-surface/50 rounded-lg border border-border/50">
            <Description sx={{ fontSize: 12 }} className="text-accent mt-0.5 flex-shrink-0" />
            <div>
              <div className="text-[12px] font-semibold text-ink/80">{ex}</div>
              <div className="text-[11px] text-muted/70">{desc}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-6 w-full space-y-1.5 text-left">
        <div className="text-[10px] text-faint uppercase tracking-wider font-semibold mb-1">Keyboard shortcuts</div>
        {[
          ['⌘↵', 'Send'], ['⌘K', 'Focus input'], ['⌘N', 'New chat'], ['⌘P', 'Upload files'], ['Esc', 'Stop'],
        ].map(([key, desc]) => (
          <div key={key} className="flex items-center gap-2">
            <kbd className="px-1.5 py-0.5 rounded bg-surface border border-border text-[10px] font-mono text-muted">{key}</kbd>
            <span className="text-[11px] text-muted/70">{desc}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** Fold picked-element chips into the outgoing message text at send time
 *  only — the chips themselves never touch the textarea's visible value.
 *
 *  The appended context is still full text sent to the model (unchanged
 *  behavior), but it's wrapped in an unambiguous marker pair so the chat
 *  transcript can find it again — including after a page reload, since
 *  parsing reads straight from the persisted `msg.content` string rather
 *  than any client-only state. That lets the bubble show the same compact
 *  pill the user saw in the composer instead of a wall of raw HTML,
 *  with an explicit expand toggle to see the underlying snippet(s). */
const PICKED_CTX_START = '\n\n<!--PICKED_ELEMENTS_CONTEXT-->\n'
const PICKED_CTX_END = '\n<!--/PICKED_ELEMENTS_CONTEXT-->'

function appendPickedElementsContext(text: string, elements: PickedElementRef[]): string {
  if (elements.length === 0) return text
  const blocks = elements.map((el, i) => {
    const snippet = el.outerHTML.length > 800 ? el.outerHTML.slice(0, 800) + '…' : el.outerHTML
    const label = el.elId ? ` id="${el.elId}"` : ''
    return [
      `Element ${i + 1}${el.pageUrl ? ` (from ${el.pageUrl})` : ''}: <${el.tag}${label}>`,
      '```html',
      snippet,
      '```',
      el.text ? `Visible text: "${el.text.slice(0, 200)}"` : null,
    ].filter(Boolean).join('\n')
  })
  return `${text}${PICKED_CTX_START}${blocks.join('\n\n')}${PICKED_CTX_END}`
}

interface ParsedPickedElementCtx { tag: string; idLabel: string; text?: string; snippet: string }

/** Splits a message's stored content back into the part the user actually
 *  typed and the picked-element context blocks appended at send time.
 *  Returns elements: [] (and the content untouched) for any message that
 *  never had picked elements — including all pre-existing chat history. */
function parsePickedElementsFromContent(content: string): { visibleText: string; elements: ParsedPickedElementCtx[] } {
  const startIdx = content.indexOf(PICKED_CTX_START)
  if (startIdx === -1) return { visibleText: content, elements: [] }
  const endIdx = content.indexOf(PICKED_CTX_END, startIdx)
  if (endIdx === -1) return { visibleText: content, elements: [] }
  const visibleText = content.slice(0, startIdx)
  const raw = content.slice(startIdx + PICKED_CTX_START.length, endIdx)
  const elements = raw.split('\n\n').map((block) => {
    const tagMatch = block.match(/^Element \d+(?: \(from [^)]*\))?: <([a-zA-Z0-9-]+)/)
    const idMatch = block.match(/ id="([^"]*)"/)
    const textMatch = block.match(/Visible text: "([^"]*)"/)
    return {
      tag: tagMatch?.[1] || 'div',
      idLabel: idMatch ? `#${idMatch[1]}` : '',
      text: textMatch?.[1],
      snippet: block,
    }
  })
  return { visibleText, elements }
}

/** Read-only version of the composer's picked-element pill, shown inside a
 *  sent message. Stays exactly as compact as the composer chip by default;
 *  clicking any pill expands a details panel with the full HTML snippet(s)
 *  underneath, mirroring the existing compact-marker expand pattern. */
function PickedElementsInMessage({ elements }: { elements: ParsedPickedElementCtx[] }) {
  const [open, setOpen] = useState(false)
  if (elements.length === 0) return null
  return (
    <div className="mt-1.5">
      <div className="flex flex-wrap items-center gap-1.5 justify-end">
        {elements.map((el, i) => (
          <button
            key={i}
            onClick={() => setOpen((o) => !o)}
            title={el.text ? `"${el.text.slice(0, 120)}"` : 'Click to view HTML'}
            className="flex items-center gap-1.5 pl-2 pr-2 py-1 rounded-lg bg-accent/10 border border-accent/25 text-[11px] text-accent font-medium hover:bg-accent/15 transition-colors"
          >
            <AdsClick sx={{ fontSize: 13 }} className="shrink-0" />
            <span className="font-mono">&lt;{el.tag}&gt;{el.idLabel}</span>
          </button>
        ))}
        <span className="text-[10px] text-muted/50 cursor-pointer select-none" onClick={() => setOpen((o) => !o)}>
          {open ? '▲ hide' : '▼ expand'}
        </span>
      </div>
      {open && (
        <div className="mt-2 max-w-full text-[11px] leading-relaxed bg-surface/40 border border-border/40 rounded-lg p-3 whitespace-pre-wrap text-fg/80 font-mono text-left">
          {elements.map((e) => e.snippet).join('\n\n')}
        </div>
      )}
    </div>
  )
}

// ── Main Chat Panel ───────────────────────────────────────
export function ChatPanel() {
  const {
    activeSessions, setActiveSession, messages, addMessage, setMessages,
    isStreaming, setIsStreaming, streamingMessage, setStreamingMessage,
    streamProgress, setStreamProgress, sessions, setSessions, settings, setSettings,
    streamingSessions, setSessionStreaming, setSessionStreamingMessage, setSessionStreamProgress, clearSessionStream,
    sessionFiles, setSessionFiles, addSessionFile, removeSessionFile,
    agentTasks, setAgentTasks, updateAgentTask, clearAgentTasks, setTaskRunId, setTaskPreamble, setAgentPhase,
    pendingChatInput, setPendingChatInput,
    pickedElements, clearPickedElements,
  } = useAppStore()

  // Keep the agentic task list in sync with Claude's DB-backed progress while a
  // run is active (resilient fallback if the live stream drops mid-run).
  useTaskPolling(activeSessions)

  const [input, setInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [uploadingFiles, setUploadingFiles] = useState(false)
  const [isBuildingEdit, setIsBuildingEdit] = useState(false)
  const [isComposerExpanded, setIsComposerExpanded] = useState(false)

  // Consume pending input injected from sidebar components (e.g. deploy watcher "Ask Claude to fix")
  useEffect(() => {
    if (pendingChatInput) {
      setInput(pendingChatInput)
      setPendingChatInput(null)
      setTimeout(() => textareaRef.current?.focus(), 50)
    }
  }, [pendingChatInput])

  // Resize the composer immediately when expand/collapse is toggled, so any
  // existing text reflows to fit the new max-height right away.
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    const maxHeight = isComposerExpanded ? 480 : 200
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, maxHeight) + 'px'
  }, [isComposerExpanded])

  const [progressHistory, setProgressHistory] = useState<string[]>([])
  const [thinkingText, setThinkingText] = useState('')
  const [isThinking, setIsThinking] = useState(false)
  const [isCompacting, setIsCompacting] = useState(false)
  const [availableModels, setAvailableModels] = useState<{id: string; name: string; role: string; description?: string; cost?: number; provider?: string}[]>([])
  const [modelPickerOpen, setModelPickerOpen] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const userScrolledUpRef = useRef(false)
  // ── Agent Mode toggle (multi-agent task breakdown) ───────────────────────
  // Mode selector: Edit (default) | Ask | Plan | Agent.
  // Read localStorage at send time (not from a closure) so doStream never sees
  // a stale value. State exists only to drive the selector UI.
  const readChatMode = (): ChatMode => {
    try {
      const v = localStorage.getItem('sai_chat_mode')
      if (v === 'edit' || v === 'ask' || v === 'plan' || v === 'agent') return v
      // One-time migration from the legacy Agent Mode toggle.
      if (localStorage.getItem('sai_agent_mode') === '1') return 'agent'
    } catch { /* storage blocked — default below */ }
    return 'edit'
  }
  const [chatMode, setChatMode] = useState<ChatMode>(readChatMode)
  const [modeMenuOpen, setModeMenuOpen] = useState(false)
  // Dismissible Agent+non-Claude warning banner — resets whenever the mode or
  // architect model changes, so it can't stay hidden after the condition that
  // triggered it changes again.
  const [agentClaudeNoticeDismissed, setAgentClaudeNoticeDismissed] = useState(false)
  const selectChatMode = (m: ChatMode) => {
    setChatMode(m)
    try { localStorage.setItem('sai_chat_mode', m) } catch { /* storage blocked — session-only */ }
  }

  // ── Offline (Ollama/Qwen) mode gating ──────────────────────────────────
  // Mirror of backend _should_use_ollama: a local "ollama:" model, OR
  // ollama_enabled with no cloud OpenAI key. Offline runs on a 7B local
  // model that can only do plain chat + whole-file rewrite, so we expose
  // just Ask + Edit and hide Plan + Agent. The backend enforces the same
  // guard structurally — this is the honest UI mirror. We never mutate the
  // user's stored preference, so their cloud choice returns when a key is added.
  const isOffline = !!(
    settings?.architect_model?.startsWith('ollama:') ||
    (settings?.ollama_enabled && !settings?.openai_api_key_set)
  )
  const availableModes: ChatMode[] = isOffline ? ['edit', 'ask'] : CHAT_MODES
  const effectiveMode: ChatMode = isOffline
    ? (chatMode === 'agent' ? 'edit' : chatMode === 'plan' ? 'ask' : chatMode)
    : chatMode
  // Ask/Plan file-search & lookup tools are Claude-only today (backend gate:
  // `_ask_plan_tools_enabled = bool(session_files) and _is_claude_model(...)`
  // in pipeline.py). If the current architect model is GPT while the user is
  // in Ask or Plan mode, flag it — the run will still work but silently
  // degrades to a 300-line file preview instead of real search/lookup.
  const currentModelProvider = availableModels.find(m => m.id === settings?.architect_model)?.provider
  const searchToolsUnavailableForCurrentModel =
    (effectiveMode === 'ask' || effectiveMode === 'plan') && currentModelProvider === 'openai'
  // Agent mode (multi-agent task pipeline) is Claude-only today (backend gate:
  // `_is_claude = _arch_model.startswith("claude-")` in chat.py). Unlike
  // Ask/Plan, GPT doesn't just lose a feature here — the whole run silently
  // downgrades to a normal single-pass edit instead of multi-agent tasks.
  const agentRequiresClaudeForCurrentModel =
    effectiveMode === 'agent' && currentModelProvider === 'openai'
  // Re-arm the dismissible banner whenever the condition it warns about
  // changes — a dismissal only applies to the specific mode/model pairing
  // the user saw it for, never silently suppressed going forward.
  useEffect(() => {
    setAgentClaudeNoticeDismissed(false)
  }, [effectiveMode, settings?.architect_model])
  // Ref so doStream (deps: [sessionFiles]) reads the live offline flag with no
  // stale closure and without widening its dependency array.
  const isOfflineRef = useRef(isOffline)
  isOfflineRef.current = isOffline

  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const abortMapRef = useRef<Map<string, AbortController>>(new Map())
  const sentMessageMapRef = useRef<Map<string, string>>(new Map())
  const thinkingTextRef = useRef('')
  const progressHistoryRef = useRef<string[]>([])

  // Manual abort (Escape / Stop button): preserve whatever was streamed so far
  // — including any thinking text — as a real message instead of discarding it.
  // Long-term fix: nothing streamed is ever silently dropped on user-initiated stop.
  const saveAbortedMessage = (sid: string) => {
    const state = useAppStore.getState()
    const entry = state.streamingSessions[sid]
    const accumulated = (entry?.streamingMessage ?? (state.activeSessions === sid ? state.streamingMessage : '')) || ''
    const thinking = thinkingTextRef.current
    const steps = [...progressHistoryRef.current]
    if (accumulated.trim() || thinking.trim()) {
      addMessage({
        id: Date.now().toString() + '_ai_aborted',
        session_id: sid,
        role: 'assistant',
        content: accumulated.trim(),
        created_at: new Date().toISOString(),
        _thinking: thinking || undefined,
        _steps: steps,
        _aborted: true,
      })
    }
    clearSessionStream(sid)
  }

  // Mid-thought injection state
  const [injectionInput, setInjectionInput] = useState('')
  const [injectionQueued, setInjectionQueued] = useState(false)
  const pendingInjectionRef = useRef<string>('')
  const sentMessageRef = useRef<string>('')
  // v1.4: holds the planned run while the planning stream closes, so the
  // per-task execution queue can start once /smart-stream returns.
  const pendingRunRef = useRef<{ runId: string; tasks: any[]; serverRun?: boolean } | null>(null)
  const [restartSignal, setRestartSignal] = useState<{ msg: string; sid: string } | null>(null)

  // ── Human-in-the-loop file request ──────────────────────────────────────
  // The agent paused mid-run because it needs a file that isn't in the
  // session and couldn't be auto-fetched. We surface an inline prompt so the
  // user can upload it (run resumes) or skip (run continues without it).
  const [fileRequest, setFileRequest] = useState<{
    sessionId: string
    filename: string
    message: string
    retry?: boolean
    respond: (resp: { filename?: string; content?: string; action?: 'skip' }) => void
  } | null>(null)
  const fileRequestInputRef = useRef<HTMLInputElement>(null)
  const [fileRequestBusy, setFileRequestBusy] = useState(false)

  // Smart auto-scroll: only scroll to bottom when user is already at the bottom.
  // If user scrolls up to read, don't yank them back down.
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const handleScroll = () => {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      userScrolledUpRef.current = distanceFromBottom > 80
    }
    el.addEventListener('scroll', handleScroll, { passive: true })
    return () => el.removeEventListener('scroll', handleScroll)
  }, [])

  useEffect(() => {
    if (!userScrolledUpRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, streamingMessage])

  // Load session files when session changes
  useEffect(() => {
    // Concurrent sessions: no abort on switch — streams continue in background
    setResumableRun(null)  // stale banner never survives a session switch
    // Reset local display state so stale flags from the previous session don't
    // bleed into the new one (e.g. isCompacting disabling input permanently)
    setIsThinking(false)
    setIsCompacting(false)
    setThinkingText('')
    thinkingTextRef.current = ''
    setIsBuildingEdit(false)
    setProgressHistory([])
    progressHistoryRef.current = []
    if (activeSessions) {
      api.sessionFiles.list(activeSessions)
        .then(files => setSessionFiles(files))
        .catch(() => {})
      // Reconcile the agentic task list: only show tasks for ACTIVE runs.
      // Completed runs are historical — their results are already in the chat
      // as message cards. Repopulating them would keep the Executor panel
      // permanently visible, blocking natural chat flow.
      clearAgentTasks()
      api.tasks.list(activeSessions)
        .then((rows: any[]) => {
          if (!Array.isArray(rows) || rows.length === 0) return
          const latestRun = rows[0]?.run_id
          const forRun = rows.filter(r => r.run_id === latestRun)
          const TERMINAL = ['done', 'blocked', 'cancelled', 'error']
          const allTerminal = forRun.every(r => TERMINAL.includes(r.status))
          // Skip — completed runs don't need the mission control panel.
          if (allTerminal) return
          setTaskRunId(latestRun || null)
          setAgentTasks(forRun
            .slice()
            .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
            .map(r => ({
              id: r.id, seq: r.seq, title: r.title, detail: r.detail,
              kind: r.kind || 'code',
              status: r.status, qa_score: r.qa_score ?? null, verdict: r.verdict ?? null,
              run_id: r.run_id, progress: r.result_summary ?? undefined,
            })))
          // Resume detection: pending tasks with nothing running means the
          // run was interrupted (tab closed / refreshed mid-run). Offer a
          // one-click Resume instead of leaving the tasks stranded forever.
          const pendingTasks = forRun
            .filter(r => r.status === 'pending')
            .slice()
            .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
            .map(r => ({ id: r.id, seq: r.seq, title: r.title }))
          const anyRunning = forRun.some(r => r.status === 'running')
          if (pendingTasks.length > 0 && !anyRunning && latestRun) {
            setResumableRun({ sid: activeSessions, runId: latestRun, tasks: pendingTasks })
          }
        })
        .catch(() => {})
    } else {
      setSessionFiles([])
      clearAgentTasks()
    }
  }, [activeSessions])

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); textareaRef.current?.focus() }
      if ((e.ctrlKey || e.metaKey) && e.key === 'n') { e.preventDefault(); newChat() }
      if ((e.ctrlKey || e.metaKey) && e.key === 'p') { e.preventDefault(); fileInputRef.current?.click() }
      if (e.key === 'Escape' && isStreaming) {
        const sid = useAppStore.getState().activeSessions
        if (sid) { abortMapRef.current.get(sid)?.abort(); abortMapRef.current.delete(sid); saveAbortedMessage(sid) }
        else { setIsStreaming(false); setStreamingMessage(''); setStreamProgress('') }
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [isStreaming])

  // Load available models for inline model picker
  useEffect(() => {
    api.settings.getModels().then((d: any) => setAvailableModels(d.models || [])).catch(() => {})
  }, [])

  const stopStream = (sessionId?: string) => {
    const sid = sessionId || useAppStore.getState().activeSessions
    if (sid) clearSessionStream(sid)
    else { setIsStreaming(false); setStreamingMessage(''); setStreamProgress('') }
  }

  // ── v1.4 per-task execution queue ─────────────────────────────────────
  // Hoisted to component scope so BOTH a freshly planned run (doStream) and
  // a resumed interrupted run (resumeInterruptedRun) can drive it.
  // /smart-stream ends right after planning. We then run each task in its
  // own short-lived SSE stream, sequentially, so no single connection can
  // hit the proxy/process timeout that previously killed long runs.

  // Holds an interrupted run detected on session load (pending tasks, none
  // running) — rendered as a Resume banner above the Mission Control panel.
  const [resumableRun, setResumableRun] = useState<{ sid: string; runId: string; tasks: any[] } | null>(null)

  const addTaskResultCard = (sid: string, result: any) => {
    const naturalText = (result.natural_text || '')
      .replace(/<new_file>[\s\S]*?<\/new_file>/g, '')
      .replace(/<new_file>[\s\S]*$/, '')
      .trim()
    addMessage({
      id: Date.now().toString() + '_task_' + Math.random().toString(36).slice(2, 7),
      session_id: sid,
      role: 'assistant',
      message_type: naturalText ? 'natural_result' : 'surgical_result',
      surgical_data: JSON.stringify(result),
      content: naturalText,
      created_at: new Date().toISOString(),
      _model: result._model || 'N/A',
    })
    api.sessionFiles.list(sid).then(setSessionFiles).catch(() => {})
  }

  const handleTaskEvent = (event: any) => {
    switch (event.type) {
      case 'task_start':
        updateAgentTask(event.id, { status: 'running', progress: undefined }); break
      case 'task_progress':
        updateAgentTask(event.id, { progress: event.content }); break
      case 'task_thinking': {
        const st = useAppStore.getState()
        const existing = st.agentTasks.find(t => t.id === event.id)?.thinking || ''
        updateAgentTask(event.id, { thinking: existing + (event.content || '') }); break
      }
      case 'task_done':
        updateAgentTask(event.id, { status: 'done', qa_score: event.qa_score, verdict: event.verdict }); break
      case 'task_blocked':
        updateAgentTask(event.id, { status: 'blocked', qa_score: event.qa_score, verdict: event.verdict }); break
      case 'task_cancelled':
        updateAgentTask(event.id, { status: 'cancelled' }); break
      case 'tasks_complete':
        setAgentPhase('complete'); break
    }
  }

  const finishTaskRun = (sid: string, onFinish?: () => void) => {
    clearSessionStream(sid)
    if (useAppStore.getState().activeSessions === sid) {
      setIsBuildingEdit(false)
      setAgentPhase('complete')
    }
    if (onFinish) onFinish()
    else api.chat.getSessions().then(setSessions).catch(() => {})
    api.sessionFiles.list(sid).then(setSessionFiles).catch(() => {})
  }

  const runTaskQueue = (sid: string, runId: string, tasks: any[], onFinish?: () => void) => {
    let idx = 0
    const runNext = () => {
      if (idx >= tasks.length) {
        finishTaskRun(sid, onFinish); return
      }
      const t = tasks[idx++]
      // True once this task's stream delivered a terminal event
      // (task_done / task_blocked / task_cancelled). If the stream closes
      // without one, the connection dropped mid-task and we must reconcile
      // the real status from the DB instead of stalling the queue.
      let sawTerminal = false
      let streamErr = ''
      const reconcileOnClose = () => {
        if (sawTerminal) return  // normal close after task_done — already advanced
        api.tasks.list(sid, runId)
          .then((rows: any[]) => {
            const row: any = (rows || []).find((r: any) => r.id === t.id)
            const status = row?.status
            if (status === 'done') {
              // Backend finished the task but the task_done event was lost
              // in transit — record it and keep the queue moving.
              updateAgentTask(t.id, { status: 'done', qa_score: row.qa_score, verdict: row.verdict })
              runNext()
            } else if (status === 'blocked' || status === 'cancelled') {
              updateAgentTask(t.id, { status, qa_score: row?.qa_score, verdict: row?.verdict })
              finishTaskRun(sid, onFinish)
            } else if (status === 'pending') {
              // The backend disconnect guard reset the orphaned task after
              // the drop — pause the run and offer a one-click Resume.
              updateAgentTask(t.id, { status: 'pending', progress: undefined })
              setResumableRun({ sid, runId, tasks: tasks.slice(idx - 1) })
              setError(streamErr || 'Connection dropped mid-task. The run is paused — click Resume to continue.')
              finishTaskRun(sid, onFinish)
            } else {
              // running / unknown — we cannot safely continue.
              setError(streamErr || 'Connection dropped mid-task. Task status is unresolved — reopen the session to check progress.')
              finishTaskRun(sid, onFinish)
            }
          })
          .catch(() => {
            setError(streamErr || 'Connection dropped and task status could not be verified.')
            finishTaskRun(sid, onFinish)
          })
      }
      const ctrl = api.stream.executeTask(
        { session_id: sid, run_id: runId, task_id: t.id },
        (progress) => { setSessionStreamProgress(sid, progress) },
        (result) => addTaskResultCard(sid, result),
        reconcileOnClose,  // per-task stream closed
        (err) => { streamErr = err },  // defer: reconcileOnClose decides the outcome
        (event) => {
          if (event.type === 'task_done' || event.type === 'task_blocked' || event.type === 'task_cancelled') {
            sawTerminal = true
          }
          handleTaskEvent(event)
          if (event.type === 'task_done') runNext()
          else if (event.type === 'task_blocked' || event.type === 'task_cancelled') finishTaskRun(sid, onFinish)
        },
      )
      abortMapRef.current.set(sid, ctrl)
    }
    runNext()
  }

  // Restart an interrupted run: re-drives the queue over its remaining
  // pending tasks. The backend idempotency guard makes this safe — a task
  // that is not "pending" can never be re-executed.
  const resumeInterruptedRun = () => {
    if (!resumableRun || isStreaming) return
    if (useAppStore.getState().activeSessions !== resumableRun.sid) { setResumableRun(null); return }
    const { sid, runId, tasks } = resumableRun
    setResumableRun(null)
    setError(null)
    setSessionStreaming(sid, true)
    if (useAppStore.getState().activeSessions === sid) setAgentPhase('executing')
    // Server runner (when enabled) also handles resume — /runs/start simply
    // executes whatever is still pending. Non-ok → client queue, as always.
    startServerRun(sid, runId, tasks)
  }

  // ── v2.0 server-side task runner ──────────────────────────────────────
  // When the backend plans a run with server_run=true, execution is handed
  // to POST /runs/start and the backend supervisor drives every task (the
  // tab can even close — the run keeps going). This tab stays a pure
  // observer: useTaskPolling reconciles task state every 2.5s, and the
  // watcher below finishes the run + reloads persisted result cards once
  // every task reaches a terminal state. Any start failure falls back to
  // the browser-driven queue, so the feature can never strand a run.
  const [serverRun, setServerRun] = useState<{ sid: string; runId: string; onFinish?: () => void } | null>(null)

  const startServerRun = (sid: string, runId: string, tasks: any[], onFinish?: () => void) => {
    api.runs.start(sid, runId)
      .then((resp: any) => {
        if (resp?.ok) {
          setAgentPhase('executing')
          setServerRun({ sid, runId, onFinish })
        } else {
          // Disabled / already running / error — the client queue always works.
          runTaskQueue(sid, runId, tasks, onFinish)
        }
      })
      .catch(() => {
        runTaskQueue(sid, runId, tasks, onFinish)
      })
  }

  useEffect(() => {
    if (!serverRun) return
    if (agentTasks.length === 0) return
    const TERMINAL = ['done', 'blocked', 'cancelled', 'error']
    if (!agentTasks.every(t => TERMINAL.includes(t.status))) return
    // Every task is terminal — the server run is over. Reload the persisted
    // messages so the result cards + run summary note appear, then tear down.
    const { sid, onFinish } = serverRun
    setServerRun(null)
    api.chat.getMessages(sid)
      .then((saved: any[]) => {
        if (useAppStore.getState().activeSessions === sid && saved?.length) setMessages(saved)
      })
      .catch(() => {})
    finishTaskRun(sid, onFinish)
  }, [agentTasks, serverRun])

  // ── Core stream launcher — shared by handleSend and injection restart ─────
  const doStream = useCallback((
    sessionId: string,
    messageText: string,
    isFirst: boolean,
    autoRename: () => void,
  ) => {
    let accumulated = ''
    let gotResult = false
    let streamModel = ''

    // Resolve the outgoing mode fresh from storage, then degrade to the
    // offline set (Edit/Ask) when on a local model so Qwen is never asked to
    // Plan/Agent. Backend applies the same guard structurally as a backstop.
    const _rawMode = readChatMode()
    const _sendMode: ChatMode = isOfflineRef.current
      ? (_rawMode === 'agent' ? 'edit' : _rawMode === 'plan' ? 'ask' : _rawMode)
      : _rawMode

    const ctrl = api.stream.smart(
      { session_id: sessionId, message: messageText, file_ids: sessionFiles.map(f => f.id), mode: _sendMode, force_tasks: _sendMode === 'agent' },
      (progress) => {
        setSessionStreamProgress(sessionId, progress)
        if (useAppStore.getState().activeSessions !== sessionId) return
        setProgressHistory(prev => {
          if (prev[prev.length - 1] !== progress) {
            const next = [...prev, progress]
            progressHistoryRef.current = next
            return next
          }
          return prev
        })
      },
      (token) => { accumulated += token; setSessionStreamingMessage(sessionId, accumulated) },
      (result) => {
        gotResult = true
        if (result._model) streamModel = result._model
        const _thinking = thinkingTextRef.current
        const _steps = [...progressHistoryRef.current]
        const naturalText = (result.natural_text || accumulated)
          .replace(/<new_file>[\s\S]*?<\/new_file>/g, '')
          .replace(/<new_file>[\s\S]*$/, '')
          .trim()

        clearSessionStream(sessionId)
        if (useAppStore.getState().activeSessions === sessionId) setIsBuildingEdit(false)

        if (naturalText.trim()) {
          addMessage({
            id: Date.now().toString() + '_ai',
            session_id: sessionId,
            role: 'assistant',
            message_type: 'natural_result',
            surgical_data: JSON.stringify(result),
            content: naturalText.trim(),
            created_at: new Date().toISOString(),
            _thinking,
            _steps,
            _model: streamModel || 'N/A',
          })
        } else {
          addMessage({
            id: Date.now().toString() + '_ai',
            session_id: sessionId,
            role: 'assistant',
            message_type: 'surgical_result',
            surgical_data: JSON.stringify(result),
            content: '',
            created_at: new Date().toISOString(),
            _thinking,
            _steps,
            _model: streamModel || 'N/A',
          })
        }
        if (isFirst) autoRename()
        else api.chat.getSessions().then(setSessions).catch(() => {})
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
      },
      (fullText: string, model?: string) => {
        if (model) streamModel = model
        // Planning stream closed — if a task run was planned, start executing
        // tasks one at a time (each in its own SSE stream) instead of the
        // single-pass teardown below.
        if (pendingRunRef.current) {
          const run = pendingRunRef.current
          pendingRunRef.current = null
          const onRunFinish = () => {
            if (isFirst) autoRename()
            else api.chat.getSessions().then(setSessions).catch(() => {})
          }
          if (run.serverRun) startServerRun(sessionId, run.runId, run.tasks, onRunFinish)
          else runTaskQueue(sessionId, run.runId, run.tasks, onRunFinish)
          return
        }
        if (gotResult) return
        // Planning was started but fell back to single_pass (< 2 tasks) —
        // reset the mission-control panel so the Architect card disappears.
        if (useAppStore.getState().activeSessions === sessionId) clearAgentTasks()
        const _thinking = thinkingTextRef.current
        const _steps = [...progressHistoryRef.current]
        clearSessionStream(sessionId)
        if (useAppStore.getState().activeSessions === sessionId) setIsBuildingEdit(false)
        if (fullText.trim()) {
          addMessage({
            id: Date.now().toString() + '_ai',
            session_id: sessionId,
            role: 'assistant',
            content: fullText,
            created_at: new Date().toISOString(),
            _thinking,
            _steps,
            _model: streamModel || 'N/A',
          })
        }
        if (isFirst) autoRename()
        else api.chat.getSessions().then(setSessions).catch(() => {})
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
      },
      (err) => {
        if ((accumulated.trim() || thinkingTextRef.current.trim()) && !gotResult) {
          addMessage({
            id: Date.now().toString() + '_ai_err',
            session_id: sessionId,
            role: 'assistant',
            content: accumulated.trim(),
            created_at: new Date().toISOString(),
            _thinking: thinkingTextRef.current || undefined,
            _steps: [...progressHistoryRef.current],
            _model: streamModel || 'N/A',
          })
          gotResult = true
        }
        if (useAppStore.getState().activeSessions === sessionId) setError(err)
        clearSessionStream(sessionId)
        if (useAppStore.getState().activeSessions === sessionId) setIsBuildingEdit(false)
        setTimeout(async () => {
          try {
            if (useAppStore.getState().activeSessions !== sessionId) return
            const saved = await api.chat.getMessages(sessionId)
            if (saved?.length) setMessages(saved)
          } catch {}
        }, 3000)
      },
      // onThinking — injection point: when thinking ends and injection is queued, restart
      //
      // NOTE: the backend fires thinking_start/thinking_end multiple times within a
      // single operation (one pair per correction round / retry / phase). We must NOT
      // wipe accumulated thinking on every 'start' — only a brand-new user message
      // resets thinkingTextRef (see the three explicit resets at message-send time).
      // Here we just append a separator so every round's thinking survives to the
      // final saved message instead of only the last round's.
      (thinkToken, phase) => {
        if (useAppStore.getState().activeSessions !== sessionId) return
        if (phase === 'start') {
          setIsThinking(true)
          if (thinkingTextRef.current) {
            const next = thinkingTextRef.current + '\n\n---\n\n'
            setThinkingText(next); thinkingTextRef.current = next
          }
        } else if (phase === 'delta') {
          setThinkingText(prev => { const next = prev + thinkToken; thinkingTextRef.current = next; return next })
        } else if (phase === 'end') {
          setIsThinking(false)
        }
      },
      // onCompacting — the persisted __COMPACTION_EVENT__ row (see backend
      // _compact_session) is what will actually reload from the server on
      // next getMessages() call; this optimistic insert just renders the
      // same shape immediately so the user doesn't have to refresh to see it.
      (phase, info) => {
        if (useAppStore.getState().activeSessions !== sessionId) return
        if (phase === 'start') {
          setIsCompacting(true)
        } else {
          setIsCompacting(false)
          addMessage({
            id: Date.now().toString() + '_compact',
            session_id: sessionId,
            role: 'system' as any,
            message_type: 'compact_marker',
            content: '',
            compact_summary: info?.summary || '',
            compact_count: info?.compacted_count || 0,
            created_at: new Date().toISOString(),
          } as any)
        }
      },
      // onEditStart
      () => { setIsBuildingEdit(true) },
      // onEditEnd
      () => { setIsBuildingEdit(false) },
      // onTask
      (event) => {
        switch (event.type) {
          case 'planning_started':
            setAgentPhase('planning')
            break
          case 'task_plan':
            setAgentPhase('executing')
            setTaskRunId(event.run_id)
            setTaskPreamble(event.preamble || '')
            setAgentTasks((event.tasks || []).map((t: any) => ({
              id: t.id, seq: t.seq, title: t.title, detail: t.detail,
              kind: t.kind || 'code',
              status: t.status || 'pending', qa_score: null, verdict: null,
              run_id: event.run_id,
            })))
            // Stash the plan; the queue starts once the planning stream closes.
            pendingRunRef.current = { runId: event.run_id, tasks: event.tasks || [], serverRun: !!event.server_run }
            break
          case 'task_start':
            updateAgentTask(event.id, { status: 'running', progress: undefined })
            break
          case 'task_progress':
            updateAgentTask(event.id, { progress: event.content })
            break
          case 'task_done':
            updateAgentTask(event.id, { status: 'done', qa_score: event.qa_score, verdict: event.verdict })
            break
          case 'task_blocked':
            updateAgentTask(event.id, { status: 'blocked', qa_score: event.qa_score, verdict: event.verdict })
            break
          case 'task_cancelled':
            updateAgentTask(event.id, { status: 'cancelled' })
            break
          case 'tasks_complete':
            setAgentPhase('complete')
            break
        }
      },
      // onFileNeeded — agent paused; it needs a file not in the session.
      (info, respond) => {
        // If the user isn't looking at this session, auto-skip so the run
        // never hangs on a prompt nobody can see.
        if (useAppStore.getState().activeSessions !== sessionId) {
          respond({ action: 'skip' })
          return
        }
        setFileRequestBusy(false)
        setFileRequest({ sessionId, filename: info.filename, message: info.message, retry: info.retry, respond })
      },
      // onFileCleared — prompt resolved (provided / skipped / timed out).
      (_filename) => {
        setFileRequest(null)
        setFileRequestBusy(false)
      },
    )
    abortMapRef.current.set(sessionId, ctrl)
  }, [sessionFiles]) // all setters are stable; only sessionFiles can change

  // Restart stream when an injection was queued — fires once isStreaming settles to false
  useEffect(() => {
    if (!restartSignal || isStreaming) return
    const { msg, sid } = restartSignal
    setRestartSignal(null)
    setSessionStreaming(sid, true)
    setSessionStreamProgress(sid, 'Applying your context...')
    setSessionStreamingMessage(sid, '')
    setProgressHistory(['Applying your context...'])
    setThinkingText('')
    setIsThinking(false)
    thinkingTextRef.current = ''
    progressHistoryRef.current = ['Applying your context...']
    doStream(sid, msg, false, () => {
      api.chat.getSessions().then(setSessions).catch(() => {})
    })
  }, [restartSignal, isStreaming, doStream])

  // ── Human-in-the-loop file request handlers ────────────────────────────
  const handleFileRequestUpload = () => fileRequestInputRef.current?.click()

  const handleFileRequestFileChosen = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''            // allow re-selecting the same filename later
    const req = fileRequest
    if (!file || !req) return
    const sizeErr = validateFileSize(file.name, file.size)
    if (sizeErr) { setError(sizeErr); return }
    setFileRequestBusy(true)
    try {
      const content = await file.text()
      req.respond({ filename: file.name, content })
    } catch {
      setFileRequestBusy(false)
      setError('Could not read that file — please try again or skip.')
    }
  }

  const handleFileRequestSkip = () => {
    if (!fileRequest) return
    setFileRequestBusy(true)
    fileRequest.respond({ action: 'skip' })
  }

  const handleModelChange = async (modelId: string) => {
    if (!settings) return
    setModelPickerOpen(false)
    try {
      await api.settings.update({ ...settings, architect_model: modelId })
      const updated = await api.settings.get()
      setSettings(updated)
    } catch { toast.error('Failed to update model') }
  }

  const newChat = async () => {
    // Concurrent sessions: don't abort background streams
    try {
      const s = await api.chat.createSession({ title: 'New Chat' })
      const updated = await api.chat.getSessions()
      setSessions(updated)
      setActiveSession(s.id)
      setMessages([])
      setSessionFiles([])
    } catch {
      toast.error('Failed to create chat')
    }
  }

  const ensureSession = async () => {
    if (activeSessions) return activeSessions
    const s = await api.chat.createSession({ title: input.slice(0, 40) || 'New Chat' })
    const updated = await api.chat.getSessions()
    setSessions(updated)
    setActiveSession(s.id)
    return s.id
  }

  // ── File upload ───────────────────────────────────────
  const uploadFiles = useCallback(async (fileList: FileList | File[]) => {
    // Convert FileList → Array SYNCHRONOUSLY before any awaits.
    // FileList objects tied to a cleared <input> can lose their entries
    // once e.target.value = '' runs while an async ensureSession() is in-flight.
    let files = Array.from(fileList)
    if (!files.length) return

    // ── Zip extraction ───────────────────────────────────
    const zipFiles = files.filter(f => f.name.toLowerCase().endsWith('.zip'))
    const nonZipFiles = files.filter(f => !f.name.toLowerCase().endsWith('.zip'))

    if (zipFiles.length > 0) {
      const JSZip = (await import('jszip')).default
      const SKIP_DIRS = ['node_modules', '.git', '__pycache__', '.next', 'dist', 'build', '.venv', 'venv']
      const SKIP_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg', 'ico', 'woff', 'woff2', 'ttf', 'eot', 'otf', 'mp4', 'mp3', 'zip', 'tar', 'gz', 'exe', 'dll', 'so', 'dylib', 'lock']
      const SKIP_FILES = ['.ds_store', 'thumbs.db', '.gitignore', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml']

      const extracted: File[] = []
      for (const zipFile of zipFiles) {
        try {
          const zip = await JSZip.loadAsync(zipFile)
          const entries = Object.entries(zip.files)
          let skipped = 0
          for (const [path, entry] of entries) {
            if (entry.dir) continue
            const parts = path.split('/')
            const filename = parts[parts.length - 1]
            const ext = filename.split('.').pop()?.toLowerCase() || ''
            // Skip junk dirs, binary exts, hidden files, lock files
            if (parts.some(p => SKIP_DIRS.includes(p))) { skipped++; continue }
            if (SKIP_EXTS.includes(ext)) { skipped++; continue }
            if (SKIP_FILES.includes(filename.toLowerCase())) { skipped++; continue }
            if (filename.startsWith('.')) { skipped++; continue }
            const content = await entry.async('string')
            // Use full path as filename to preserve context (e.g. src/components/App.tsx)
            extracted.push(new File([content], path, { type: 'text/plain' }))
          }
          const msg = skipped > 0
            ? `Extracted ${extracted.length} files from ${zipFile.name} (${skipped} skipped)`
            : `Extracted ${extracted.length} files from ${zipFile.name}`
          toast.success(msg)
        } catch (e: any) {
          toast.error(`Failed to extract ${zipFile.name}: ${e.message}`)
        }
      }
      files = [...nonZipFiles, ...extracted]
      if (!files.length) return
    }

    const sessionId = await ensureSession()
    setUploadingFiles(true)
    const promises = files.map(async (file) => {
      // ── File size validation ─────────────────────────────────────────
      const sizeErr = validateFileSize(file.name, file.size)
      if (sizeErr) { toast.error(sizeErr); return null }

      const language = getLanguage(file.name)
      const ext = file.name.split('.').pop()?.toLowerCase() || ''
      const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'svg', 'heic', 'heif']
      const BINARY_EXTS = ['pdf', 'xlsx', 'xls']
      let isImage = !!(file.type && file.type.startsWith('image/')) || IMAGE_EXTS.includes(ext)
      const isBinary = BINARY_EXTS.includes(ext)
      let detectedMime = file.type || ''

      // ── DIAGNOSTIC TOAST — visible on device screen ─────────────────────────
      // Shows raw file properties so we can see exactly what iOS Chrome sends.
      // Remove after the iOS upload bug is fixed.
      toast.info(`📁 ${file.name}`, `type="${file.type}" ext="${ext}" img=${isImage}`)

      // ── Magic byte fallback ────────────────────────────────────────────────
      if (!isImage && !isBinary) {
        try {
          const hdr = new Uint8Array(await file.slice(0, 12).arrayBuffer())
          if (hdr[0] === 0xFF && hdr[1] === 0xD8 && hdr[2] === 0xFF) {
            isImage = true; detectedMime = 'image/jpeg'
          } else if (hdr[0] === 0x89 && hdr[1] === 0x50 && hdr[2] === 0x4E && hdr[3] === 0x47) {
            isImage = true; detectedMime = 'image/png'
          } else if (hdr[0] === 0x47 && hdr[1] === 0x49 && hdr[2] === 0x46 && hdr[3] === 0x38) {
            isImage = true; detectedMime = 'image/gif'
          } else if (hdr[0] === 0x52 && hdr[1] === 0x49 && hdr[2] === 0x46 && hdr[3] === 0x46 &&
                     hdr[8] === 0x57 && hdr[9] === 0x45 && hdr[10] === 0x42 && hdr[11] === 0x50) {
            isImage = true; detectedMime = 'image/webp'
          } else if (hdr[4] === 0x66 && hdr[5] === 0x74 && hdr[6] === 0x79 && hdr[7] === 0x70) {
            isImage = true; detectedMime = 'image/heic'
          }
          if (isImage) {
            toast.info(`🔬 Magic bytes → ${detectedMime}`)
            console.log(`[IMG-UPLOAD] Magic bytes → ${detectedMime} for "${file.name}"`)
          }
        } catch (e: any) {
          console.warn(`[IMG-UPLOAD] Magic byte check failed: ${e.message}`)
        }
      }

      let uploadBody: { filename: string; content: string; language?: string; base64_data?: string; file_type?: string }

      if (isImage) {
        // ── Multipart upload ──────────────────────────────────────────────────
        toast.info(`🚀 → MULTIPART path`)
        console.log(`[IMG-UPLOAD] Multipart: ${file.name} ${(file.size / 1024 / 1024).toFixed(1)}MB type=${file.type}`)
        const formData = new FormData()
        formData.append('file', file)
        formData.append('filename', file.name)
        try {
          const result = await api.sessionFiles.uploadMultipart(sessionId, formData)
          addSessionFile(result)
          toast.success(`${file.name} uploaded OK`)
          return result
        } catch (e: any) {
          const detail = (e as any)?.response?.status ? `HTTP ${(e as any).response.status}` : (e as Error).message
          toast.error(`MULTIPART FAILED: ${detail}`)
          console.error('[IMG-UPLOAD] multipart error:', e)
          return null
        }
      } else if (isBinary) {
        // PDF or Excel — read as base64, let backend extract text
        const base64Data = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader()
          reader.onload = () => resolve(reader.result as string)
          reader.onerror = reject
          reader.readAsDataURL(file)
        })
        const fileType = ext === 'pdf' ? 'pdf' : 'excel'
        uploadBody = { filename: file.name, content: '', base64_data: base64Data, language, file_type: fileType }
      } else {
        // ── DIAGNOSTIC: should never reach here for images ────────────────────
        toast.error(`⚠️ TEXT path for "${file.name}" (ext="${ext}" type="${file.type}")`)
        // Text / code — existing behavior
        const content = await file.text()
        uploadBody = { filename: file.name, content, language }
      }

      try {
        const payloadSize = JSON.stringify(uploadBody).length
        console.log(`[IMG-UPLOAD] Sending ${file.name} (${(payloadSize / 1024).toFixed(0)}KB)`)
        const result = await api.sessionFiles.upload(sessionId, uploadBody)
        addSessionFile(result)
        toast.success(`${file.name} uploaded OK`)
        return result
      } catch (e: any) {
        const detail = e?.response?.status ? `HTTP ${e.response.status}` : e.message
        toast.error(`UPLOAD FAILED ${file.name}: ${detail}`)
        console.error('[IMG-UPLOAD] fetch error:', e)
        return null
      }
    })

    await Promise.all(promises)
    setUploadingFiles(false)
    toast.success(`${files.length} file${files.length > 1 ? 's' : ''} ready`)
    textareaRef.current?.focus()
  }, [activeSessions])

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) {
      uploadFiles(e.target.files)
      e.target.value = ''
    }
  }

  // Drag and drop
  const handleDragOver = (e: React.DragEvent) => { e.preventDefault(); setIsDragging(true) }
  const handleDragLeave = (e: React.DragEvent) => { e.preventDefault(); setIsDragging(false) }
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
    const files = Array.from(e.dataTransfer.files).filter(f => {
      const ext = f.name.split('.').pop()?.toLowerCase() || ''
      return ['py','js','ts','tsx','jsx','go','rs','java','cs','rb','php','swift','kt','html','css','json','yaml','yml','toml','md','sh','sql','cpp','c','h','png','jpg','jpeg','webp','gif','bmp','pdf','csv','xlsx','xls','txt'].includes(ext)
    })
    if (files.length) uploadFiles(files)
  }

  const removeFile = async (fileId: string) => {
    if (!activeSessions) return
    try {
      await api.sessionFiles.delete(activeSessions, fileId)
      removeSessionFile(fileId)
    } catch {}
  }

  // ── Send message ──────────────────────────────────────
  const handleSend = useCallback(async () => {
    if (!input.trim()) return
    // Steer: if Claude is streaming (thinking or generating), inject the user's
    // context immediately — abort current stream and restart with combined message.
    // User sees their message bubble + Claude reacting, just like Tasklet.
    if (isStreaming && input.trim()) {
      const sid = activeSessions
      if (!sid) return                       // no session — nothing to inject into
      const inj = input.trim()
      setInput('')
      if (textareaRef.current) textareaRef.current.style.height = 'auto'
      // Show user bubble so the injected message is visible in chat
      addMessage({
        id: Date.now().toString() + '_inj',
        session_id: sid,
        role: 'user',
        content: inj,
        created_at: new Date().toISOString(),
      })
      // Build combined message and update sentMessageRef so chained injections
      // accumulate correctly (inject #2 includes inject #1's context)
      const lastMsg = sentMessageMapRef.current.get(sid) || sentMessageRef.current
      const combined = lastMsg + '\n\n[Context added mid-stream]: ' + inj
      sentMessageRef.current = combined
      sentMessageMapRef.current.set(sid, combined)
      // Abort current stream and restart with combined context
      pendingInjectionRef.current = ''
      setInjectionQueued(false)
      abortMapRef.current.get(sid)?.abort()
      abortMapRef.current.delete(sid)
      clearSessionStream(sid)
      setRestartSignal({ msg: combined, sid })
      return
    }
    if (isStreaming) return
    if (!settings?.openai_api_key_set && !(settings as any)?.anthropic_api_key_set) {
      setError('Add your API key (OpenAI or Anthropic) in Settings first.')
      return
    }
    setError(null)
    // Picked-element chips are additive context shown above the composer —
    // never part of the visible draft — so they're folded into the outgoing
    // text only at send time, then cleared (mirrors how sessionFiles attach
    // without living inside the textarea).
    const text = appendPickedElementsContext(input.trim(), pickedElements)
    sentMessageRef.current = text
    setInput('')
    if (pickedElements.length > 0) clearPickedElements()
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
    userScrolledUpRef.current = false  // snap to bottom on user's own send

    const sessionId = await ensureSession()
    sentMessageMapRef.current.set(sessionId, text)

    // Capture whether this is the first message so we can auto-name the session
    const isFirstMessage = messages.length === 0

    // Helper: auto-rename session from first user message (removes technical noise)
    const autoNameSession = () => {
      const title = text.replace(/\s+/g, ' ').trim().slice(0, 55) || 'New Chat'
      api.chat.renameSession(sessionId, title)
        .then(() => api.chat.getSessions().then(setSessions).catch(() => {}))
        .catch(() => {})
    }

    // Add user message to UI immediately
    addMessage({
      id: Date.now().toString(),
      session_id: sessionId,
      role: 'user',
      content: text,
      created_at: new Date().toISOString(),
    })

    setSessionStreaming(sessionId, true)
    setSessionStreamProgress(sessionId, 'Thinking...')
    setSessionStreamingMessage(sessionId, '')
    setProgressHistory(['Thinking...'])
    setThinkingText('')
    setIsThinking(false)
    setIsBuildingEdit(false)
    thinkingTextRef.current = ''
    progressHistoryRef.current = ['Thinking...']

    doStream(sessionId, text, isFirstMessage, autoNameSession)
  }, [input, isStreaming, isThinking, settings, activeSessions, sessionFiles, doStream])

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleSend() }
  }

  const hasFiles = sessionFiles.length > 0

  return (
    <div
      className="flex flex-col h-full relative"
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* Drag overlay */}
      {isDragging && (
        <div className="absolute inset-0 z-50 bg-accent/10 border-2 border-dashed border-accent rounded-xl flex items-center justify-center pointer-events-none">
          <div className="text-center">
            <AttachFile sx={{ fontSize: 32 }} className="text-accent mx-auto mb-2" />
            <p className="text-accent font-semibold text-sm">Drop files here</p>
            <p className="text-accent/60 text-xs mt-1">py, ts, js, go, rs, and more</p>
          </div>
        </div>
      )}

      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept=".py,.js,.ts,.tsx,.jsx,.go,.rs,.java,.cs,.rb,.php,.swift,.kt,.html,.css,.scss,.sass,.less,.json,.jsonl,.ndjson,.xml,.yaml,.yml,.toml,.ini,.cfg,.env,.properties,.md,.rst,.txt,.sh,.bash,.zsh,.fish,.sql,.cpp,.c,.h,.hpp,.cc,.cxx,.m,.mm,.vue,.svelte,.astro,.prisma,.graphql,.gql,.proto,.r,.R,.scala,.dart,.lua,.zig,.v,.nim,.ex,.exs,.erl,.hs,.ml,.clj,.tf,.hcl,.dockerfile,.conf,.nginx,.log,.diff,.patch,.tex,.bib,.makefile,image/*,.pdf,.csv,.tsv,.xlsx,.xls,.zip,.tar,.gz,.7z,.rar"
        className="hidden"
        onChange={handleFileInput}
      />

      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border bg-base/50 flex-shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <Bolt sx={{ fontSize: 13 }} className="text-accent flex-shrink-0" />
          <span className="text-sm font-semibold text-ink">
            SurgicalAI
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {activeSessions && (
            <button
              onClick={() => { navigator.clipboard.writeText(activeSessions); }}
              className="text-[10px] font-mono text-muted/50 hover:text-accent bg-overlay/40 hover:bg-overlay px-1.5 py-0.5 rounded transition-colors leading-none"
              title={`Session ID: ${activeSessions}\nClick to copy`}
            >
              {activeSessions.slice(0, 8)}
            </button>
          )}
          {/* Inline model picker */}
          <div className="relative">
            <button
              onClick={() => setModelPickerOpen(v => !v)}
              className="text-[11px] font-mono text-muted/70 hover:text-accent bg-overlay/40 hover:bg-overlay px-1.5 py-0.5 rounded transition-colors leading-none flex items-center gap-1"
              title={
                agentRequiresClaudeForCurrentModel
                  ? `${settings?.architect_model || 'claude-sonnet-4-6'} — Agent Mode requires a Claude model. This will silently run as a normal single-pass edit instead of multi-agent tasks.`
                  : searchToolsUnavailableForCurrentModel
                    ? `${settings?.architect_model || 'claude-sonnet-4-6'} — file search/lookup tools are Claude-only in ${MODE_META[effectiveMode].label} mode. This model will use a basic file preview instead.`
                    : 'Change model'
              }
            >
              {(searchToolsUnavailableForCurrentModel || agentRequiresClaudeForCurrentModel) && (
                <Warning sx={{ fontSize: 11 }} className="text-warning" />
              )}
              {settings?.architect_model || 'claude-sonnet-4-6'}
              <span className="text-[9px]">▾</span>
            </button>
            {modelPickerOpen && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setModelPickerOpen(false)} />
                <div className="absolute right-0 top-full mt-1 z-50 bg-surface border border-border rounded-lg shadow-xl py-1 min-w-[220px] max-h-[300px] overflow-y-auto">
                  {(effectiveMode === 'ask' || effectiveMode === 'plan') && (
                    <div className="px-3 py-1.5 mb-1 border-b border-border/60 text-[10px] text-muted/80 leading-snug">
                      File search &amp; lookup tools work with Claude models only in {MODE_META[effectiveMode].label} mode.
                    </div>
                  )}
                  {effectiveMode === 'agent' && (
                    <div className="px-3 py-1.5 mb-1 border-b border-border/60 text-[10px] text-muted/80 leading-snug">
                      Agent Mode (multi-agent task breakdown) works with Claude models only.
                    </div>
                  )}
                  {availableModels.filter(m => m.role === 'architect').map(m => {
                    const isGpt = m.provider === 'openai'
                    const locked = isGpt && (effectiveMode === 'ask' || effectiveMode === 'plan' || effectiveMode === 'agent')
                    const lockLabel = effectiveMode === 'agent' ? 'Needs Claude' : 'No search'
                    const lockTitle = effectiveMode === 'agent'
                      ? `Agent Mode requires a Claude model. Selecting ${m.id} would silently run as a normal single-pass edit instead of multi-agent tasks.`
                      : `File search & lookup tools are Claude-only in ${MODE_META[effectiveMode].label} mode. ${m.id} would fall back to a basic file preview — not recommended.`
                    return (
                      <button
                        key={m.id}
                        onClick={() => { if (!locked) handleModelChange(m.id) }}
                        disabled={locked}
                        title={locked ? lockTitle : undefined}
                        className={`w-full text-left px-3 py-1.5 text-[12px] transition-colors ${
                          locked
                            ? 'opacity-45 cursor-not-allowed select-none'
                            : 'hover:bg-overlay'
                        } ${settings?.architect_model === m.id ? 'text-accent font-medium' : 'text-ink'}`}
                      >
                        <div className="font-mono flex items-center gap-1">
                          {m.id}
                          <ModelCostIndicator cost={m.cost} />
                          {locked && (
                            <span className="ml-auto flex items-center gap-0.5 text-[9px] uppercase tracking-wide text-muted shrink-0">
                              <Lock sx={{ fontSize: 11 }} className="text-muted" />
                              {lockLabel}
                            </span>
                          )}
                        </div>
                        {m.description && <div className="text-[10px] text-muted/70 mt-0.5 truncate">{m.description}</div>}
                      </button>
                    )
                  })}
                </div>
              </>
            )}
          </div>
          <button onClick={() => fileInputRef.current?.click()} className="p-1.5 rounded-lg hover:bg-overlay text-muted hover:text-ink transition-colors" title="Upload files">
            <AttachFile sx={{ fontSize: 13 }} />
          </button>
          <button onClick={newChat} className="p-1.5 rounded-lg hover:bg-overlay text-muted hover:text-ink transition-colors" title="New chat (⌘N)">
            <Add sx={{ fontSize: 14 }} />
          </button>
        </div>
      </div>

      {/* Compacting banner */}
      {isCompacting && (
        <div className="flex items-center justify-center gap-2 px-4 py-2 bg-accent/10 border-b border-accent/20 flex-shrink-0">
          <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
          <span className="text-[12px] text-accent font-medium">Compacting conversation history…</span>
        </div>
      )}

      {/* Messages */}
      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto">
        {messages.length === 0 && !isStreaming ? (
          <EmptyState onUpload={() => fileInputRef.current?.click()} />
        ) : (
          <div className="py-2">
            {messages.map((msg, i) => (
              <Message key={msg.id || i} msg={msg} sessionId={msg.session_id || activeSessions || ''} />
            ))}
            {resumableRun && resumableRun.sid === activeSessions && !isStreaming && (
              <div className="mx-3 mb-2 flex items-center justify-between gap-3 rounded-xl border border-warning/30 bg-warning/10 px-3.5 py-2.5 animate-slide-up">
                <span className="text-[12px] text-ink leading-snug">
                  This task run was interrupted — {resumableRun.tasks.length} task{resumableRun.tasks.length === 1 ? '' : 's'} remaining.
                </span>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    onClick={resumeInterruptedRun}
                    className="text-[11px] font-semibold px-2.5 py-1 rounded-lg text-accent bg-accent/10 border border-accent/20 hover:bg-accent/20 transition-colors"
                  >
                    Resume
                  </button>
                  <button
                    onClick={() => setResumableRun(null)}
                    className="text-[11px] font-medium px-2 py-1 rounded-lg text-muted hover:bg-overlay/60 transition-colors"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}
            <AgentMissionControl />
            {isStreaming && (streamingMessage || streamProgress) && (
              <StreamingBubble content={streamingMessage} progress={streamProgress} progressHistory={progressHistory} thinkingText={thinkingText} isThinking={isThinking} isBuildingEdit={isBuildingEdit} />
            )}
            {/* Human-in-the-loop: agent needs a file that isn't in the session */}
            {fileRequest && fileRequest.sessionId === activeSessions && (
              <div className="mx-4 my-3 rounded-xl border border-accent/40 bg-accent/10 px-4 py-3.5 animate-slide-up">
                <div className="flex items-start gap-2.5">
                  <AttachFile sx={{ fontSize: 16 }} className="flex-shrink-0 mt-0.5 text-accent" />
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-semibold text-ink flex items-center gap-1.5">
                      File needed to continue
                      <code className="px-1.5 py-0.5 rounded bg-surface/70 border border-border text-[11px] font-mono text-accent">{fileRequest.filename}</code>
                    </div>
                    <p className="mt-1 text-[12px] text-muted leading-snug">{fileRequest.message}</p>
                    <div className="mt-2.5 flex items-center gap-2">
                      <button
                        onClick={handleFileRequestUpload}
                        disabled={fileRequestBusy}
                        className="text-[12px] font-semibold px-3 py-1.5 rounded-lg text-white bg-accent hover:bg-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1.5"
                      >
                        <AttachFile sx={{ fontSize: 13 }} />
                        {fileRequestBusy ? 'Sending…' : 'Upload file'}
                      </button>
                      <button
                        onClick={handleFileRequestSkip}
                        disabled={fileRequestBusy}
                        className="text-[12px] font-medium px-2.5 py-1.5 rounded-lg text-muted hover:bg-overlay/60 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        Skip
                      </button>
                      <span className="text-[10.5px] text-muted/60 ml-auto">Waiting… the run resumes as soon as you respond</span>
                    </div>
                  </div>
                </div>
                <input
                  ref={fileRequestInputRef}
                  type="file"
                  className="hidden"
                  onChange={handleFileRequestFileChosen}
                />
              </div>
            )}
            {/* Steer queued indicator — brief flash while injection restarts stream */}
            {injectionQueued && isStreaming && (
              <div className="mx-4 mt-1.5 flex items-center gap-1.5 text-[11px] text-purple/60">
                <span className="w-1.5 h-1.5 rounded-full bg-purple/50 animate-pulse flex-shrink-0" />
                Injecting your context...
              </div>
            )}
            {error && (
              <div className="mx-4 my-3 flex items-start gap-2.5 px-3.5 py-3 bg-danger/10 border border-danger/30 rounded-xl text-sm text-danger">
                <Warning sx={{ fontSize: 15 }} className="flex-shrink-0 mt-0.5" />
                <span>{error}</span>
                <button onClick={() => setError(null)} className="ml-auto p-0.5 hover:text-danger">
                  <Close sx={{ fontSize: 11 }} />
                </button>
              </div>
            )}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Apply All — single click to apply every pending change across all files */}
      {activeSessions && !isStreaming && (
        <div className="px-3 pt-2 flex-shrink-0">
          <ApplyAllButton
            messages={messages}
            sessionId={activeSessions}
            sessionFiles={sessionFiles}
            setSessionFiles={setSessionFiles}
          />
        </div>
      )}

      {/* Input area */}
      <div className="border-t border-border p-3 flex-shrink-0 bg-base/50">
        {/* Session file drawer — single docked source of truth atop the composer */}
        {activeSessions && hasFiles && (
          <SessionFilesTray
            sessionId={activeSessions}
            sessionFiles={sessionFiles}
            onAddFiles={() => fileInputRef.current?.click()}
            onRemove={removeFile}
          />
        )}
        {uploadingFiles && (
          <div className="mb-2.5 flex items-center gap-1.5 px-2.5 py-1 bg-surface/60 border border-border rounded-lg w-fit">
            <span className="text-[11px] text-muted/70 animate-pulse">Uploading...</span>
          </div>
        )}

        <PickedElementsTray />

        {/* Prompt templates — quick-start chips */}
        {!isStreaming && (
          <div className="mb-2.5 flex flex-wrap gap-1.5">
            {[
              { Icon: LightbulbOutlined, label: 'Explain this', prompt: 'Explain what this code does in detail. Cover the logic, patterns used, and any potential issues.' },
              { Icon: BugReport, label: 'Find bugs', prompt: 'Review this code for bugs, edge cases, and potential runtime errors. List each issue found.' },
              { Icon: Security, label: 'Error handling', prompt: 'Add comprehensive error handling. Use specific exception types and handle edge cases.' },
              { Icon: Biotech, label: 'Write tests', prompt: 'Write unit tests for this code. Cover happy path, edge cases, and error cases.' },
              { Icon: AutoFixHigh, label: 'Refactor', prompt: 'Refactor this code for readability and maintainability. Improve naming and reduce complexity.' },
              { Icon: AccountTree, label: 'Project Structure', prompt: 'Analyze the uploaded files and produce a clean markdown outline of the project architecture. Use 3–4 top-level sections (e.g. Frontend, Backend, Services, Data). Under each section list the key modules/files with a one-line description. Keep it under 20 lines total. Use plain markdown — no diagrams, no code blocks.' },
            ].map(({ Icon, label, prompt }) => (
              <button
                key={label}
                onClick={() => { setInput(prompt); textareaRef.current?.focus() }}
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-surface/60 border border-border/60 text-[11px] text-muted hover:text-ink hover:border-border transition-colors"
              >
                <Icon sx={{ fontSize: 13 }} />
                {label}
              </button>
            ))}
          </div>
        )}

        {/* Persistent mode chip — always in direct line of sight before typing,
            so the active mode is never buried in a toolbar dropdown. */}
        <div className="mb-1.5 flex items-center gap-1.5">
          <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold ${MODE_COLOR[effectiveMode].bg} ${MODE_COLOR[effectiveMode].text}`}>
            {(() => { const I = MODE_META[effectiveMode].icon; return <I sx={{ fontSize: 11 }} /> })()}
            {MODE_META[effectiveMode].label} Mode
          </span>
        </div>

        {agentRequiresClaudeForCurrentModel && !agentClaudeNoticeDismissed && (
          <div className="mb-1.5 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-1.5 animate-slide-up">
            <Warning sx={{ fontSize: 14 }} className="text-warning shrink-0" />
            <span className="flex-1 text-[11px] text-ink/80 leading-snug">
              Agent Mode needs a Claude model. With <strong>{settings?.architect_model || 'the current model'}</strong> selected, this will silently run as a normal single-pass edit instead of multi-agent tasks.
            </span>
            <button
              onClick={() => setAgentClaudeNoticeDismissed(true)}
              className="text-muted/60 hover:text-ink/80 transition-colors shrink-0"
              title="Dismiss"
            >
              <Close sx={{ fontSize: 14 }} />
            </button>
          </div>
        )}

        {/* Unified input pill — Claude/Tasklet style */}
        <div className={`relative flex flex-col bg-surface/80 border rounded-2xl shadow-lg shadow-black/20 focus-within:shadow-accent/5 transition-all ${
          effectiveMode !== 'edit' ? MODE_COLOR[effectiveMode].border : 'border-border/80 focus-within:border-border'
        }`}>
          <button
            type="button"
            onClick={() => setIsComposerExpanded(prev => !prev)}
            className="absolute top-2 right-2 z-10 h-6 px-2 rounded-md text-[10px] font-medium text-muted/60 hover:text-ink/80 hover:bg-overlay/60 transition-colors flex items-center gap-1"
            title={isComposerExpanded ? 'Collapse input' : 'Expand input'}
          >
            {isComposerExpanded ? '⤡ Collapse' : '⤢ Expand'}
          </button>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKey}
            disabled={isCompacting}
            placeholder={
              isCompacting
                ? 'Compacting history, please wait…'
                : hasFiles
                  ? `Ask about your ${sessionFiles.length} file${sessionFiles.length > 1 ? 's' : ''} — "Add X", "Fix Y", "Explain Z"…`
                  : 'Ask anything, or drop files here to edit code…'
            }
            rows={1}
            onInput={(e) => {
              const el = e.currentTarget
              const maxHeight = isComposerExpanded ? 480 : 200
              el.style.height = 'auto'
              el.style.height = Math.min(el.scrollHeight, maxHeight) + 'px'
            }}
            style={{ maxHeight: isComposerExpanded ? 480 : 200 }}
            className="w-full bg-transparent text-sm text-ink placeholder:text-muted/70 resize-none px-4 pr-16 pt-3 pb-2 focus:outline-none leading-relaxed font-[inherit] min-h-[44px] max-h-[200px] overflow-y-auto"
          />
          {/* Bottom toolbar — flex row below textarea, never overlaps */}
          <div className="flex items-center justify-between px-2 pb-2">
            <div className="flex items-center gap-1">
              <button
                onClick={() => fileInputRef.current?.click()}
                className="h-8 w-8 rounded-lg text-muted/70 hover:text-ink/80 hover:bg-overlay/60 transition-colors flex items-center justify-center"
                title="Attach files"
              >
                <AttachFile sx={{ fontSize: 15 }} />
              </button>
              <VoiceButton
                onTranscript={(text) => setInput(prev => prev ? prev + ' ' + text : text)}
                lastResponse={messages.filter(m => m.role === 'assistant' && m.content).slice(-1)[0]?.content}
                disabled={isStreaming || isCompacting}
              />
              {/* Mode selector — Edit (default) | Ask | Plan | Agent */}
              <div className="relative">
                <button
                  onClick={() => setModeMenuOpen(o => !o)}
                  disabled={isStreaming || isCompacting}
                  title="Choose how the assistant responds"
                  className={`h-8 px-2.5 rounded-lg text-xs font-medium flex items-center gap-1.5 transition-colors disabled:opacity-40 ${
                    effectiveMode !== 'edit'
                      ? `${MODE_COLOR[effectiveMode].bg} ${MODE_COLOR[effectiveMode].text} hover:brightness-110`
                      : 'text-muted/70 hover:text-ink/80 hover:bg-overlay/60'
                  }`}
                >
                  {(() => { const I = MODE_META[effectiveMode].icon; return <I sx={{ fontSize: 14 }} /> })()}
                  {MODE_META[effectiveMode].label}
                  <span className="text-[9px] opacity-60 leading-none">▾</span>
                </button>
                {modeMenuOpen && (
                  <>
                    {/* click-outside backdrop */}
                    <div className="fixed inset-0 z-40" onClick={() => setModeMenuOpen(false)} />
                    <div className="absolute bottom-full left-0 mb-2 w-64 p-1.5 rounded-xl bg-surface border border-border/80 shadow-xl shadow-black/40 z-50">
                      {isOffline && (
                        <div className="flex items-center gap-1.5 px-2.5 pt-1 pb-2 mb-1 border-b border-border/50">
                          <Bolt sx={{ fontSize: 14 }} className="text-amber-400" />
                          <span className="text-[11px] font-semibold text-ink">Offline · local model</span>
                          <span className="ml-auto text-[10px] text-muted font-mono truncate max-w-[92px]">
                            {(settings?.architect_model || 'qwen').replace('ollama:', '')}
                          </span>
                        </div>
                      )}
                      {availableModes.map(m => {
                        const meta = MODE_META[m]
                        const color = MODE_COLOR[m]
                        const I = meta.icon
                        const active = effectiveMode === m
                        return (
                          <button
                            key={m}
                            onClick={() => { selectChatMode(m); setModeMenuOpen(false) }}
                            className={`w-full text-left px-2.5 py-2 rounded-lg flex items-start gap-2 transition-colors ${
                              active ? color.bg : 'hover:bg-overlay/60'
                            }`}
                          >
                            <I sx={{ fontSize: 15 }} className={active ? `${color.text} mt-0.5` : 'text-muted mt-0.5'} />
                            <span className="flex-1 min-w-0">
                              <span className={`block text-xs font-semibold ${active ? color.text : 'text-ink'}`}>{meta.label}</span>
                              <span className="block text-[11px] text-muted leading-snug">{meta.desc}</span>
                            </span>
                            {active && <span className={`w-1.5 h-1.5 rounded-full ${color.dot} mt-1.5 shrink-0`} />}
                          </button>
                        )
                      })}
                      {isOffline && (
                        <>
                          <div className="mt-1 pt-1 border-t border-border/50">
                            {(['plan', 'agent'] as ChatMode[]).map(m => {
                              const meta = MODE_META[m]
                              const I = meta.icon
                              return (
                                <div
                                  key={m}
                                  title="Requires a cloud model (Claude). Add an API key in Settings."
                                  className="w-full text-left px-2.5 py-2 rounded-lg flex items-start gap-2 opacity-45 cursor-not-allowed select-none"
                                >
                                  <I sx={{ fontSize: 15 }} className="text-muted mt-0.5" />
                                  <span className="flex-1 min-w-0">
                                    <span className="block text-xs font-semibold text-ink">{meta.label}</span>
                                    <span className="block text-[11px] text-muted leading-snug">{meta.desc}</span>
                                  </span>
                                  <span className="flex items-center gap-1 mt-0.5 shrink-0">
                                    <Lock sx={{ fontSize: 12 }} className="text-muted" />
                                    <span className="text-[9px] uppercase tracking-wide text-muted">Cloud</span>
                                  </span>
                                </div>
                              )
                            })}
                          </div>
                          <div className="px-2.5 pt-1.5 pb-1 text-[10px] text-faint leading-snug">
                            Plan &amp; Agent need a cloud model. Add an API key in Settings to enable them.
                          </div>
                        </>
                      )}
                    </div>
                  </>
                )}
              </div>
              <span className="text-[11px] text-faint ml-1 select-none">
                {hasFiles ? `${sessionFiles.length} file${sessionFiles.length > 1 ? 's' : ''} attached` : ''}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] text-faint mr-1 select-none">⌘↵</span>
              {isStreaming && (
                <button
                  onClick={() => {
                    const sid = useAppStore.getState().activeSessions
                    if (sid) { abortMapRef.current.get(sid)?.abort(); abortMapRef.current.delete(sid); saveAbortedMessage(sid) }
                    else stopStream(undefined)
                  }}
                  className="h-8 px-3 rounded-lg bg-danger/15 text-danger text-xs font-semibold flex items-center gap-1.5 hover:bg-danger/25 transition-colors"
                >
                  <Close sx={{ fontSize: 13 }} /> Stop
                </button>
              )}
              {/* Send button: always visible. During thinking, acts as Steer (queues context). */}
              {(!isStreaming || isThinking) && (
                <button
                  onClick={handleSend}
                  disabled={!input.trim() || isCompacting}
                  className={`h-8 w-8 rounded-lg flex items-center justify-center transition-all ${
                    !input.trim() || isCompacting
                      ? 'text-faint cursor-not-allowed'
                      : isThinking
                        ? 'bg-purple/80 text-white hover:bg-purple active:scale-95 shadow-sm shadow-purple/25'
                        : 'bg-accent text-white hover:bg-accent active:scale-95 shadow-sm shadow-accent/25'
                  }`}
                  title={isThinking ? 'Send as steering context' : 'Send message'}
                >
                  <Send sx={{ fontSize: 14 }} />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
