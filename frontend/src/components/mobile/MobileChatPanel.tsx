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
      <div className="w-7 h-7 rounded-full bg-orange/20 border border-orange/30 flex items-center justify-center flex-shrink-0 mt-0.5">
        <span className="text-orange text-[10px] font-bold">AI</span>
      </div>
      <div className="flex-1 min-w-0">
        {progress && progress !== 'Thinking...' && (
          <div className="flex items-center gap-1.5 text-[11px] text-muted/70 mb-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-orange animate-pulse" />
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
      <div className="w-7 h-7 rounded-full bg-orange/20 border border-orange/30 flex items-center justify-center flex-shrink-0 mt-0.5">
        <span className="text-orange text-[10px] font-bold">AI</span>
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
          className="text-sm font-semibold text-orange disabled:text-muted/40 transition-colors px-1 py-1"
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
          <div className="flex flex-col items-center justify-center h-full gap-4 px-6 text-center">
            <div className="w-14 h-14 rounded-2xl bg-orange/10 border border-orange/20 flex items-center justify-center">
              <span className="text-2xl">✂️</span>
            </div>
            <div>
              <p className="text-sm font-semibold text-ink/80 mb-1">SurgicalAI</p>
              <p className="text-xs text-muted/60">Upload files and ask for code changes.</p>
            </div>
          </div>
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

      {/* Input bar */}
      <div className="flex-shrink-0 px-3 py-3 border-t border-border bg-surface/50">
        <div className="flex items-end gap-2">
          {/* File upload button */}
          <button
            onClick={() => fileInputRef.current?.click()}
            className="flex-shrink-0 w-9 h-9 rounded-xl border border-border bg-surface flex items-center justify-center text-muted/60 hover:text-ink hover:border-border/80 transition-colors"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
            </svg>
          </button>
          <VoiceButton
            onTranscript={(text) => setInput(prev => prev ? prev + ' ' + text : text)}
            lastResponse={messages.filter(m => m.role === 'assistant' && m.content).slice(-1)[0]?.content}
            disabled={isStreaming || isCompacting}
            size="compact"
          />
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={e => handleFileUpload(e.target.files)}
          />

          {/* Text input + expand button */}
          <div className="flex-1 relative">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={e => {
                setInput(e.target.value)
                e.target.style.height = 'auto'
                e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px'
              }}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
              }}
              placeholder="Ask about your code..."
              rows={1}
              className="w-full resize-none bg-overlay/40 border border-border rounded-xl px-3 py-2.5 pr-8 text-sm text-ink placeholder:text-muted/40 focus:outline-none focus:border-orange/40 focus:bg-overlay/60 transition-colors"
              style={{ minHeight: 40, maxHeight: 120 }}
            />
            {/* Expand button — opens full-screen compose */}
            <button
              onClick={() => setComposeOpen(true)}
              className="absolute right-2 top-2 w-5 h-5 flex items-center justify-center text-muted/40 hover:text-muted/80 transition-colors"
              title="Expand editor"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/>
                <line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/>
              </svg>
            </button>
          </div>

          {/* Compose sheet — full-screen editor */}
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

          {/* Send / Stop button */}
          <button
            onClick={isStreaming ? stopStream : handleSend}
            disabled={!isStreaming && !canSend}
            className={`flex-shrink-0 w-9 h-9 rounded-xl flex items-center justify-center transition-all
              ${isStreaming
                ? 'bg-red-500/20 border border-red-500/40 text-red-400 hover:bg-red-500/30'
                : canSend
                  ? 'bg-orange text-white shadow-sm hover:bg-orange/90'
                  : 'bg-surface border border-border text-muted/30'
              }`}
          >
            {isStreaming ? (
              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                <rect x="4" y="4" width="16" height="16" rx="2"/>
              </svg>
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="22" y1="2" x2="11" y2="13"/>
                <polygon points="22 2 15 22 11 13 2 9 22 2"/>
              </svg>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
