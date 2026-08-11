/**
 * MobileChatPanel — full-screen chat for mobile.
 * Mirrors ChatPanel.tsx logic exactly — same API calls, same store, same streaming.
 * Render layer is completely separate: no shared JSX with desktop ChatPanel.
 */
import React, { useState, useRef, useEffect, useCallback, memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAppStore } from '../../stores/appStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'
import { MobileDiffCard } from './MobileDiffCard'
import { MobileModeSheet } from './MobileModeSheet'
import {
  CHAT_MODES,
  MODE_COLOR,
  MODE_META,
  degradeModeForOffline,
  isOfflineSettings,
  readChatMode,
  readWebResearch,
  webResearchAvailableFor,
  writeChatMode,
  writeWebResearch,
  type ChatMode,
} from './chatMode'
import { SessionFilesTray } from '../SessionFilesTray'
import { AgentMissionControl } from '../AgentMissionControl'
import { useTaskPolling } from '../../hooks/useTaskPolling'
import { VoiceButton } from '../VoiceButton'
import { useCodeRain } from '../../hooks/useCodeRain'
import { useThemeStore } from '../../stores/themeStore'
import type { SessionFile, SmartResult } from '../../types'
import { validateFileSize } from '../../utils/fileValidation'
import { clientLog } from '../../lib/clientLog'

// ── Thin progress steps component ───────────────────────────────────────────
function ProgressSteps({ steps }: { steps: string[] }) {
  const visible = steps.filter(s => s && s !== 'Thinking...')
  if (!visible.length) return null
  return (
    <div className="flex flex-col gap-0.5 mb-2">
      {visible.map((s, i) => (
        <div key={i} className="flex items-center gap-1.5 text-[11px] text-muted/70">
          <span className="text-success text-[10px]">✓</span>
          {s}
        </div>
      ))}
    </div>
  )
}

/** Lightweight thinking toggle — plain text (no Prism) to preserve lag wins. */
function MobileThinkingBlock({ text, isStreaming }: { text: string; isStreaming: boolean }) {
  const [expanded, setExpanded] = useState(isStreaming)
  const userToggledRef = useRef(false)
  useEffect(() => {
    if (isStreaming && !userToggledRef.current) setExpanded(true)
  }, [isStreaming])
  if (!text && !isStreaming) return null
  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => { userToggledRef.current = true; setExpanded(e => !e) }}
        className="flex items-center gap-1.5 text-[11px] text-purple"
      >
        <span className={`transform transition-transform ${expanded ? 'rotate-90' : ''}`}>▶</span>
        {isStreaming ? <span>Thinking<span className="animate-pulse">…</span></span> : <span>Reasoning</span>}
      </button>
      {expanded && text && (
        <div className="mt-1.5 ml-3 pl-2 border-l-2 border-purple/30 text-[11px] text-muted/90 whitespace-pre-wrap max-h-48 overflow-y-auto font-mono leading-relaxed">
          {text}
          {isStreaming && <span className="inline-block w-1 h-2.5 bg-purple/60 ml-0.5 animate-pulse" />}
        </div>
      )}
    </div>
  )
}

