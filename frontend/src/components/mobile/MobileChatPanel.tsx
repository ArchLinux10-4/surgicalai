/**
 * MobileChatPanel — full-screen chat for mobile.
 * Mirrors ChatPanel.tsx logic exactly — same API calls, same store, same streaming.
 * Render layer is completely separate: no shared JSX with desktop ChatPanel.
 */
import React, { useState, useRef, useEffect, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAppStore } from '../../stores/appStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'
import { MobileDiffCard } from './MobileDiffCard'
import { SessionFilesTray } from '../SessionFilesTray'
import { AgentMissionControl } from '../AgentMissionControl'
import { useTaskPolling } from '../../hooks/useTaskPolling'
import { VoiceButton } from '../VoiceButton'
import { useCodeRain } from '../../hooks/useCodeRain'
import { useThemeStore } from '../../stores/themeStore'
import type { SessionFile, SmartResult } from '../../types'
import { validateFileSize } from '../../utils/fileValidation'

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

// ── Streaming bubble ─────────────────────────────────────────────────────────
function StreamingBubble({ text, progress, isBuildingEdit }: {
  text: string; progress: string; isBuildingEdit: boolean
}) {
  return (
    <div className="flex items-start gap-2.5 px-4 py-3">
      <div className="w-7 h-7 rounded-full bg-[rgba(74,222,128,0.12)] border border-[rgba(74,222,128,0.25)] flex items-center justify-center flex-shrink-0 mt-0.5">
        <span className="text-[#4ade80] text-[10px] font-bold">AI</span>
      </div>
      <div className="flex-1 min-w-0">
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
          <div className="text-sm text-ink leading-relaxed prose-mobile">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
          </div>
        )}
        {!text && !isBuildingEdit && (
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
function MessageBubble({ msg, sessionId, sessionFiles, setSessionFiles }: {
  msg: any; sessionId: string
  sessionFiles: SessionFile[]; setSessionFiles: (f: SessionFile[]) => void
}) {
  const isUser = msg.role === 'user'
  const isResult = msg.message_type === 'natural_result' || msg.message_type === 'surgical_result'

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

        {/* Natural text */}
        {msg.content && (
          <div className="text-sm text-ink leading-relaxed mb-2">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
          </div>
        )}

        {/* Diff card */}
        {result && (
          <MobileDiffCard
            result={result}
            sessionId={sessionId}
            sessionFiles={sessionFiles}
            setSessionFiles={setSessionFiles}
          />
        )}
      </div>
    </div>
  )
}

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
  const {
    activeSessions, setActiveSession, messages, addMessage, setMessages,
    sessions, setSessions, settings,
    sessionFiles, setSessionFiles,
    setAgentTasks, updateAgentTask, clearAgentTasks, setTaskRunId, setTaskPreamble, setAgentPhase,
  } = useAppStore()

  // Keep the task list in sync with the DB-backed source of truth while a run
  // is active (SSE is the instant channel; polling reconciles after drops).
  useTaskPolling(activeSessions)

  const [input, setInput]               = useState('')
  const [isStreaming, setIsStreaming]    = useState(false)
  const [streamProgress, setProgress]   = useState('')
  const [streamingMsg, setStreamingMsg] = useState('')
  const [progressHistory, setProgHist]  = useState<string[]>([])
  const [isBuildingEdit, setBuildEdit]  = useState(false)
  const [isCompacting, setIsCompacting] = useState(false)
  const [composeOpen, setComposeOpen]   = useState(false)
  const [error, setError]               = useState<string | null>(null)

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
  const textareaRef        = useRef<HTMLTextAreaElement>(null)

  // ── Agent Mode toggle (multi-agent task breakdown) — shares the desktop
  // localStorage key; read at send time so the payload is never stale.
  const agentModeOn = () => {
    try { return localStorage.getItem('sai_agent_mode') === '1' } catch { return false }
  }
  const [agentMode, setAgentMode] = useState(agentModeOn)
  const toggleAgentMode = () => setAgentMode(prev => {
    const next = !prev
    try { localStorage.setItem('sai_agent_mode', next ? '1' : '0') } catch { /* storage blocked — session-only toggle */ }
    return next
  })
  const fileInputRef       = useRef<HTMLInputElement>(null)

  // Scroll to bottom on new messages/streaming
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingMsg])

  // Load session files when active session changes
  useEffect(() => {
    if (activeSessions) {
      api.sessionFiles.list(activeSessions).then(setSessionFiles).catch(() => {})
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

  const stopStream = useCallback(() => {
    ctrlRef.current?.abort()
    ctrlRef.current = null
    setIsStreaming(false)
    setProgress('')
    setStreamingMsg('')
    setProgHist([])
    setBuildEdit(false)
    progressHistoryRef.current = []
  }, [])

  // Manual Stop tap: preserve whatever was streamed so far as a real message
  // instead of discarding it. Long-term fix: nothing streamed is ever silently
  // dropped on user-initiated stop.
  const handleStopClick = useCallback(() => {
    if (!gotResultRef.current && accumulatedRef.current.trim() && streamingSessionIdRef.current) {
      addMessage({
        id: Date.now().toString() + '_ai_aborted',
        session_id: streamingSessionIdRef.current,
        role: 'assistant',
        content: accumulatedRef.current.trim(),
        created_at: new Date().toISOString(),
        _steps: [...progressHistoryRef.current],
        _aborted: true,
      })
      gotResultRef.current = true
    }
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
    setProgHist(['Thinking...'])
    setBuildEdit(false)
    progressHistoryRef.current = ['Thinking...']
    streamingSessionIdRef.current = sessionId
    accumulatedRef.current = ''
    gotResultRef.current = false

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
      const runNext = () => {
        if (idx >= tasks.length) { finishTaskRun(); return }
        const t = tasks[idx++]
        const ctrl = api.stream.executeTask(
          { session_id: sid, run_id: runId, task_id: t.id },
          (progress) => setProgress(progress),
          (result) => addTaskResultCard(result),
          () => {},  // per-task stream closed; queue advances on task_done
          (err) => { setError(err); finishTaskRun() },
          (event) => {
            handleTaskEvent(event)
            if (event.type === 'task_done') runNext()
            else if (event.type === 'task_blocked' || event.type === 'task_cancelled') finishTaskRun()
          },
        )
        ctrlRef.current = ctrl
      }
      runNext()
    }

    const ctrl = api.stream.smart(
      { session_id: sessionId, message: text, file_ids: sessionFiles.map(f => f.id), force_tasks: agentModeOn() },
      (progress) => {
        setProgress(progress)
        setProgHist(prev => {
          if (prev[prev.length - 1] !== progress) {
            const next = [...prev, progress]
            progressHistoryRef.current = next
            return next
          }
          return prev
        })
      },
      (token) => { accumulated += token; accumulatedRef.current = accumulated; setStreamingMsg(accumulated) },
      (result) => {
        gotResult = true
        gotResultRef.current = true
        const _steps = [...progressHistoryRef.current]
        const naturalText = result.natural_text || accumulated
        stopStream()
        setBuildEdit(false)
        addMessage({
          id: Date.now().toString() + '_ai',
          session_id: sessionId,
          role: 'assistant',
          message_type: 'natural_result',
          surgical_data: JSON.stringify(result),
          content: naturalText.trim(),
          created_at: new Date().toISOString(),
          _steps,
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
        stopStream()
        if (fullText.trim()) {
          addMessage({
            id: Date.now().toString() + '_ai',
            session_id: sessionId,
            role: 'assistant',
            content: fullText,
            created_at: new Date().toISOString(),
            _steps,
          })
        }
        if (isFirst) autoName()
        else api.chat.getSessions().then(setSessions).catch(() => {})
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
      },
      (err) => {
        // Preserve whatever was streamed before the connection error — same
        // long-term fix as manual stop: nothing streamed is silently dropped.
        if (accumulated.trim() && !gotResult) {
          addMessage({
            id: Date.now().toString() + '_ai_err',
            session_id: sessionId,
            role: 'assistant',
            content: accumulated.trim(),
            created_at: new Date().toISOString(),
            _steps: [...progressHistoryRef.current],
          })
          gotResult = true
          gotResultRef.current = true
        }
        setError(err); stopStream(); setBuildEdit(false)
      },
      undefined, // onThinking — omitted on mobile for simplicity
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
    )
    ctrlRef.current = ctrl
  }, [input, isStreaming, settings, ensureSession, messages.length, sessionFiles,
      addMessage, setSessions, stopStream])

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
      <div className="flex-1 overflow-y-auto">
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
            {isStreaming && (
              <StreamingBubble
                text={streamingMsg}
                progress={streamProgress}
                isBuildingEdit={isBuildingEdit}
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

              {/* Agent Mode toggle — forces multi-agent task breakdown */}
              <button
                onClick={toggleAgentMode}
                disabled={isStreaming || isCompacting}
                className={`w-9 h-9 flex items-center justify-center rounded-xl transition-colors disabled:opacity-40 ${
                  agentMode
                    ? 'text-accent bg-accent/15 active:bg-accent/25'
                    : 'text-muted/60 hover:text-ink hover:bg-overlay/60 active:bg-overlay'
                }`}
                title="Agent Mode: breaks your request into tasks and runs them with a team of AI agents (architect, surgeon, QA per task + integration review). Requires a Claude model."
              >
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="9" y="2" width="6" height="5" rx="1"/>
                  <rect x="2" y="17" width="6" height="5" rx="1"/>
                  <rect x="16" y="17" width="6" height="5" rx="1"/>
                  <path d="M12 7v4M12 11H5v6M12 11h7v6"/>
                </svg>
              </button>

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
      </div>
    </div>
  )
}
