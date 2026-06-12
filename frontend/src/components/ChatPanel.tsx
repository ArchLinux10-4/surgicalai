import React, { useState, useRef, useEffect, useCallback, Component } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { InlineDiffCard } from './InlineDiffCard'
import { NewFileCard } from './NewFileCard'
import { MarkdownCode } from './CodeBlock'
import { SessionFilesTray } from './SessionFilesTray'
import { AgentMissionControl } from './AgentMissionControl'
import { useTaskPolling } from '../hooks/useTaskPolling'
import type { SessionFile, SmartResult } from '../types'
import { AccountTree, Add, AttachFile, AutoFixHigh, Biotech, Bolt, BugReport, Close, Delete, Description, DoneAll, LightbulbOutlined, Psychology, Security, Send, Warning } from '@mui/icons-material';
import { VoiceButton } from './VoiceButton'
import { validateFileSize } from '../utils/fileValidation'

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

  // Collect all messages with unapplied changes
  const pendingMessages = messages.filter(m =>
    (m.message_type === 'natural_result' || m.message_type === 'surgical_result') &&
    m.surgical_data
  )

  if (pendingMessages.length === 0) return null

  // Count total changes across all pending messages
  let totalChanges = 0
  let totalFiles   = 0
  for (const msg of pendingMessages) {
    try {
      const result: SmartResult = JSON.parse(msg.surgical_data)
      const files = Object.keys(result.changes_by_file || {})
      totalFiles   += files.length
      totalChanges += files.reduce(
        (acc, f) => acc + (result.changes_by_file[f]?.changes?.length || 0), 0
      )
      totalChanges += (result.new_files || []).length
    } catch {}
  }

  if (totalChanges === 0) return null

  const handleApplyAll = async () => {
    setApplying(true)
    let appliedFiles = 0
    let failed       = 0

    const markPromises: Promise<any>[] = []
    try {
      for (const msg of pendingMessages) {
        let result: SmartResult
        try { result = JSON.parse(msg.surgical_data) } catch { continue }

        // Apply edits per file
        for (const [, fileData] of Object.entries(result.changes_by_file || {})) {
          const fd = fileData as any
          if (!fd?.file_id || !fd?.changes?.length) continue
          try {
            const current = await api.sessionFiles.get(sessionId, fd.file_id)
            const applied = await api.surgical.applyAll({
              file_path: fd.filename,
              changes: fd.changes,
              file_content: current.content,
            })
            if (applied.modified_content) {
              await api.sessionFiles.update(sessionId, fd.file_id, applied.modified_content)
              appliedFiles++
              // Track every applied change in DB so state survives refresh
              for (const ch of fd.changes) {
                if (ch?.id) markPromises.push(api.surgical.markApplied(sessionId, ch.id).catch(() => {}))
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

      if (failed === 0) {
        toast.success(`Applied all changes across ${appliedFiles} file${appliedFiles !== 1 ? 's' : ''}`)
        window.dispatchEvent(new CustomEvent('sai-applied-refresh'))
        setDone(true)
      } else {
        toast.error(`Applied ${appliedFiles} file(s) — ${failed} failed`)
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
        : `Apply All  ·  ${totalChanges} change${totalChanges !== 1 ? 's' : ''} across ${totalFiles} file${totalFiles !== 1 ? 's' : ''}`
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

  // Compact marker chip
  if (msg.message_type === 'compact_marker') {
    return (
      <div className="flex items-center justify-center py-2 px-4">
        <div className="flex items-center gap-1.5 px-3 py-1 bg-surface/60 border border-border/50 rounded-full">
          <span className="text-[11px] text-muted/70">📦 Earlier conversation compacted</span>
        </div>
      </div>
    )
  }

  let surgicalResult: SmartResult | null = null
  if ((isSurgical || isNaturalResult) && msg.surgical_data) {
    try { surgicalResult = JSON.parse(msg.surgical_data) } catch {}
  }

  // ── User bubble (right-aligned) ──
  if (isUser) {
    return (
      <div className="flex justify-end px-4 py-3 group">
        <div className="max-w-[78%]">
          <div className="flex items-center justify-end gap-2 mb-1">
            <span className="text-[10px] text-faint opacity-0 group-hover:opacity-100 transition-opacity">{time}</span>
            <span className="text-[11px] font-medium text-muted/70">You</span>
          </div>
          <div className="bg-overlay/60 border border-border/40 rounded-2xl rounded-tr-sm px-4 py-2.5 text-sm text-ink leading-relaxed whitespace-pre-wrap shadow-sm">
            {msg.content}
          </div>
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
          <span className="text-[10px] text-faint opacity-0 group-hover:opacity-100 transition-opacity">{time}</span>
        </div>

        {/* Persistent thinking trail — shown after streaming completes */}
        {(msg._thinking || (msg._steps && msg._steps.filter((s: string) => s !== 'Thinking...').length > 0)) && (
          <div className="mb-3 space-y-1">
            {msg._steps && <PersistentSteps steps={msg._steps} />}
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

// ── Persistent steps trail (shown on completed messages) ────────────────
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

function StreamingBubble({ content, progress, progressHistory, thinkingText, isThinking, isBuildingEdit }: { content: string; progress: string; progressHistory: string[]; thinkingText?: string; isThinking?: boolean; isBuildingEdit?: boolean }) {
  const [thinkingExpanded, setThinkingExpanded] = useState(true)
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
          {/* Current progress badge */}
          {progress && (
            <span className="text-[11px] text-accent flex items-center gap-1.5 bg-accent/10 px-2 py-0.5 rounded-full border border-accent/20">
              <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
              {progress}
            </span>
          )}
          {/* Elapsed timer */}
          {elapsed > 0 && !content && (
            <span className="text-[10px] text-muted/70 tabular-nums">{elapsed}s</span>
          )}
        </div>

        {/* Collapsible thinking trail */}
        {hasSteps && !content && (
          <div className="mb-2">
            <button
              onClick={() => setThinkingExpanded(e => !e)}
              className="text-[11px] text-muted/70 hover:text-ink/80 flex items-center gap-1 transition-colors"
            >
              <span>{thinkingExpanded ? '▾' : '▸'}</span>
              <span>{completedSteps.length} step{completedSteps.length !== 1 ? 's' : ''} completed</span>
            </button>
            {thinkingExpanded && (
              <div className="mt-1.5 pl-3 border-l-2 border-border/60 space-y-1">
                {completedSteps.map((step, i) => (
                  <div key={i} className="text-[11px] text-muted/70 flex items-center gap-1.5">
                    <span className="text-success/80">✓</span>
                    <span>{step}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
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

// ── Main Chat Panel ───────────────────────────────────────
export function ChatPanel() {
  const {
    activeSessions, setActiveSession, messages, addMessage, setMessages,
    isStreaming, setIsStreaming, streamingMessage, setStreamingMessage,
    streamProgress, setStreamProgress, sessions, setSessions, settings, setSettings,
    sessionFiles, setSessionFiles, addSessionFile, removeSessionFile,
    setAgentTasks, updateAgentTask, clearAgentTasks, setTaskRunId, setTaskPreamble, setAgentPhase,
    pendingChatInput, setPendingChatInput,
  } = useAppStore()

  // Keep the agentic task list in sync with Claude's DB-backed progress while a
  // run is active (resilient fallback if the live stream drops mid-run).
  useTaskPolling(activeSessions)

  const [input, setInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [uploadingFiles, setUploadingFiles] = useState(false)
  const [isBuildingEdit, setIsBuildingEdit] = useState(false)

  // Consume pending input injected from sidebar components (e.g. deploy watcher "Ask Claude to fix")
  useEffect(() => {
    if (pendingChatInput) {
      setInput(pendingChatInput)
      setPendingChatInput(null)
      setTimeout(() => textareaRef.current?.focus(), 50)
    }
  }, [pendingChatInput])

  const [progressHistory, setProgressHistory] = useState<string[]>([])
  const [thinkingText, setThinkingText] = useState('')
  const [isThinking, setIsThinking] = useState(false)
  const [isCompacting, setIsCompacting] = useState(false)
  const [availableModels, setAvailableModels] = useState<{id: string; name: string; role: string; description?: string}[]>([])
  const [modelPickerOpen, setModelPickerOpen] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const streamSessionRef = useRef<string | null>(null)
  const thinkingTextRef = useRef('')
  const progressHistoryRef = useRef<string[]>([])

  // Mid-thought injection state
  const [injectionInput, setInjectionInput] = useState('')
  const [injectionQueued, setInjectionQueued] = useState(false)
  const pendingInjectionRef = useRef<string>('')
  const sentMessageRef = useRef<string>('')
  // v1.4: holds the planned run while the planning stream closes, so the
  // per-task execution queue can start once /smart-stream returns.
  const pendingRunRef = useRef<{ runId: string; tasks: any[] } | null>(null)
  const [restartSignal, setRestartSignal] = useState<{ msg: string; sid: string } | null>(null)

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingMessage])

  // Load session files when session changes
  useEffect(() => {
    // Abort any stream from a previous session to prevent cross-session bleed
    if (streamSessionRef.current && streamSessionRef.current !== activeSessions) {
      abortRef.current?.abort()
      stopStream()
      streamSessionRef.current = null
    }
    if (activeSessions) {
      api.sessionFiles.list(activeSessions)
        .then(files => setSessionFiles(files))
        .catch(() => {})
      // Reconcile the agentic task list to whatever this session has on the
      // server (clears stale cross-session state; repopulates if a run exists).
      clearAgentTasks()
      api.tasks.list(activeSessions)
        .then((rows: any[]) => {
          if (!Array.isArray(rows) || rows.length === 0) return
          const latestRun = rows[0]?.run_id
          const forRun = rows.filter(r => r.run_id === latestRun)
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
      if (e.key === 'Escape' && isStreaming) { abortRef.current?.abort(); stopStream() }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [isStreaming])

  // Load available models for inline model picker
  useEffect(() => {
    api.settings.getModels().then((d: any) => setAvailableModels(d.models || [])).catch(() => {})
  }, [])

  const stopStream = () => {
    setIsStreaming(false)
    setStreamingMessage('')
    setStreamProgress('')
  }

  // ── Core stream launcher — shared by handleSend and injection restart ─────
  const doStream = useCallback((
    sessionId: string,
    messageText: string,
    isFirst: boolean,
    autoRename: () => void,
  ) => {
    let accumulated = ''
    let gotResult = false

    // ── v1.4 per-task execution ───────────────────────────────────────────
    // /smart-stream now ends right after planning. We then run each task in
    // its own short-lived SSE stream, sequentially, so no single connection
    // can hit the proxy/process timeout that previously killed long runs.
    const addTaskResultCard = (result: any) => {
      const naturalText = (result.natural_text || '')
        .replace(/<new_file>[\s\S]*?<\/new_file>/g, '')
        .replace(/<new_file>[\s\S]*$/, '')
        .trim()
      addMessage({
        id: Date.now().toString() + '_task_' + Math.random().toString(36).slice(2, 7),
        session_id: sessionId,
        role: 'assistant',
        message_type: naturalText ? 'natural_result' : 'surgical_result',
        surgical_data: JSON.stringify(result),
        content: naturalText,
        created_at: new Date().toISOString(),
      })
      api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
    }

    const handleTaskEvent = (event: any) => {
      switch (event.type) {
        case 'task_start':
          updateAgentTask(event.id, { status: 'running', progress: undefined }); break
        case 'task_progress':
          updateAgentTask(event.id, { progress: event.content }); break
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

    const finishTaskRun = () => {
      setIsStreaming(false); setStreamingMessage(''); setStreamProgress('')
      setIsBuildingEdit(false)
      setAgentPhase('complete')
      if (isFirst) autoRename()
      else api.chat.getSessions().then(setSessions).catch(() => {})
      api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
    }

    const runTaskQueue = (sid: string, runId: string, tasks: any[]) => {
      let idx = 0
      const runNext = () => {
        if (useAppStore.getState().activeSessions !== sid || idx >= tasks.length) {
          finishTaskRun(); return
        }
        const t = tasks[idx++]
        const ctrl = api.stream.executeTask(
          { session_id: sid, run_id: runId, task_id: t.id },
          (progress) => { if (useAppStore.getState().activeSessions === sid) setStreamProgress(progress) },
          (result) => addTaskResultCard(result),
          () => {},  // per-task stream closed; queue advances on task_done
          (err) => { setError(err); finishTaskRun() },
          (event) => {
            handleTaskEvent(event)
            if (event.type === 'task_done') runNext()
            else if (event.type === 'task_blocked' || event.type === 'task_cancelled') finishTaskRun()
          },
        )
        abortRef.current = ctrl
      }
      runNext()
    }

    const ctrl = api.stream.smart(
      { session_id: sessionId, message: messageText, file_ids: sessionFiles.map(f => f.id) },
      (progress) => {
        if (useAppStore.getState().activeSessions !== sessionId) return
        setStreamProgress(progress)
        setProgressHistory(prev => {
          if (prev[prev.length - 1] !== progress) {
            const next = [...prev, progress]
            progressHistoryRef.current = next
            return next
          }
          return prev
        })
      },
      (token) => { if (useAppStore.getState().activeSessions !== sessionId) return; accumulated += token; setStreamingMessage(accumulated) },
      (result) => {
        gotResult = true
        const _thinking = thinkingTextRef.current
        const _steps = [...progressHistoryRef.current]
        const naturalText = (result.natural_text || accumulated)
          .replace(/<new_file>[\s\S]*?<\/new_file>/g, '')
          .replace(/<new_file>[\s\S]*$/, '')
          .trim()

        setIsStreaming(false); setStreamingMessage(''); setStreamProgress('')
        setIsBuildingEdit(false)

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
          })
        }
        if (isFirst) autoRename()
        else api.chat.getSessions().then(setSessions).catch(() => {})
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
      },
      (fullText) => {
        // Planning stream closed — if a task run was planned, start executing
        // tasks one at a time (each in its own SSE stream) instead of the
        // single-pass teardown below.
        if (pendingRunRef.current) {
          const run = pendingRunRef.current
          pendingRunRef.current = null
          runTaskQueue(sessionId, run.runId, run.tasks)
          return
        }
        if (gotResult) return
        const _thinking = thinkingTextRef.current
        const _steps = [...progressHistoryRef.current]
        setIsStreaming(false); setStreamingMessage(''); setStreamProgress('')
        setIsBuildingEdit(false)
        if (fullText.trim()) {
          addMessage({
            id: Date.now().toString() + '_ai',
            session_id: sessionId,
            role: 'assistant',
            content: fullText,
            created_at: new Date().toISOString(),
            _thinking,
            _steps,
          })
        }
        if (isFirst) autoRename()
        else api.chat.getSessions().then(setSessions).catch(() => {})
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
      },
      (err) => {
        if (accumulated.trim() && !gotResult) {
          addMessage({
            id: Date.now().toString() + '_ai_err',
            session_id: sessionId,
            role: 'assistant',
            content: accumulated.trim(),
            created_at: new Date().toISOString(),
            _thinking: thinkingTextRef.current,
            _steps: [...progressHistoryRef.current],
          })
          gotResult = true
        }
        setError(err)
        setIsStreaming(false); setStreamingMessage(''); setStreamProgress('')
        setIsBuildingEdit(false)
        setTimeout(async () => {
          try {
            if (useAppStore.getState().activeSessions !== sessionId) return
            const saved = await api.chat.getMessages(sessionId)
            if (saved?.length) setMessages(saved)
          } catch {}
        }, 3000)
      },
      // onThinking — injection point: when thinking ends and injection is queued, restart
      (thinkToken, phase) => {
        if (useAppStore.getState().activeSessions !== sessionId) return
        if (phase === 'start') {
          setIsThinking(true); setThinkingText(''); thinkingTextRef.current = ''
        } else if (phase === 'delta') {
          setThinkingText(prev => { const next = prev + thinkToken; thinkingTextRef.current = next; return next })
        } else if (phase === 'end') {
          setIsThinking(false)
          if (pendingInjectionRef.current) {
            const inj = pendingInjectionRef.current
            pendingInjectionRef.current = ''
            setInjectionQueued(false)
            abortRef.current?.abort()
            setIsStreaming(false); setStreamingMessage(''); setStreamProgress('')
            const combined = sentMessageRef.current + '\n\n[Context added while thinking]: ' + inj
            setRestartSignal({ msg: combined, sid: sessionId })
          }
        }
      },
      // onCompacting
      (phase) => {
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
            created_at: new Date().toISOString(),
          })
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
            pendingRunRef.current = { runId: event.run_id, tasks: event.tasks || [] }
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
      }
    )
    abortRef.current = ctrl
  }, [sessionFiles]) // all setters are stable; only sessionFiles can change

  // Restart stream when an injection was queued — fires once isStreaming settles to false
  useEffect(() => {
    if (!restartSignal || isStreaming) return
    const { msg, sid } = restartSignal
    setRestartSignal(null)
    setIsStreaming(true)
    setStreamProgress('Applying your context...')
    setStreamingMessage('')
    setProgressHistory(['Applying your context...'])
    setThinkingText('')
    setIsThinking(false)
    thinkingTextRef.current = ''
    progressHistoryRef.current = ['Applying your context...']
    doStream(sid, msg, false, () => {
      api.chat.getSessions().then(setSessions).catch(() => {})
    })
  }, [restartSignal, isStreaming, doStream])

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
    // Abort any running stream from the previous session
    abortRef.current?.abort()
    stopStream()
    streamSessionRef.current = null
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
    if (!input.trim() || isStreaming) return
    if (!settings?.openai_api_key_set && !(settings as any)?.anthropic_api_key_set) {
      setError('Add your API key (OpenAI or Anthropic) in Settings first.')
      return
    }
    setError(null)
    const text = input.trim()
    sentMessageRef.current = text
    setInput('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

    const sessionId = await ensureSession()
    streamSessionRef.current = sessionId

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

    setIsStreaming(true)
    setStreamProgress('Thinking...')
    setStreamingMessage('')
    setProgressHistory(['Thinking...'])
    setThinkingText('')
    setIsThinking(false)
    setIsBuildingEdit(false)
    thinkingTextRef.current = ''
    progressHistoryRef.current = ['Thinking...']

    doStream(sessionId, text, isFirstMessage, autoNameSession)
  }, [input, isStreaming, settings, activeSessions, sessionFiles, doStream])

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
        accept=".py,.js,.ts,.tsx,.jsx,.go,.rs,.java,.cs,.rb,.php,.swift,.kt,.html,.css,.json,.yaml,.yml,.toml,.md,.sh,.sql,.cpp,.c,.h,image/*,.pdf,.csv,.xlsx,.xls,.txt,.zip"
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
              title="Change model"
            >
              {settings?.architect_model || 'gpt-4.1'}
              <span className="text-[9px]">▾</span>
            </button>
            {modelPickerOpen && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setModelPickerOpen(false)} />
                <div className="absolute right-0 top-full mt-1 z-50 bg-surface border border-border rounded-lg shadow-xl py-1 min-w-[220px] max-h-[300px] overflow-y-auto">
                  {availableModels.filter(m => m.role === 'architect').map(m => (
                    <button
                      key={m.id}
                      onClick={() => handleModelChange(m.id)}
                      className={`w-full text-left px-3 py-1.5 text-[12px] hover:bg-overlay transition-colors ${settings?.architect_model === m.id ? 'text-accent font-medium' : 'text-ink'}`}
                    >
                      <div className="font-mono">{m.id}</div>
                      {m.description && <div className="text-[10px] text-muted/70 mt-0.5 truncate">{m.description}</div>}
                    </button>
                  ))}
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
      <div className="flex-1 overflow-y-auto">
        {messages.length === 0 && !isStreaming ? (
          <EmptyState onUpload={() => fileInputRef.current?.click()} />
        ) : (
          <div className="py-2">
            {messages.map((msg, i) => (
              <Message key={msg.id || i} msg={msg} sessionId={msg.session_id || activeSessions || ''} />
            ))}
            <AgentMissionControl />
            {isStreaming && (streamingMessage || streamProgress) && (
              <StreamingBubble content={streamingMessage} progress={streamProgress} progressHistory={progressHistory} thinkingText={thinkingText} isThinking={isThinking} isBuildingEdit={isBuildingEdit} />
            )}
            {/* Mid-thought injection input — visible only during the thinking phase */}
            {isThinking && !injectionQueued && (
              <div className="mx-4 mt-2">
                <div className="flex items-center gap-2 bg-surface/50 border border-purple/20 rounded-xl px-3 py-2 focus-within:border-purple/40 transition-colors">
                  <span className="text-[10px] font-semibold text-purple/60 uppercase tracking-wide whitespace-nowrap select-none">Steer</span>
                  <input
                    autoFocus
                    value={injectionInput}
                    onChange={e => setInjectionInput(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && injectionInput.trim()) {
                        pendingInjectionRef.current = injectionInput.trim()
                        setInjectionQueued(true)
                        setInjectionInput('')
                      }
                    }}
                    placeholder="Add context before Claude writes code... (Enter to queue)"
                    className="flex-1 bg-transparent text-xs text-ink placeholder:text-muted/40 focus:outline-none"
                  />
                </div>
              </div>
            )}
            {injectionQueued && isStreaming && (
              <div className="mx-4 mt-1.5 flex items-center gap-1.5 text-[11px] text-purple/60">
                <span className="w-1.5 h-1.5 rounded-full bg-purple/50 animate-pulse flex-shrink-0" />
                Context queued — will apply when thinking ends
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

        {/* Unified input pill — Claude/Tasklet style */}
        <div className="relative bg-surface/80 border border-border/80 rounded-2xl shadow-lg shadow-black/20 focus-within:border-border focus-within:shadow-accent/5 transition-all">
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
              el.style.height = 'auto'
              el.style.height = Math.min(el.scrollHeight, 200) + 'px'
            }}
            className="w-full bg-transparent text-sm text-ink placeholder:text-muted/70 resize-none pl-4 pr-4 pt-3 pb-10 focus:outline-none leading-relaxed font-[inherit] min-h-[52px] max-h-[200px] overflow-y-auto"
          />
          {/* Bottom toolbar inside pill */}
          <div className="absolute bottom-0 left-0 right-0 flex items-center justify-between px-2 pb-2">
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
              <span className="text-[11px] text-faint ml-1 select-none">
                {hasFiles ? `${sessionFiles.length} file${sessionFiles.length > 1 ? 's' : ''} attached` : ''}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] text-faint mr-1 select-none">⌘↵</span>
              {isStreaming ? (
                <button
                  onClick={() => { abortRef.current?.abort(); stopStream() }}
                  className="h-8 px-3 rounded-lg bg-danger/15 text-danger text-xs font-semibold flex items-center gap-1.5 hover:bg-danger/25 transition-colors"
                >
                  <Close sx={{ fontSize: 13 }} /> Stop
                </button>
              ) : (
                <button
                  onClick={handleSend}
                  disabled={!input.trim() || isCompacting}
                  className={`h-8 w-8 rounded-lg flex items-center justify-center transition-all ${
                    !input.trim() || isCompacting
                      ? 'text-faint cursor-not-allowed'
                      : 'bg-accent text-white hover:bg-accent active:scale-95 shadow-sm shadow-accent/25'
                  }`}
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