// ── Streaming bubble ─────────────────────────────────────────────────────────
// Plain text while streaming (no ReactMarkdown per token — major mobile jank).
// Completed history bubbles still markdown on finalize.
function StreamingBubble({ text, progress, isBuildingEdit, thinkingText, isThinking }: {
  text: string; progress: string; isBuildingEdit: boolean
  thinkingText?: string; isThinking?: boolean
}) {
  return (
    <div className="flex items-start gap-2.5 px-4 py-3">
      <div className="w-7 h-7 rounded-full bg-[rgba(74,222,128,0.12)] border border-[rgba(74,222,128,0.25)] flex items-center justify-center flex-shrink-0 mt-0.5">
        <span className="text-[#4ade80] text-[10px] font-bold">AI</span>
      </div>
      <div className="flex-1 min-w-0">
        {(thinkingText || isThinking) && (
          <MobileThinkingBlock text={thinkingText || ''} isStreaming={!!isThinking} />
        )}
        {progress && progress !== 'Thinking...' && (
          <div className="flex items-center gap-1.5 text-[11px] text-muted/70 mb-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-[#4ade80] animate-pulse" />
            {progress}
          </div>
        )}
        {isBuildingEdit && (
          <div className="flex items-center gap-1.5 text-[11px] text-warning/80 mb-1.5 px-2 py-1 bg-warning/10 rounded-lg border border-warning/20">
            <span className="w-1.5 h-1.5 rounded-full bg-warning animate-pulse" />
            Preparing code change...
          </div>
        )}
        {text && (
          <div className="text-sm text-ink leading-relaxed whitespace-pre-wrap break-words">
            {text}
          </div>
        )}
        {!text && !isBuildingEdit && !thinkingText && !isThinking && (
          <div className="flex gap-1">
            {[0, 1, 2].map(i => (
              <span key={i} className="w-1.5 h-1.5 rounded-full bg-muted/40 animate-bounce"
                style={{ animationDelay: `${i * 0.15}s` }} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// Compact marker chip — persisted (see backend __COMPACTION_EVENT__ rows), so
// it survives reload. Tap to reveal exactly what was kept in the summary that
// replaced the older turns, instead of a toast with no way to audit it.
function CompactMarkerChip({ msg }: { msg: any }) {
  const [open, setOpen] = useState(false)
  const summary: string = msg.compact_summary || ''
  const count: number = msg.compact_count || 0
  const hasSummary = summary.trim().length > 0
  return (
    <div className="flex flex-col items-center py-2 px-3">
      <button
        type="button"
        onClick={() => hasSummary && setOpen(o => !o)}
        className="flex items-center gap-1.5 px-3 py-1 bg-surface/60 rounded-full border border-border/40"
      >
        <span className="text-[10px] text-muted/50">
          📦 Earlier conversation compacted{count ? ` (${count})` : ''}
        </span>
        {hasSummary && <span className="text-[9px] text-muted/40">{open ? '▲' : '▼ view'}</span>}
      </button>
      {open && hasSummary && (
        <div className="mt-2 w-full text-[11px] leading-relaxed bg-surface/40 border border-border/40 rounded-lg p-2.5 whitespace-pre-wrap text-fg/80">
          {summary}
        </div>
      )}
    </div>
  )
}

// ── Message bubble ────────────────────────────────────────────────────────────
function MessageBubbleImpl({ msg, sessionId, sessionFiles, setSessionFiles }: {
  msg: any; sessionId: string
  sessionFiles: SessionFile[]; setSessionFiles: (f: SessionFile[]) => void
}) {
  const isUser = msg.role === 'user'
  const isResult = msg.message_type === 'natural_result' || msg.message_type === 'surgical_result'
  const [narrativeOpen, setNarrativeOpen] = useState(false)

  if (msg.message_type === 'compact_marker') {
    return <CompactMarkerChip msg={msg} />
  }

  let result: SmartResult | null = null
  if (isResult && msg.surgical_data) {
    try { result = JSON.parse(msg.surgical_data) } catch {}
  }

  if (isUser) {
    return (
      <div className="flex justify-end px-4 py-2">
        <div className="max-w-[82%] bg-overlay/60 border border-border/40 rounded-2xl rounded-tr-sm px-4 py-2.5 text-sm text-ink leading-relaxed">
          {msg.content}
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-start gap-2.5 px-4 py-3">
      <div className="w-7 h-7 rounded-full bg-[rgba(74,222,128,0.12)] border border-[rgba(74,222,128,0.25)] flex items-center justify-center flex-shrink-0 mt-0.5">
        <span className="text-[#4ade80] text-[10px] font-bold">AI</span>
      </div>
      <div className="flex-1 min-w-0 overflow-hidden">
        {msg._aborted && (
          <span className="inline-block text-[9px] font-medium text-danger/80 bg-danger/10 px-1.5 py-0.5 rounded mb-1.5">Stopped</span>
        )}
        {/* Steps trail */}
        {msg._steps && <ProgressSteps steps={msg._steps} />}

        {msg._thinking && (
          <MobileThinkingBlock text={msg._thinking} isStreaming={false} />
        )}

        {/* Natural text — collapse long narratives when Apply cards are present */}
        {msg.content && (() => {
          const narrative = msg.content as string
          const hasCards = Boolean(result)
          const longNarrative = narrative.length > 280
          const collapseNarrative = hasCards && longNarrative && !narrativeOpen
          if (collapseNarrative) {
            return (
              <button
                type="button"
                onClick={() => {
                  setNarrativeOpen(true)
                  clientLog('mobile_narrative_toggled', {
                    open: true,
                    contentLen: narrative.length,
                  }, sessionId)
                }}
                className="w-full flex items-center gap-2 px-2.5 py-1.5 mb-2 rounded-lg border border-border/50 bg-surface/40 text-left"
              >
                <span className="text-[12px] text-muted truncate flex-1">
                  {narrative.replace(/\s+/g, ' ').slice(0, 100)}…
                </span>
                <span className="text-[11px] font-semibold text-[#4ade80] flex-shrink-0">Show</span>
              </button>
            )
          }
          return (
            <div className="mb-2">
              <div className="text-sm text-ink leading-relaxed">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{narrative}</ReactMarkdown>
              </div>
              {hasCards && longNarrative && (
                <button
                  type="button"
                  onClick={() => {
                    setNarrativeOpen(false)
                    clientLog('mobile_narrative_toggled', {
                      open: false,
                      contentLen: narrative.length,
                    }, sessionId)
                  }}
                  className="mt-1 text-[11px] font-semibold text-muted/70"
                >
                  Hide summary
                </button>
              )}
            </div>
          )
        })()}

        {/* Diff card */}
        {result && (
          <>
            {result.recovered && (
              <div className="flex items-center gap-1.5 text-[11px] text-warning/90 px-2.5 py-1.5 mb-2 bg-warning/10 rounded-lg border border-warning/25">
                Recovered after interruption — apply or re-send
              </div>
            )}
            <MobileDiffCard
              result={result}
              sessionId={sessionId}
              sessionFiles={sessionFiles}
              setSessionFiles={setSessionFiles}
            />
          </>
        )}
      </div>
    </div>
  )
}

const MessageBubble = memo(MessageBubbleImpl, (prev, next) => (
  prev.msg.id === next.msg.id &&
  prev.msg.content === next.msg.content &&
  prev.msg.surgical_data === next.msg.surgical_data &&
  prev.msg.message_type === next.msg.message_type &&
  prev.msg._aborted === next.msg._aborted &&
  prev.msg._steps === next.msg._steps &&
  prev.msg._thinking === next.msg._thinking &&
  prev.msg.compact_summary === next.msg.compact_summary &&
  prev.msg.compact_count === next.msg.compact_count &&
  prev.sessionId === next.sessionId &&
  prev.sessionFiles === next.sessionFiles &&
  prev.setSessionFiles === next.setSessionFiles
))

// ── File chip ─────────────────────────────────────────────────────────────────
function FileChip({ file, onRemove }: { file: SessionFile; onRemove: () => void }) {
  const ext = file.filename.split('.').pop() || ''
  return (
    <div className="flex items-center gap-1.5 px-2.5 py-1 bg-surface border border-border rounded-lg max-w-[140px]">
      <span className="text-[10px] text-muted/70 font-mono truncate">{file.filename}</span>
      <button onClick={onRemove} className="flex-shrink-0 text-muted/50 hover:text-danger text-[10px] ml-0.5">✕</button>
    </div>
  )
}

// ── Full-screen compose sheet ─────────────────────────────────────────────────
function MobileComposeSheet({ value, onChange, onSend, onClose, isStreaming, disabled }: {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onClose: () => void
  isStreaming: boolean
  disabled: boolean
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    // Focus and move cursor to end
    if (ref.current) {
      ref.current.focus()
      const len = ref.current.value.length
      ref.current.setSelectionRange(len, len)
    }
  }, [])

  const handleSend = () => {
    onSend()
    onClose()
  }

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-base"
      style={{ paddingTop: 'max(env(safe-area-inset-top, 0px), 12px)' }}>
      {/* Header */}
      <div className="flex-shrink-0 flex items-center justify-between px-4 py-3 border-b border-border bg-surface/90">
        <button
          onClick={onClose}
          className="text-sm text-muted/70 hover:text-ink transition-colors px-1 py-1"
        >
          Cancel
        </button>
        <span className="text-[13px] font-medium text-ink/60">Compose</span>
        <button
          onClick={handleSend}
          disabled={!value.trim() || disabled || isStreaming}
          className="text-sm font-semibold text-[#4ade80] disabled:text-muted/40 transition-colors px-1 py-1"
        >
          Send
        </button>
      </div>

      {/* Large textarea */}
      <textarea
        ref={ref}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder="Ready when you are! Describe changes, or paste requirements..."
        className="flex-1 w-full resize-none bg-transparent px-5 py-4 text-base text-ink
          placeholder:text-muted/40 focus:outline-none leading-relaxed"
      />

      {/* Character count + hint */}
      <div className="flex-shrink-0 flex items-center justify-between px-5 py-3
        border-t border-border/50 bg-surface/50"
        style={{ paddingBottom: 'max(env(safe-area-inset-bottom, 0px), 12px)' }}>
        <span className="text-[11px] text-muted/40">Shift+Enter for new line</span>
        <span className={`text-[11px] tabular-nums ${value.length > 2000 ? 'text-warning' : 'text-muted/40'}`}>
          {value.length > 0 ? `${value.length.toLocaleString()} chars` : ''}
        </span>
      </div>
    </div>
  )
}

// ── Animated home screen ─────────────────────────────────────────────────────
function EmptyHomeScreen() {
  const canvasRef = useCodeRain(true)
  const { theme }  = useThemeStore()
  const isLight    = theme === 'light'

  const g   = isLight ? '#15803d' : '#4ade80'
  const ga  = (a: number) => isLight ? `rgba(21,128,61,${a})` : `rgba(74,222,128,${a})`

  return (
    <div className="relative flex flex-col items-center justify-center h-full overflow-hidden"
      style={{ background: isLight ? '#ffffff' : '#0a0a0a' }}>

      {/* Code rain — dark theme only */}
      {!isLight && (
        <canvas ref={canvasRef} className="absolute inset-0 w-full h-full"
          style={{ animation: 'sai-rain-fade 1.5s ease-in-out 3s forwards', opacity: 0.85 }} />
      )}

      {/* Light mode subtle grid */}
      {isLight && (
        <div className="absolute inset-0 pointer-events-none" style={{
          backgroundImage: 'linear-gradient(rgba(0,0,0,0.04) 1px,transparent 1px),linear-gradient(90deg,rgba(0,0,0,0.04) 1px,transparent 1px)',
          backgroundSize: '24px 24px',
        }} />
      )}

      {/* Center glow — dark only */}
      {!isLight && (
        <div className="absolute inset-0 pointer-events-none" style={{
          background: 'radial-gradient(ellipse at center,rgba(74,222,128,0.07) 0%,transparent 70%)',
          animation: 'sai-glow-fade 1.5s ease-in-out 3s forwards',
        }} />
      )}

      <div className="relative z-10 flex flex-col items-center gap-5 px-8 text-center">

        {/* Precision Scope icon */}
        <div className="relative" style={{ width: 88, height: 88, animation: 'sai-icon-out 1.2s ease-in-out 3s forwards' }}>
          <div className="absolute inset-0 rounded-full" style={{
            background: isLight ? 'radial-gradient(circle,rgba(21,128,61,0.08) 0%,transparent 70%)' : 'radial-gradient(circle,rgba(74,222,128,0.12) 0%,transparent 70%)',
          }} />
          <div className="w-full h-full rounded-[22px] flex items-center justify-center" style={{
            background: ga(0.07),
            border: `1px solid ${ga(0.28)}`,
            boxShadow: isLight ? '0 2px 16px rgba(21,128,61,0.08)' : '0 0 32px rgba(74,222,128,0.1)',
          }}>
            <svg width="56" height="56" viewBox="0 0 64 64" style={{ overflow: 'visible' }}>
              <style>{`
                @keyframes sai-spin    { from{transform:rotate(0deg)}  to{transform:rotate(360deg)}  }
                @keyframes sai-rspin   { from{transform:rotate(0deg)}  to{transform:rotate(-360deg)} }
                @keyframes sai-blink   { 0%,100%{opacity:1} 50%{opacity:0.15} }
                @keyframes sai-scan    { 0%,100%{transform:translateY(-22px);opacity:0} 20%{opacity:0.7} 80%{opacity:0.7} 100%{transform:translateY(22px);opacity:0} }
                @keyframes sai-icon-out  { to { opacity:0; transform:scale(0.85); } }
                @keyframes sai-rain-fade { to { opacity:0; } }
                @keyframes sai-glow-fade { to { opacity:0; } }
                @keyframes sai-tag-fade  { to { opacity:0; } }
              `}</style>
              <line x1="8" y1="32" x2="56" y2="32" stroke={ga(0.55)} strokeWidth="0.75" strokeDasharray="3 3" style={{ animation: 'sai-scan 2.4s ease-in-out infinite' }} />
              <circle cx="32" cy="32" r="27" fill="none" stroke={ga(0.22)} strokeWidth="0.75" strokeDasharray="4 3" style={{ animation: 'sai-spin 9s linear infinite', transformOrigin: '32px 32px' }} />
              <circle cx="32" cy="32" r="20" fill="none" stroke={ga(0.5)} strokeWidth="1" />
              <circle cx="32" cy="32" r="13" fill="none" stroke={ga(0.18)} strokeWidth="0.75" strokeDasharray="2 4" style={{ animation: 'sai-rspin 5s linear infinite', transformOrigin: '32px 32px' }} />
              <line x1="32" y1="4"  x2="32" y2="18" stroke={g} strokeWidth="1.5" strokeLinecap="round" />
              <line x1="32" y1="46" x2="32" y2="60" stroke={g} strokeWidth="1.5" strokeLinecap="round" />
              <line x1="4"  y1="32" x2="18" y2="32" stroke={g} strokeWidth="1.5" strokeLinecap="round" />
              <line x1="46" y1="32" x2="60" y2="32" stroke={g} strokeWidth="1.5" strokeLinecap="round" />
              <path d="M14 21 L14 14 L21 14" fill="none" stroke={ga(0.55)} strokeWidth="1" strokeLinecap="round" />
              <path d="M50 21 L50 14 L43 14" fill="none" stroke={ga(0.55)} strokeWidth="1" strokeLinecap="round" />
              <path d="M14 43 L14 50 L21 50" fill="none" stroke={ga(0.55)} strokeWidth="1" strokeLinecap="round" />
              <path d="M50 43 L50 50 L43 50" fill="none" stroke={ga(0.55)} strokeWidth="1" strokeLinecap="round" />
              <circle cx="32" cy="32" r="4.5" fill={ga(0.1)} stroke={g} strokeWidth="1.5" />
              <circle cx="32" cy="32" r="1.5" fill={g} style={{ animation: 'sai-blink 1.2s ease-in-out infinite' }} />
            </svg>
          </div>
        </div>

        <div>
          <h1 style={{ fontFamily: 'monospace', fontSize: 22, fontWeight: 700, color: isLight ? '#111827' : '#e2e8f0', letterSpacing: 1, margin: 0 }}>
            SurgicalAI
          </h1>
          <div style={{ fontFamily: 'monospace', fontSize: 11, color: g, marginTop: 5, letterSpacing: 0.5, opacity: 0.85, animation: 'sai-tag-fade 1.2s ease-in-out 3s forwards' }}>
            Precision code edits. Zero collateral.
          </div>
        </div>

        <div className="flex flex-wrap justify-center gap-2">
          {['Symbol-level edits', 'QA verified', 'Multi-file'].map(chip => (
            <span key={chip} style={{
              fontFamily: 'monospace', fontSize: 9,
              color: isLight ? '#15803d' : 'rgba(74,222,128,0.7)',
              border: `1px solid ${ga(isLight ? 0.25 : 0.2)}`,
              borderRadius: 6, padding: '3px 10px',
              background: ga(isLight ? 0.06 : 0.05),
              letterSpacing: 0.5,
            }}>
              {chip}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

export function MobileChatPanel() {
  // Scoped selectors — bare useAppStore() re-rendered the whole tree on every
  // agentTasks poll (2.5s). Do not subscribe to agent task arrays here;
  // AgentMissionControl owns that slice. Setters are stable.
  const activeSessions = useAppStore(s => s.activeSessions)
  const setActiveSession = useAppStore(s => s.setActiveSession)
  const messages = useAppStore(s => s.messages)
  const addMessage = useAppStore(s => s.addMessage)
  const setMessages = useAppStore(s => s.setMessages)
  const setSessions = useAppStore(s => s.setSessions)
  const settings = useAppStore(s => s.settings)
  const sessionFiles = useAppStore(s => s.sessionFiles)
  const setSessionFiles = useAppStore(s => s.setSessionFiles)
  const setAgentTasks = useAppStore(s => s.setAgentTasks)
  const updateAgentTask = useAppStore(s => s.updateAgentTask)
  const clearAgentTasks = useAppStore(s => s.clearAgentTasks)
  const setTaskRunId = useAppStore(s => s.setTaskRunId)
  const setTaskPreamble = useAppStore(s => s.setTaskPreamble)
  const setAgentPhase = useAppStore(s => s.setAgentPhase)

  // Keep the task list in sync with the DB-backed source of truth while a run
  // is active (SSE is the instant channel; polling reconciles after drops).
  useTaskPolling(activeSessions)

  const [input, setInput]               = useState('')
  const [isStreaming, setIsStreaming]    = useState(false)
  const [streamProgress, setProgress]   = useState('')
  const [streamingMsg, setStreamingMsg] = useState('')
  const [isBuildingEdit, setBuildEdit]  = useState(false)
  const [isCompacting, setIsCompacting] = useState(false)
  const [composeOpen, setComposeOpen]   = useState(false)
  const [error, setError]               = useState<string | null>(null)

  // Progress steps for finalize/_steps only — no live React state (StreamingBubble
  // does not paint a step history during stream).
  const progressHistoryRef = useRef<string[]>([])
  const ctrlRef            = useRef<AbortController | null>(null)
  // Bridge refs so a manual Stop (outside the streaming closure) can still see
  // what was accumulated and save it instead of discarding it. Long-term fix:
  // nothing streamed is ever silently dropped on user-initiated stop.
  const accumulatedRef      = useRef('')
  const gotResultRef        = useRef(false)
  const streamingSessionIdRef = useRef<string | null>(null)
  // v1.4: holds the planned run while the planning stream closes, so the
  // per-task execution queue can start once /smart-stream returns.
  const pendingRunRef      = useRef<{ runId: string; tasks: any[] } | null>(null)
  const bottomRef          = useRef<HTMLDivElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const userScrolledUpRef  = useRef(false)
  const tokenRafRef        = useRef(0)
  const textareaRef        = useRef<HTMLTextAreaElement>(null)
  const thinkingTextRef    = useRef('')
  const creditPausePollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const fileRequestInputRef = useRef<HTMLInputElement>(null)
  const fileRequestRespondRef = useRef<((resp: { filename?: string; content?: string; action?: 'skip' }) => boolean) | null>(null)

  // Mode + Research — same localStorage keys as desktop ChatPanel.
  const [chatMode, setChatMode] = useState<ChatMode>(readChatMode)
  const [modeSheetOpen, setModeSheetOpen] = useState(false)
  const [webResearchEnabled, setWebResearchEnabled] = useState(readWebResearch)
  const webResearchEnabledRef = useRef(webResearchEnabled)
  webResearchEnabledRef.current = webResearchEnabled

  const [thinkingText, setThinkingText] = useState('')
  const [isThinking, setIsThinking] = useState(false)

  const [resumableRun, setResumableRun] = useState<{ sid: string; runId: string; tasks: any[] } | null>(null)
  const [creditPause, setCreditPause] = useState<{
    sid: string
    pauseId: string
    remainingCount: number
    completedEditCount: number
    heldWriteCount: number
    message: string
    creditsOk: boolean
    probing: boolean
  } | null>(null)
  const [fileRequest, setFileRequest] = useState<{
    sessionId: string
    filename: string
    message: string
    retry?: boolean
  } | null>(null)
  const [fileRequestBusy, setFileRequestBusy] = useState(false)

  const offline = isOfflineSettings(settings)
  const availableModes: ChatMode[] = offline ? ['edit', 'ask'] : CHAT_MODES
  const effectiveMode = degradeModeForOffline(chatMode, offline)
  const researchAvailable = webResearchAvailableFor(settings, offline)
  const selectChatMode = (m: ChatMode) => {
    setChatMode(m)
    writeChatMode(m)
  }
  const toggleWebResearch = () => {
    setWebResearchEnabled(prev => {
      const next = !prev
      writeWebResearch(next)
      return next
    })
  }
  const fileInputRef       = useRef<HTMLInputElement>(null)

  // Smart auto-scroll: same 80px sticky-bottom gate as desktop ChatPanel.
  // While tokens stream, use instant scroll (smooth per-token fights iOS).
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
    if (userScrolledUpRef.current) return
    if (streamingMsg) {
      const el = scrollContainerRef.current
      if (el) el.scrollTop = el.scrollHeight
      else bottomRef.current?.scrollIntoView({ behavior: 'auto' })
      return
    }
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingMsg])

  // Load session files when active session changes
  useEffect(() => {
    setResumableRun(null)
    setCreditPause(null)
    setFileRequest(null)
    setFileRequestBusy(false)
    fileRequestRespondRef.current = null
    if (creditPausePollRef.current) {
      clearInterval(creditPausePollRef.current)
      creditPausePollRef.current = null
    }
    if (activeSessions) {
      api.sessionFiles.list(activeSessions).then(setSessionFiles).catch(() => {})
      // Rehydrate any active Anthropic credit-pause (same API as desktop).
      api.chat.getCreditPause(activeSessions)
        .then((resp) => {
          if (!resp?.active || !resp.pause?.pause_id) return
          if (useAppStore.getState().activeSessions !== activeSessions) return
          setCreditPause({
            sid: activeSessions,
            pauseId: resp.pause.pause_id,
            remainingCount: resp.pause.remaining_count || 0,
            completedEditCount: resp.pause.completed_edit_count || 0,
            heldWriteCount: resp.pause.held_write_count || 0,
            message: resp.message || resp.pause.message || 'Anthropic credits exhausted — progress saved.',
            creditsOk: !!resp.credits_ok,
            probing: false,
          })
        })
        .catch(() => {})
      // Reconcile agentic tasks: only show ACTIVE runs (desktop parity).
      clearAgentTasks()
      api.tasks.list(activeSessions)
        .then((rows: any[]) => {
          if (!Array.isArray(rows) || rows.length === 0) return
          const latestRun = rows[0]?.run_id
          const forRun = rows.filter(r => r.run_id === latestRun)
          const TERMINAL = ['done', 'blocked', 'cancelled', 'error']
          const allTerminal = forRun.every(r => TERMINAL.includes(r.status))
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

  // Poll Anthropic until credits restore (same 15s cadence as desktop).
  useEffect(() => {
    if (creditPausePollRef.current) {
      clearInterval(creditPausePollRef.current)
      creditPausePollRef.current = null
    }
    if (!creditPause || creditPause.creditsOk) return
    const pauseId = creditPause.pauseId
    const sid = creditPause.sid
    const tick = () => {
      setCreditPause(prev => (prev && prev.pauseId === pauseId ? { ...prev, probing: true } : prev))
      api.chat.probeCreditPause(pauseId)
        .then((resp) => {
          if (useAppStore.getState().activeSessions !== sid) return
          setCreditPause(prev => {
            if (!prev || prev.pauseId !== pauseId) return prev
            return { ...prev, creditsOk: !!resp.credits_ok, probing: false }
          })
        })
        .catch(() => {
          setCreditPause(prev => (prev && prev.pauseId === pauseId ? { ...prev, probing: false } : prev))
        })
    }
    tick()
    creditPausePollRef.current = setInterval(tick, 15000)
    return () => {
      if (creditPausePollRef.current) {
        clearInterval(creditPausePollRef.current)
        creditPausePollRef.current = null
      }
    }
  }, [creditPause?.pauseId, creditPause?.creditsOk, creditPause?.sid])

  const stopStream = useCallback(() => {
    ctrlRef.current?.abort()
    ctrlRef.current = null
    if (tokenRafRef.current) {
      cancelAnimationFrame(tokenRafRef.current)
      tokenRafRef.current = 0
    }
    setIsStreaming(false)
    setProgress('')
    setStreamingMsg('')
    setBuildEdit(false)
    setIsThinking(false)
    progressHistoryRef.current = []
  }, [])

  // Manual Stop tap: preserve whatever was streamed so far as a real message
  // instead of discarding it. Long-term fix: nothing streamed is ever silently
  // dropped on user-initiated stop.
  const handleStopClick = useCallback(() => {
    if (!gotResultRef.current && (accumulatedRef.current.trim() || thinkingTextRef.current.trim()) && streamingSessionIdRef.current) {
      addMessage({
        id: Date.now().toString() + '_ai_aborted',
        session_id: streamingSessionIdRef.current,
        role: 'assistant',
        content: accumulatedRef.current.trim(),
        created_at: new Date().toISOString(),
        _steps: [...progressHistoryRef.current],
        _thinking: thinkingTextRef.current || undefined,
        _aborted: true,
      })
      gotResultRef.current = true
    }
    thinkingTextRef.current = ''
    setThinkingText('')
    stopStream()
  }, [addMessage, stopStream])

  const ensureSession = useCallback(async (): Promise<string> => {
    if (activeSessions) return activeSessions
    const s = await api.chat.createSession({ title: 'New Chat' })
    const updated = await api.chat.getSessions()
    setSessions(updated)
    setActiveSession(s.id)
    return s.id
  }, [activeSessions, setSessions, setActiveSession])

  const handleSend = useCallback(async () => {
    if (!input.trim() || isStreaming) return
    if (!settings?.openai_api_key_set && !(settings as any)?.anthropic_api_key_set) {
      setError('Add an API key in Settings first.')
      return
    }
    setError(null)
    const text = input.trim()
    setInput('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

    const sessionId = await ensureSession()
    const isFirst   = messages.length === 0

    const autoName = () => {
      const title = text.slice(0, 55) || 'New Chat'
      api.chat.renameSession(sessionId, title)
        .then(() => api.chat.getSessions().then(setSessions).catch(() => {}))
        .catch(() => {})
    }

    addMessage({
      id: Date.now().toString(),
      session_id: sessionId,
      role: 'user',
      content: text,
      created_at: new Date().toISOString(),
    })

    setIsStreaming(true)
    setProgress('Thinking...')
    setStreamingMsg('')
    setBuildEdit(false)
    setResumableRun(null)
    setThinkingText('')
    thinkingTextRef.current = ''
    setIsThinking(false)
    progressHistoryRef.current = ['Thinking...']
    streamingSessionIdRef.current = sessionId
    accumulatedRef.current = ''
    gotResultRef.current = false
    if (tokenRafRef.current) {
      cancelAnimationFrame(tokenRafRef.current)
      tokenRafRef.current = 0
    }

    const _rawMode = readChatMode()
    const _sendMode = degradeModeForOffline(_rawMode, isOfflineSettings(settings))
    const _sendWebResearch = webResearchAvailableFor(settings, isOfflineSettings(settings))
      && webResearchEnabledRef.current

    let accumulated  = ''
    let gotResult    = false

    // ── v1.4 per-task execution ───────────────────────────────────────────
    // /smart-stream now ends right after planning. Each task then runs in its
    // own short-lived SSE stream, sequentially, so no single connection can
    // hit the proxy/process timeout that previously killed long runs.
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
      stopStream()
      setBuildEdit(false)
      setAgentPhase('complete')
      if (isFirst) autoName()
      else api.chat.getSessions().then(setSessions).catch(() => {})
      api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
    }

    const runTaskQueue = (sid: string, runId: string, tasks: any[]) => {
      let idx = 0
      const onTaskCreditPaused = (info: any) => {
        if (!info?.pause_id) return
        if (useAppStore.getState().activeSessions !== sid) return
        setCreditPause({
          sid,
          pauseId: info.pause_id,
          remainingCount: info.remaining_count || 0,
          completedEditCount: info.completed_edit_count || 0,
          heldWriteCount: info.held_write_count || 0,
          message: info.message || 'Anthropic credits exhausted — progress saved.',
          creditsOk: false,
          probing: false,
        })
      }
      const runNext = () => {
        if (idx >= tasks.length) { finishTaskRun(); return }
        const t = tasks[idx++]
        let sawTerminal = false
        let streamErr = ''
        const reconcileOnClose = () => {
          if (sawTerminal) return
          api.tasks.list(sid, runId)
            .then((rows: any[]) => {
              const row: any = (rows || []).find((r: any) => r.id === t.id)
              const status = row?.status
              if (status === 'done') {
                updateAgentTask(t.id, { status: 'done', qa_score: row.qa_score, verdict: row.verdict })
                if (row.verdict === 'interrupted_recovered') {
                  api.chat.getMessages(sid).then(saved => {
                    if (useAppStore.getState().activeSessions === sid && saved?.length) {
                      setMessages(saved)
                    }
                  }).catch(() => {})
                  const remaining = tasks.slice(idx)
                  if (remaining.length > 0) {
                    setResumableRun({ sid, runId, tasks: remaining })
                  }
                  setError(
                    streamErr
                    || 'Connection dropped mid-task. Recovered edits are ready to review — apply them, then Resume remaining tasks.',
                  )
                  finishTaskRun()
                  return
                }
                runNext()
              } else if (status === 'blocked' || status === 'cancelled') {
                updateAgentTask(t.id, { status, qa_score: row?.qa_score, verdict: row?.verdict })
                if (status === 'blocked' && row?.verdict === 'credit_paused') {
                  const remaining = tasks.slice(idx)
                  if (remaining.length > 0) setResumableRun({ sid, runId, tasks: remaining })
                }
                finishTaskRun()
              } else if (status === 'pending') {
                updateAgentTask(t.id, { status: 'pending', progress: undefined })
                setResumableRun({ sid, runId, tasks: tasks.slice(idx - 1) })
                setError(streamErr || 'Connection dropped mid-task. The run is paused — tap Resume to continue.')
                finishTaskRun()
              } else {
                setError(streamErr || 'Connection dropped mid-task. Task status is unresolved — reopen the session to check progress.')
                finishTaskRun()
              }
            })
            .catch(() => {
              setError(streamErr || 'Connection dropped and task status could not be verified.')
              finishTaskRun()
            })
        }
        const ctrl = api.stream.executeTask(
          { session_id: sid, run_id: runId, task_id: t.id },
          (progress) => setProgress(progress),
          (result) => addTaskResultCard(result),
          reconcileOnClose,
          (err) => { streamErr = err },
          (event) => {
            if (event.type === 'task_done' || event.type === 'task_blocked' || event.type === 'task_cancelled') {
              sawTerminal = true
            }
            handleTaskEvent(event)
            if (event.type === 'task_done') runNext()
            else if (event.type === 'task_blocked' || event.type === 'task_cancelled') {
              if (event.type === 'task_blocked' && event.verdict === 'credit_paused') {
                const remaining = tasks.slice(idx)
                if (remaining.length > 0) setResumableRun({ sid, runId, tasks: remaining })
              }
              finishTaskRun()
            }
          },
          onTaskCreditPaused,
        )
        ctrlRef.current = ctrl
      }
      runNext()
    }

    const ctrl = api.stream.smart(
      {
        session_id: sessionId,
        message: text,
        file_ids: sessionFiles.map(f => f.id),
        mode: _sendMode,
        force_tasks: _sendMode === 'agent',
        enable_web_research: _sendWebResearch,
      },
      (progress) => {
        setProgress(progress)
        // Ref-only: live StreamingBubble does not paint step history.
        if (progressHistoryRef.current[progressHistoryRef.current.length - 1] !== progress) {
          progressHistoryRef.current = [...progressHistoryRef.current, progress]
        }
      },
      (token) => {
        accumulated += token
        accumulatedRef.current = accumulated
        // ≤1 React update per animation frame — do not throttle in client.ts
        if (!tokenRafRef.current) {
          tokenRafRef.current = requestAnimationFrame(() => {
            tokenRafRef.current = 0
            setStreamingMsg(accumulatedRef.current)
          })
        }
      },
      (result) => {
        gotResult = true
        gotResultRef.current = true
        const _steps = [...progressHistoryRef.current]
        const _thinking = thinkingTextRef.current
        const naturalText = result.natural_text || accumulated
        stopStream()
        setBuildEdit(false)
        thinkingTextRef.current = ''
        setThinkingText('')
        addMessage({
          id: Date.now().toString() + '_ai',
          session_id: sessionId,
          role: 'assistant',
          message_type: 'natural_result',
          surgical_data: JSON.stringify(result),
          content: naturalText.trim(),
          created_at: new Date().toISOString(),
          _steps,
          _thinking,
        })
        if (isFirst) autoName()
        else api.chat.getSessions().then(setSessions).catch(() => {})
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
      },
      (fullText) => {
        // Planning stream closed — if a task run was planned, execute tasks
        // one at a time (each its own SSE stream) instead of finishing here.
        if (pendingRunRef.current) {
          const run = pendingRunRef.current
          pendingRunRef.current = null
          runTaskQueue(sessionId, run.runId, run.tasks)
          return
        }
        if (gotResult) return
        gotResultRef.current = true
        const _steps = [...progressHistoryRef.current]
        const _thinking = thinkingTextRef.current
        stopStream()
        thinkingTextRef.current = ''
        setThinkingText('')
        if (fullText.trim()) {
          addMessage({
            id: Date.now().toString() + '_ai',
            session_id: sessionId,
            role: 'assistant',
            content: fullText,
            created_at: new Date().toISOString(),
            _steps,
            _thinking,
          })
        }
        if (isFirst) autoName()
        else api.chat.getSessions().then(setSessions).catch(() => {})
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
      },
      (err) => {
        // Preserve whatever was streamed before the connection error — same
        // long-term fix as manual stop: nothing streamed is silently dropped.
        if ((accumulated.trim() || thinkingTextRef.current.trim()) && !gotResult) {
          addMessage({
            id: Date.now().toString() + '_ai_err',
            session_id: sessionId,
            role: 'assistant',
            content: accumulated.trim(),
            created_at: new Date().toISOString(),
            _steps: [...progressHistoryRef.current],
            _thinking: thinkingTextRef.current || undefined,
          })
          gotResult = true
          gotResultRef.current = true
        }
        thinkingTextRef.current = ''
        setThinkingText('')
        setError(err); stopStream(); setBuildEdit(false)
      },
      // onThinking — do not wipe on every start (multi-round within one turn).
      (thinkToken, phase) => {
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
      // onCompacting
      (phase, info) => {
        if (phase === 'start') {
          setIsCompacting(true)
          setProgress('Compacting conversation history...')
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
      () => setBuildEdit(true),
      () => setBuildEdit(false),
      // onTask — agentic task lifecycle (instant channel; polling reconciles)
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
      },
      // onFileNeeded
      (info, respond) => {
        if (useAppStore.getState().activeSessions !== sessionId) {
          respond({ action: 'skip' })
          return
        }
        fileRequestRespondRef.current = respond
        setFileRequestBusy(false)
        setFileRequest({ sessionId, filename: info.filename, message: info.message, retry: info.retry })
      },
      // onFileCleared
      () => {
        setFileRequest(null)
        setFileRequestBusy(false)
        fileRequestRespondRef.current = null
      },
      // onWebSearch — light progress only (full sources UI is PR2)
      (event) => {
        if (event.phase === 'start' || event.phase === 'query') {
          setProgress(event.phase === 'query' && 'query' in event ? `Searching: ${event.query}` : 'Searching the web…')
        }
      },
      undefined, // onDoneSources — deferred to PR2 citations strip
      // onCreditPaused
      (info) => {
        if (useAppStore.getState().activeSessions !== sessionId) return
        const pauseId = info?.pause_id
        if (!pauseId) return
        setCreditPause({
          sid: sessionId,
          pauseId,
          remainingCount: info.remaining_count || 0,
          completedEditCount: info.completed_edit_count || 0,
          heldWriteCount: info.held_write_count || 0,
          message: info.message || 'Anthropic credits exhausted — progress saved.',
          creditsOk: false,
          probing: false,
        })
      },
    )
    ctrlRef.current = ctrl
  }, [input, isStreaming, settings, ensureSession, messages.length, sessionFiles,
      addMessage, setSessions, stopStream, updateAgentTask, setAgentPhase, setTaskRunId,
      setTaskPreamble, setAgentTasks, setSessionFiles])

  const resumeInterruptedRun = useCallback(() => {
    if (!resumableRun || isStreaming) return
    if (useAppStore.getState().activeSessions !== resumableRun.sid) { setResumableRun(null); return }
    const { sid, runId, tasks } = resumableRun
    setResumableRun(null)
    setError(null)
    setIsStreaming(true)
    setProgress('Resuming tasks…')
    setAgentPhase('executing')
    let idx = 0
    const finish = () => {
      stopStream()
      setAgentPhase('complete')
      api.sessionFiles.list(sid).then(setSessionFiles).catch(() => {})
      api.chat.getSessions().then(setSessions).catch(() => {})
    }
    const runNext = () => {
      if (idx >= tasks.length) { finish(); return }
      const t = tasks[idx++]
      let sawTerminal = false
      let streamErr = ''
      const reconcileOnClose = () => {
        if (sawTerminal) return
        api.tasks.list(sid, runId)
          .then((rows: any[]) => {
            const row: any = (rows || []).find((r: any) => r.id === t.id)
            const status = row?.status
            if (status === 'done') {
              updateAgentTask(t.id, { status: 'done', qa_score: row.qa_score, verdict: row.verdict })
              if (row.verdict === 'interrupted_recovered') {
                api.chat.getMessages(sid).then(saved => {
                  if (useAppStore.getState().activeSessions === sid && saved?.length) {
                    setMessages(saved)
                  }
                }).catch(() => {})
                const remaining = tasks.slice(idx)
                if (remaining.length > 0) {
                  setResumableRun({ sid, runId, tasks: remaining })
                }
                setError(
                  streamErr
                  || 'Connection dropped mid-task. Recovered edits are ready to review — apply them, then Resume remaining tasks.',
                )
                finish()
                return
              }
              runNext()
            } else if (status === 'blocked' || status === 'cancelled') {
              updateAgentTask(t.id, { status, qa_score: row?.qa_score, verdict: row?.verdict })
              if (status === 'blocked' && row?.verdict === 'credit_paused') {
                const remaining = tasks.slice(idx)
                if (remaining.length > 0) setResumableRun({ sid, runId, tasks: remaining })
              }
              finish()
            } else if (status === 'pending') {
              updateAgentTask(t.id, { status: 'pending', progress: undefined })
              setResumableRun({ sid, runId, tasks: tasks.slice(idx - 1) })
              setError(streamErr || 'Connection dropped mid-task. The run is paused — tap Resume to continue.')
              finish()
            } else {
              setError(streamErr || 'Connection dropped mid-task. Task status is unresolved — reopen the session to check progress.')
              finish()
            }
          })
          .catch(() => {
            setError(streamErr || 'Connection dropped and task status could not be verified.')
            finish()
          })
      }
      const ctrl = api.stream.executeTask(
        { session_id: sid, run_id: runId, task_id: t.id },
        (progress) => setProgress(progress),
        (result) => {
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
          })
          api.sessionFiles.list(sid).then(setSessionFiles).catch(() => {})
        },
        reconcileOnClose,
        (err) => { streamErr = err },
        (event) => {
          if (event.type === 'task_done' || event.type === 'task_blocked' || event.type === 'task_cancelled') {
            sawTerminal = true
          }
          if (event.type === 'task_start') updateAgentTask(event.id, { status: 'running', progress: undefined })
          else if (event.type === 'task_progress') updateAgentTask(event.id, { progress: event.content })
          else if (event.type === 'task_done') {
            updateAgentTask(event.id, { status: 'done', qa_score: event.qa_score, verdict: event.verdict })
            runNext()
          } else if (event.type === 'task_blocked' || event.type === 'task_cancelled') {
            updateAgentTask(event.id, { status: event.type === 'task_blocked' ? 'blocked' : 'cancelled', qa_score: event.qa_score, verdict: event.verdict })
            if (event.type === 'task_blocked' && event.verdict === 'credit_paused') {
              const remaining = tasks.slice(idx)
              if (remaining.length > 0) setResumableRun({ sid, runId, tasks: remaining })
            }
            finish()
          }
        },
        (info) => {
          if (!info?.pause_id) return
          if (useAppStore.getState().activeSessions !== sid) return
          setCreditPause({
            sid,
            pauseId: info.pause_id,
            remainingCount: info.remaining_count || 0,
            completedEditCount: info.completed_edit_count || 0,
            heldWriteCount: info.held_write_count || 0,
            message: info.message || 'Anthropic credits exhausted — progress saved.',
            creditsOk: false,
            probing: false,
          })
        },
      )
      ctrlRef.current = ctrl
    }
    runNext()
  }, [resumableRun, isStreaming, stopStream, setAgentPhase, setSessionFiles, setSessions, addMessage, updateAgentTask, setMessages])

  const dismissCreditPause = useCallback(() => {
    if (!creditPause) return
    api.chat.dismissCreditPause(creditPause.pauseId).catch(() => {})
    setCreditPause(null)
  }, [creditPause])

  const resumeCreditPause = useCallback(() => {
    if (!creditPause || isStreaming || !creditPause.creditsOk) return
    const { sid, pauseId } = creditPause
    if (useAppStore.getState().activeSessions !== sid) { setCreditPause(null); return }
    setError(null)
    setIsStreaming(true)
    setProgress('Resuming saved edits…')
    setStreamingMsg('')
    progressHistoryRef.current = ['Resuming saved edits…']
    thinkingTextRef.current = ''
    setThinkingText('')
    setIsThinking(false)
    let gotResult = false
    let accumulated = ''
    const ctrl = api.chat.resumeCreditPause(
      pauseId,
      (msg) => {
        setProgress(msg)
        if (progressHistoryRef.current[progressHistoryRef.current.length - 1] !== msg) {
          progressHistoryRef.current = [...progressHistoryRef.current, msg]
        }
      },
      (token) => {
        accumulated += token
        accumulatedRef.current = accumulated
        if (!tokenRafRef.current) {
          tokenRafRef.current = requestAnimationFrame(() => {
            tokenRafRef.current = 0
            setStreamingMsg(accumulatedRef.current)
          })
        }
      },
      (result) => {
        gotResult = true
        if (result?.credit_paused) {
          setCreditPause(prev => prev ? { ...prev, creditsOk: false } : prev)
          return
        }
        const naturalText = (result.natural_text || '')
          .replace(/<new_file>[\s\S]*?<\/new_file>/g, '')
          .replace(/<new_file>[\s\S]*$/, '')
          .trim()
        addMessage({
          id: Date.now().toString() + '_credit_resume',
          session_id: sid,
          role: 'assistant',
          message_type: naturalText ? 'natural_result' : 'surgical_result',
          surgical_data: JSON.stringify(result),
          content: naturalText,
          created_at: new Date().toISOString(),
        } as any)
        api.sessionFiles.list(sid).then(setSessionFiles).catch(() => {})
        setCreditPause(null)
      },
      () => {
        stopStream()
        if (!gotResult) {
          api.chat.getMessages(sid).then(saved => {
            if (useAppStore.getState().activeSessions === sid && saved?.length) setMessages(saved)
          }).catch(() => {})
        }
      },
      (err) => { setError(err); stopStream() },
      (info) => {
        if (!info?.pause_id) return
        setCreditPause({
          sid,
          pauseId: info.pause_id,
          remainingCount: info.remaining_count || 0,
          completedEditCount: info.completed_edit_count || 0,
          heldWriteCount: info.held_write_count || 0,
          message: info.message || 'Anthropic credits exhausted — progress saved.',
          creditsOk: false,
          probing: false,
        })
      },
    )
    ctrlRef.current = ctrl
  }, [creditPause, isStreaming, addMessage, setSessionFiles, stopStream])

  const handleFileRequestUpload = () => fileRequestInputRef.current?.click()

  const handleFileRequestFileChosen = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    const respond = fileRequestRespondRef.current
    if (!file || !fileRequest || !respond) return
    const sizeErr = validateFileSize(file.name, file.size)
    if (sizeErr) { setError(sizeErr); return }
    setFileRequestBusy(true)
    try {
      const content = await file.text()
      const sent = respond({ filename: file.name, content })
      if (!sent) {
        setFileRequestBusy(false)
        setFileRequest(null)
        fileRequestRespondRef.current = null
        setError('Connection to the agent was lost — please resend your message.')
      }
    } catch {
      setFileRequestBusy(false)
      setError('Could not read that file — please try again or skip.')
    }
  }

  const handleFileRequestSkip = () => {
    const respond = fileRequestRespondRef.current
    if (!fileRequest || !respond) return
    setFileRequestBusy(true)
    const sent = respond({ action: 'skip' })
    if (!sent) {
      setFileRequestBusy(false)
      setFileRequest(null)
      fileRequestRespondRef.current = null
      setError('Connection to the agent was lost — please resend your message.')
    }
  }
  // File upload
  const handleFileUpload = async (files: FileList | null) => {
    if (!files?.length) return
    const sessionId = await ensureSession()
    for (const file of Array.from(files)) {
      // ── File size validation ─────────────────────────────────────────
      const sizeErr = validateFileSize(file.name, file.size)
      if (sizeErr) { toast.error(sizeErr); continue }

      try {
        const content = await file.text()
        const body = { filename: file.name, content, file_type: 'code' as const }
        const uploaded = await api.sessionFiles.upload(sessionId, body)
        setSessionFiles([...(useAppStore.getState().sessionFiles), uploaded])
        toast.success(`${file.name} uploaded`)
      } catch (e: any) {
        toast.error(`Upload failed: ${file.name}`)
      }
    }
  }

  const removeFile = async (fileId: string) => {
    if (!activeSessions) return
    try {
      await api.sessionFiles.delete(activeSessions, fileId)
      setSessionFiles(sessionFiles.filter(f => f.id !== fileId))
    } catch {}
  }

  const hasFiles    = sessionFiles.length > 0
  const canSend     = input.trim().length > 0 && !isStreaming

  return (
    <div className="flex flex-col h-full bg-base">
      {/* Messages */}
      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto">
        {messages.length === 0 && !isStreaming ? (
          <EmptyHomeScreen />
        ) : (
          <div className="py-2">
            {messages.map((msg, i) => (
              <MessageBubble
                key={msg.id || i}
                msg={msg}
                sessionId={msg.session_id || activeSessions || ''}
                sessionFiles={sessionFiles}
                setSessionFiles={setSessionFiles}
              />
            ))}
            <AgentMissionControl />
            {resumableRun && resumableRun.sid === activeSessions && !isStreaming && (
              <div className="mx-3 mb-2 flex items-center justify-between gap-3 rounded-xl border border-warning/30 bg-warning/10 px-3.5 py-2.5">
                <span className="text-[12px] text-ink leading-snug">
                  This task run was interrupted — {resumableRun.tasks.length} task{resumableRun.tasks.length === 1 ? '' : 's'} remaining.
                </span>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    type="button"
                    onClick={resumeInterruptedRun}
                    className="text-[11px] font-semibold px-2.5 py-1 rounded-lg text-accent bg-accent/10 border border-accent/20"
                  >
                    Resume
                  </button>
                  <button
                    type="button"
                    onClick={() => setResumableRun(null)}
                    className="text-[11px] font-medium px-2 py-1 rounded-lg text-muted"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}
            {creditPause && creditPause.sid === activeSessions && !isStreaming && (
              <div className="mx-3 mb-2 rounded-xl border border-danger/30 bg-danger/10 px-3.5 py-2.5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[12px] font-semibold text-ink">
                      Anthropic credits exhausted — progress saved
                    </div>
                    <p className="mt-1 text-[11px] text-muted leading-snug">
                      {creditPause.remainingCount} plan step{creditPause.remainingCount === 1 ? '' : 's'} waiting
                      {creditPause.completedEditCount > 0 ? ` · ${creditPause.completedEditCount} edit(s) already done` : ''}
                      {creditPause.heldWriteCount > 0 ? ` · ${creditPause.heldWriteCount} Grok write(s) saved` : ''}
                      . Add credits at console.anthropic.com, then Resume.
                      {creditPause.probing ? ' Checking balance…' : creditPause.creditsOk ? ' Credits detected — ready to resume.' : ' Waiting for credits…'}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <button
                      type="button"
                      onClick={resumeCreditPause}
                      disabled={!creditPause.creditsOk || isStreaming}
                      className="text-[11px] font-semibold px-2.5 py-1 rounded-lg text-accent bg-accent/10 border border-accent/20 disabled:opacity-40"
                    >
                      Resume
                    </button>
                    <button
                      type="button"
                      onClick={dismissCreditPause}
                      className="text-[11px] font-medium px-2 py-1 rounded-lg text-muted"
                    >
                      Dismiss
                    </button>
                  </div>
                </div>
              </div>
            )}
            {fileRequest && fileRequest.sessionId === activeSessions && (
              <div className="mx-4 my-3 rounded-xl border border-accent/40 bg-accent/10 px-4 py-3.5">
                <div className="text-[13px] font-semibold text-ink">
                  File needed to continue{' '}
                  <code className="px-1.5 py-0.5 rounded bg-surface/70 border border-border text-[11px] font-mono text-accent">{fileRequest.filename}</code>
                </div>
                <p className="mt-1 text-[12px] text-muted leading-snug">{fileRequest.message}</p>
                <div className="mt-2.5 flex items-center gap-2">
                  <button
                    type="button"
                    onClick={handleFileRequestUpload}
                    disabled={fileRequestBusy}
                    className="text-[12px] font-semibold px-3 py-1.5 rounded-lg text-white bg-accent disabled:opacity-50"
                  >
                    {fileRequestBusy ? 'Sending…' : 'Upload file'}
                  </button>
                  <button
                    type="button"
                    onClick={handleFileRequestSkip}
                    disabled={fileRequestBusy}
                    className="text-[12px] font-medium px-2.5 py-1.5 rounded-lg text-muted disabled:opacity-50"
                  >
                    Skip
                  </button>
                </div>
                <input
                  ref={fileRequestInputRef}
                  type="file"
                  className="hidden"
                  onChange={handleFileRequestFileChosen}
                />
              </div>
            )}
            {isStreaming && (
              <StreamingBubble
                text={streamingMsg}
                progress={streamProgress}
                isBuildingEdit={isBuildingEdit}
                thinkingText={thinkingText}
                isThinking={isThinking}
              />
            )}
            {error && (
              <div className="mx-4 my-2 px-3 py-2.5 bg-danger/10 border border-danger/30 rounded-xl text-xs text-danger flex items-start gap-2">
                <span>⚠️</span>
                <span className="flex-1">{error}</span>
                <button onClick={() => setError(null)} className="text-danger/60 hover:text-danger">✕</button>
              </div>
            )}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Session file drawer — single docked source of truth atop the composer */}
      {activeSessions && hasFiles && (
        <div className="flex-shrink-0 px-3 pt-2 border-t border-border/50">
          <SessionFilesTray
            sessionId={activeSessions}
            sessionFiles={sessionFiles}
            onAddFiles={() => fileInputRef.current?.click()}
            onRemove={removeFile}
          />
        </div>
      )}

      {/* ── Input bar ── */}
      <div className="flex-shrink-0 bg-base px-3 pt-2 pb-3 border-t border-border/60">

        {/* Unified pill — contains textarea + all actions */}
        <div className="flex flex-col bg-surface border border-border/80 rounded-2xl
          overflow-hidden shadow-sm focus-within:border-[rgba(74,222,128,0.35)] transition-colors">

          {/* Textarea row */}
          <textarea
            ref={textareaRef}
            value={input}
            onChange={e => {
              setInput(e.target.value)
              e.target.style.height = 'auto'
              e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px'
            }}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
            }}
            placeholder={isCompacting ? 'Compacting history…' : 'Ready when you are…'}
            rows={1}
            disabled={isCompacting}
            className="w-full resize-none bg-transparent px-4 pt-3 pb-1 text-[15px]
              text-ink placeholder:text-muted/40 focus:outline-none leading-relaxed"
            style={{ minHeight: 44, maxHeight: 160 }}
          />

          {/* Bottom toolbar — left icons | right send */}
          <div className="flex items-center justify-between px-2 pb-2 pt-1">

            {/* Left: attach + voice + expand + session-id chip */}
            <div className="flex items-center gap-0.5">
              {activeSessions && (
                <button
                  onClick={() => { navigator.clipboard.writeText(activeSessions); }}
                  className="h-9 flex items-center px-2 font-mono text-[10px] text-muted/50 hover:text-accent hover:bg-overlay/60 active:bg-overlay rounded-xl transition-colors leading-none"
                  title={`Session ID: ${activeSessions}\nClick to copy`}
                >
                  {activeSessions.slice(0, 8)}
                </button>
              )}
              {/* Attach */}
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={isCompacting}
                className="w-9 h-9 flex items-center justify-center rounded-xl
                  text-muted/60 hover:text-ink hover:bg-overlay/60 active:bg-overlay
                  transition-colors disabled:opacity-40"
                title="Attach file"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
                </svg>
              </button>

              {/* Voice */}
              <VoiceButton
                onTranscript={(text) => setInput(prev => prev ? prev + ' ' + text : text)}
                lastResponse={messages.filter(m => m.role === 'assistant' && m.content).slice(-1)[0]?.content}
                disabled={isStreaming || isCompacting}
                size="compact"
              />

              {/* Mode chip — opens MobileModeSheet (Edit/Ask/Plan/Agent) */}
              <button
                type="button"
                onClick={() => setModeSheetOpen(true)}
                disabled={isStreaming || isCompacting}
                className={`h-9 px-2.5 flex items-center gap-1.5 rounded-xl text-[11px] font-semibold border transition-colors disabled:opacity-40 ${MODE_COLOR[effectiveMode].text} ${MODE_COLOR[effectiveMode].bg} ${MODE_COLOR[effectiveMode].border}`}
                title={`${MODE_META[effectiveMode].label}: ${MODE_META[effectiveMode].desc}`}
              >
                <span className={`w-1.5 h-1.5 rounded-full ${MODE_COLOR[effectiveMode].dot}`} />
                {MODE_META[effectiveMode].label}
              </button>

              {/* Research — Claude-only; same key as desktop sai_web_research_enabled */}
              {researchAvailable && (
                <button
                  type="button"
                  onClick={toggleWebResearch}
                  disabled={isStreaming || isCompacting}
                  className={`h-9 px-2.5 flex items-center rounded-xl text-[11px] font-semibold border transition-colors disabled:opacity-40 ${
                    webResearchEnabled
                      ? 'text-accent bg-accent/15 border-accent/40'
                      : 'text-muted/60 border-transparent hover:bg-overlay/60'
                  }`}
                  title="Research: enable Claude web search for the next message"
                >
                  Research
                </button>
              )}

              {/* Expand to full-screen compose */}
              <button
                onClick={() => setComposeOpen(true)}
                disabled={isCompacting}
                className="w-9 h-9 flex items-center justify-center rounded-xl
                  text-muted/60 hover:text-ink hover:bg-overlay/60 active:bg-overlay
                  transition-colors disabled:opacity-40"
                title="Expand editor"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="15 3 21 3 21 9"/>
                  <polyline points="9 21 3 21 3 15"/>
                  <line x1="21" y1="3" x2="14" y2="10"/>
                  <line x1="3" y1="21" x2="10" y2="14"/>
                </svg>
              </button>
            </div>

            {/* Right: send / stop */}
            {isStreaming ? (
              <button
                onClick={handleStopClick}
                className="w-9 h-9 flex items-center justify-center rounded-xl
                  bg-danger/15 text-danger hover:bg-danger/25 active:scale-95 transition-all"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                  <rect x="4" y="4" width="16" height="16" rx="3"/>
                </svg>
              </button>
            ) : (
              <button
                onClick={handleSend}
                disabled={!canSend}
                className={`w-9 h-9 flex items-center justify-center rounded-xl
                  transition-all active:scale-95
                  ${canSend
                    ? 'bg-[#4ade80] text-white shadow-sm shadow-[rgba(74,222,128,0.2)] hover:bg-[#4ade80]/90'
                    : 'text-muted/30 cursor-not-allowed'
                  }`}
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="12" y1="19" x2="12" y2="5"/>
                  <polyline points="5 12 12 5 19 12"/>
                </svg>
              </button>
            )}
          </div>
        </div>

        {/* Hidden file input */}
        <input ref={fileInputRef} type="file" multiple className="hidden"
          onChange={e => handleFileUpload(e.target.files)} />

        {/* Compose sheet */}
        {composeOpen && (
          <MobileComposeSheet
            value={input}
            onChange={setInput}
            onSend={handleSend}
            onClose={() => setComposeOpen(false)}
            isStreaming={isStreaming}
            disabled={isCompacting}
          />
        )}

        <MobileModeSheet
          open={modeSheetOpen}
          current={effectiveMode}
          available={availableModes}
          onSelect={selectChatMode}
          onClose={() => setModeSheetOpen(false)}
        />
      </div>
    </div>
  )
}
