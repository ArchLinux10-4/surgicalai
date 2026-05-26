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
import { VoiceButton } from '../VoiceButton'
import { useCodeRain } from '../../hooks/useCodeRain'
import type { SessionFile, SmartResult } from '../../types'

// ── Thin progress steps component ───────────────────────────────────────────
function ProgressSteps({ steps }: { steps: string[] }) {
  const visible = steps.filter(s => s && s !== 'Thinking...')
  if (!visible.length) return null
  return (
    <div className="flex flex-col gap-0.5 mb-2">
      {visible.map((s, i) => (
        <div key={i} className="flex items-center gap-1.5 text-[11px] text-muted/70">
          <span className="text-emerald-400 text-[10px]">✓</span>
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
          <div className="flex items-center gap-1.5 text-[11px] text-amber-400/80 mb-1.5 px-2 py-1 bg-amber-500/10 rounded-lg border border-amber-500/20">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
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

// ── Message bubble ────────────────────────────────────────────────────────────
function MessageBubble({ msg, sessionId, sessionFiles, setSessionFiles }: {
  msg: any; sessionId: string
  sessionFiles: SessionFile[]; setSessionFiles: (f: SessionFile[]) => void
}) {
  const isUser = msg.role === 'user'
  const isResult = msg.message_type === 'natural_result' || msg.message_type === 'surgical_result'

  if (msg.message_type === 'compact_marker') {
    return (
      <div className="flex justify-center py-2">
        <span className="text-[10px] text-muted/50 bg-surface/60 px-3 py-1 rounded-full border border-border/40">
          📦 Earlier conversation compacted
        </span>
      </div>
    )
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
      <button onClick={onRemove} className="flex-shrink-0 text-muted/50 hover:text-red-400 text-[10px] ml-0.5">✕</button>
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
        placeholder="Ask about your code, describe changes, or paste requirements..."
        className="flex-1 w-full resize-none bg-transparent px-5 py-4 text-base text-ink
          placeholder:text-muted/40 focus:outline-none leading-relaxed"
      />

      {/* Character count + hint */}
      <div className="flex-shrink-0 flex items-center justify-between px-5 py-3
        border-t border-border/50 bg-surface/50"
        style={{ paddingBottom: 'max(env(safe-area-inset-bottom, 0px), 12px)' }}>
        <span className="text-[11px] text-muted/40">Shift+Enter for new line</span>
        <span className={`text-[11px] tabular-nums ${value.length > 2000 ? 'text-amber-400' : 'text-muted/40'}`}>
          {value.length > 0 ? `${value.length.toLocaleString()} chars` : ''}
        </span>
      </div>
    </div>
  )
}

// ── Animated home screen ─────────────────────────────────────────────────────
function EmptyHomeScreen() {
  const canvasRef = useCodeRain(true)

  return (
    <div className="relative flex flex-col items-center justify-center h-full overflow-hidden"
      style={{ background: '#0a0a0a' }}>

      {/* Code rain — fades to near-invisible after 3s */}
      <canvas ref={canvasRef} className="absolute inset-0 w-full h-full"
        style={{
          animation: 'sai-rain-fade 1.5s ease-in-out 3s forwards',
          opacity: 0.85,
        }} />

      {/* Center glow — also dims */}
      <div className="absolute inset-0 pointer-events-none"
        style={{
          background: 'radial-gradient(ellipse at center, rgba(74,222,128,0.07) 0%, transparent 70%)',
          animation: 'sai-glow-fade 1.5s ease-in-out 3s forwards',
        }} />

      {/* Content */}
      <div className="relative z-10 flex flex-col items-center gap-5 px-8 text-center">

        {/* Precision Scope icon — fades out completely after 3s */}
        <div className="relative" style={{
          width: 88, height: 88,
          animation: 'sai-icon-out 1.2s ease-in-out 3s forwards',
        }}>
          <div className="absolute inset-0 rounded-full"
            style={{ background: 'radial-gradient(circle, rgba(74,222,128,0.12) 0%, transparent 70%)' }} />
          <div className="w-full h-full rounded-[22px] flex items-center justify-center" style={{
            background: 'rgba(74,222,128,0.07)',
            border: '1px solid rgba(74,222,128,0.28)',
            boxShadow: '0 0 32px rgba(74,222,128,0.1)',
          }}>
            <svg width="56" height="56" viewBox="0 0 64 64" style={{ overflow: 'visible' }}>
              <style>{`
                @keyframes sai-spin    { from{transform:rotate(0deg)}  to{transform:rotate(360deg)}  }
                @keyframes sai-rspin   { from{transform:rotate(0deg)}  to{transform:rotate(-360deg)} }
                @keyframes sai-blink   { 0%,100%{opacity:1} 50%{opacity:0.15} }
                @keyframes sai-scan    { 0%,100%{transform:translateY(-22px);opacity:0} 20%{opacity:0.7} 80%{opacity:0.7} 100%{transform:translateY(22px);opacity:0} }
                @keyframes sai-icon-out  { to { opacity: 0; transform: scale(0.85); } }
                @keyframes sai-rain-fade { to { opacity: 0; } }
                @keyframes sai-glow-fade { to { opacity: 0; } }
                @keyframes sai-tag-fade  { to { opacity: 0; } }
              `}</style>
              <line x1="8" y1="32" x2="56" y2="32" stroke="rgba(74,222,128,0.55)" strokeWidth="0.75" strokeDasharray="3 3" style={{ animation: 'sai-scan 2.4s ease-in-out infinite' }} />
              <circle cx="32" cy="32" r="27" fill="none" stroke="rgba(74,222,128,0.22)" strokeWidth="0.75" strokeDasharray="4 3" style={{ animation: 'sai-spin 9s linear infinite', transformOrigin: '32px 32px' }} />
              <circle cx="32" cy="32" r="20" fill="none" stroke="rgba(74,222,128,0.5)" strokeWidth="1" />
              <circle cx="32" cy="32" r="13" fill="none" stroke="rgba(74,222,128,0.18)" strokeWidth="0.75" strokeDasharray="2 4" style={{ animation: 'sai-rspin 5s linear infinite', transformOrigin: '32px 32px' }} />
              <line x1="32" y1="4"  x2="32" y2="18" stroke="#4ade80" strokeWidth="1.5" strokeLinecap="round" />
              <line x1="32" y1="46" x2="32" y2="60" stroke="#4ade80" strokeWidth="1.5" strokeLinecap="round" />
              <line x1="4"  y1="32" x2="18" y2="32" stroke="#4ade80" strokeWidth="1.5" strokeLinecap="round" />
              <line x1="46" y1="32" x2="60" y2="32" stroke="#4ade80" strokeWidth="1.5" strokeLinecap="round" />
              <path d="M14 21 L14 14 L21 14" fill="none" stroke="rgba(74,222,128,0.55)" strokeWidth="1" strokeLinecap="round" />
              <path d="M50 21 L50 14 L43 14" fill="none" stroke="rgba(74,222,128,0.55)" strokeWidth="1" strokeLinecap="round" />
              <path d="M14 43 L14 50 L21 50" fill="none" stroke="rgba(74,222,128,0.55)" strokeWidth="1" strokeLinecap="round" />
              <path d="M50 43 L50 50 L43 50" fill="none" stroke="rgba(74,222,128,0.55)" strokeWidth="1" strokeLinecap="round" />
              <circle cx="32" cy="32" r="4.5" fill="rgba(74,222,128,0.1)" stroke="#4ade80" strokeWidth="1.5" />
              <circle cx="32" cy="32" r="1.5" fill="#4ade80" style={{ animation: 'sai-blink 1.2s ease-in-out infinite' }} />
            </svg>
          </div>
        </div>

        {/* Heading — stays prominent always */}
        <div>
          <h1 style={{
            fontFamily: 'monospace', fontSize: 22, fontWeight: 700,
            color: '#e2e8f0', letterSpacing: 1, margin: 0,
          }}>
            SurgicalAI
          </h1>
          {/* Tagline fades away with the icon */}
          <div style={{
            fontFamily: 'monospace', fontSize: 11,
            color: '#4ade80', marginTop: 5, letterSpacing: 0.5, opacity: 0.85,
            animation: 'sai-tag-fade 1.2s ease-in-out 3s forwards',
          }}>
            Precision code edits. Zero collateral.
          </div>
        </div>

        {/* Capability pills — stay visible */}
        <div className="flex flex-wrap justify-center gap-2">
          {['Symbol-level edits', 'QA verified', 'Multi-file'].map(chip => (
            <span key={chip} style={{
              fontFamily: 'monospace', fontSize: 9,
              color: 'rgba(74,222,128,0.7)',
              border: '1px solid rgba(74,222,128,0.2)',
              borderRadius: 6, padding: '3px 10px',
              background: 'rgba(74,222,128,0.05)',
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
  } = useAppStore()

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
  const bottomRef          = useRef<HTMLDivElement>(null)
  const textareaRef        = useRef<HTMLTextAreaElement>(null)
  const fileInputRef       = useRef<HTMLInputElement>(null)

  // Scroll to bottom on new messages/streaming
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingMsg])

  // Load session files when active session changes
  useEffect(() => {
    if (activeSessions) {
      api.sessionFiles.list(activeSessions).then(setSessionFiles).catch(() => {})
    } else {
      setSessionFiles([])
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

    let accumulated  = ''
    let gotResult    = false

    const ctrl = api.stream.smart(
      { session_id: sessionId, message: text, file_ids: sessionFiles.map(f => f.id) },
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
      (token) => { accumulated += token; setStreamingMsg(accumulated) },
      (result) => {
        gotResult = true
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
        if (gotResult) return
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
      (err) => { setError(err); stopStream(); setBuildEdit(false) },
      undefined, // onThinking — omitted on mobile for simplicity
      // onCompacting
      (phase) => {
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
            created_at: new Date().toISOString(),
          })
        }
      },
      () => setBuildEdit(true),
      () => setBuildEdit(false),
    )
    ctrlRef.current = ctrl
  }, [input, isStreaming, settings, ensureSession, messages.length, sessionFiles,
      addMessage, setSessions, stopStream])

  // File upload
  const handleFileUpload = async (files: FileList | null) => {
    if (!files?.length) return
    const sessionId = await ensureSession()
    for (const file of Array.from(files)) {
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
                sessionId={activeSessions || ''}
                sessionFiles={sessionFiles}
                setSessionFiles={setSessionFiles}
              />
            ))}
            {isStreaming && (
              <StreamingBubble
                text={streamingMsg}
                progress={streamProgress}
                isBuildingEdit={isBuildingEdit}
              />
            )}
            {error && (
              <div className="mx-4 my-2 px-3 py-2.5 bg-red-500/10 border border-red-500/30 rounded-xl text-xs text-red-400 flex items-start gap-2">
                <span>⚠️</span>
                <span className="flex-1">{error}</span>
                <button onClick={() => setError(null)} className="text-red-400/60 hover:text-red-400">✕</button>
              </div>
            )}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* File chips */}
      {hasFiles && (
        <div className="flex-shrink-0 px-3 py-2 border-t border-border/50 flex flex-wrap gap-1.5">
          {sessionFiles.slice(0, 4).map(f => (
            <FileChip key={f.id} file={f} onRemove={() => removeFile(f.id)} />
          ))}
          {sessionFiles.length > 4 && (
            <span className="text-[10px] text-muted/50 self-center">+{sessionFiles.length - 4} more</span>
          )}
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
            placeholder={isCompacting ? 'Compacting history…' : 'Ask about your code…'}
            rows={1}
            disabled={isCompacting}
            className="w-full resize-none bg-transparent px-4 pt-3 pb-1 text-[15px]
              text-ink placeholder:text-muted/40 focus:outline-none leading-relaxed"
            style={{ minHeight: 44, maxHeight: 160 }}
          />

          {/* Bottom toolbar — left icons | right send */}
          <div className="flex items-center justify-between px-2 pb-2 pt-1">

            {/* Left: attach + voice + expand */}
            <div className="flex items-center gap-0.5">
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
                onClick={stopStream}
                className="w-9 h-9 flex items-center justify-center rounded-xl
                  bg-red-500/15 text-red-400 hover:bg-red-500/25 active:scale-95 transition-all"
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
