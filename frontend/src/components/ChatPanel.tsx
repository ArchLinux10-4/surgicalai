import React, { useState, useRef, useEffect, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { Send, Zap, Code2, AlertTriangle, X, Paperclip, Plus, ChevronDown } from 'lucide-react'
import type { PromptTemplate } from '../types'

// ── Message bubble ──────────────────────────────────
function Message({ msg }: { msg: any }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`px-4 py-3 border-b border-border/50 ${isUser ? 'bg-overlay/40' : 'bg-surface/40'}`}>
      <div className="flex items-center gap-2 mb-1.5">
        <div className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0 ${
          isUser ? 'bg-accent/20 text-accent' : 'bg-success/20 text-success'
        }`}>
          {isUser ? 'U' : 'AI'}
        </div>
        <span className="text-[11px] font-semibold uppercase tracking-wide text-faint">
          {isUser ? 'You' : 'SurgicalAI'}
        </span>
        <span className="text-[10px] text-faint ml-auto">
          {new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </span>
      </div>
      {isUser ? (
        <div className="text-sm text-ink leading-relaxed whitespace-pre-wrap pl-7">{msg.content}</div>
      ) : (
        <div className="markdown-body text-sm text-ink pl-7">{<ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>}</div>
      )}
    </div>
  )
}

// ── Streaming bubble ────────────────────────────────
function StreamingBubble({ content, progress }: { content: string; progress?: string }) {
  return (
    <div className="px-4 py-3 border-b border-border/50 bg-surface/40">
      <div className="flex items-center gap-2 mb-1.5">
        <div className="w-5 h-5 rounded-full bg-success/20 text-success flex items-center justify-center text-[10px] font-bold">AI</div>
        <span className="text-[11px] font-semibold uppercase tracking-wide text-faint">SurgicalAI</span>
        {progress && (
          <span className="text-[11px] text-accent ml-2 flex items-center gap-1">
            <span className="spin inline-block text-xs">◌</span> {progress}
          </span>
        )}
      </div>
      {content && (
        <div className="markdown-body text-sm text-ink pl-7">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      )}
      <span className="inline-block w-2 h-3.5 bg-accent rounded-sm cursor-blink ml-7 align-text-bottom mt-1" />
    </div>
  )
}

// ── Templates picker ────────────────────────────────
function TemplatesPicker({ onSelect, onClose }: { onSelect: (t: PromptTemplate) => void; onClose: () => void }) {
  const { templates } = useAppStore()
  const [filter, setFilter] = useState<'all' | 'chat' | 'surgical'>('all')
  const filtered = templates.filter((t) => filter === 'all' || t.mode === filter)

  return (
    <div className="absolute bottom-full left-0 right-0 mb-2 z-50 bg-surface border border-border rounded-xl shadow-modal overflow-hidden animate-slide-up">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <div className="flex gap-1.5">
          {(['all', 'chat', 'surgical'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-2.5 py-1 rounded-full text-[11px] font-semibold transition-colors ${
                filter === f ? 'bg-accent/20 text-accent border border-accent/30' : 'text-faint hover:text-muted border border-transparent'
              }`}
            >
              {f}
            </button>
          ))}
        </div>
        <button onClick={onClose} className="btn-icon"><X size={12} /></button>
      </div>
      <div className="max-h-56 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="py-8 text-center text-faint text-xs">No templates</div>
        ) : filtered.map((t) => (
          <div
            key={t.id}
            onClick={() => { onSelect(t); onClose() }}
            className="flex items-start gap-3 px-3 py-2.5 cursor-pointer border-b border-border/50 hover:bg-overlay transition-colors"
          >
            <span className={`badge mt-0.5 flex-shrink-0 ${t.mode === 'surgical' ? 'badge-success' : 'badge-accent'}`}>
              {t.mode === 'surgical' ? '✂' : '💬'}
            </span>
            <div className="min-w-0">
              <div className="text-sm font-semibold text-ink">{t.name}</div>
              <div className="text-[11px] text-muted truncate mt-0.5">{t.prompt.slice(0, 90)}…</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Empty state ─────────────────────────────────────
function EmptyState({ mode, activeFile }: { mode: string; activeFile: any }) {
  return (
    <div className="flex flex-col items-center justify-center h-full px-8 py-12 text-center">
      <div className="w-14 h-14 rounded-2xl bg-accent/10 border border-accent/20 flex items-center justify-center mb-4">
        <Zap size={26} className="text-accent" />
      </div>
      <h2 className="text-base font-bold text-ink mb-2">SurgicalAI</h2>
      <p className="text-sm text-muted leading-relaxed mb-6">
        Chat mode: ask anything about code.<br />
        <span className="text-accent font-medium">Surgical mode</span>: precise, atomic edits — zero collateral damage.
      </p>

      <div className="w-full space-y-2 text-left">
        {[
          { key: '⌘↵', desc: 'Send message' },
          { key: '⌘/', desc: 'Toggle Surgical / Chat' },
          { key: '⌘K', desc: 'Focus input' },
          { key: '⌘N', desc: 'New chat' },
          { key: 'Esc', desc: 'Stop streaming' },
        ].map(({ key, desc }) => (
          <div key={key} className="flex items-center gap-3">
            <kbd className="px-2 py-0.5 rounded bg-overlay border border-border text-[11px] font-mono text-muted flex-shrink-0">{key}</kbd>
            <span className="text-[13px] text-muted">{desc}</span>
          </div>
        ))}
      </div>

      {activeFile && (
        <div className="mt-6 w-full px-3 py-2.5 bg-surface border border-border rounded-lg flex items-center gap-2">
          <Code2 size={13} className="text-accent flex-shrink-0" />
          <span className="text-[12px] text-ink font-medium truncate">{activeFile.path.split('/').pop()}</span>
          <span className="text-[11px] text-faint ml-auto flex-shrink-0">{activeFile.lines} lines</span>
        </div>
      )}
    </div>
  )
}

// ── Chat Panel ──────────────────────────────────────
export function ChatPanel() {
  const {
    activeSessions, setActiveSession, messages, addMessage, setMessages,
    isStreaming, setIsStreaming, streamingMessage, setStreamingMessage,
    streamProgress, setStreamProgress, sessions, setSessions, settings,
    setSurgicalAnalysis, setSurgicalPanelOpen, activeFile,
    setTemplates, templates, workspacePath,
  } = useAppStore()

  const [input, setInput] = useState('')
  const [mode, setMode] = useState<'chat' | 'surgical'>('chat')
  const [error, setError] = useState<string | null>(null)
  const [showTemplates, setShowTemplates] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingMessage])

  useEffect(() => {
    api.context.getTemplates().then(setTemplates).catch(() => {})
  }, [])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); textareaRef.current?.focus() }
      if ((e.ctrlKey || e.metaKey) && e.key === '/') { e.preventDefault(); setMode((m) => m === 'chat' ? 'surgical' : 'chat') }
      if ((e.ctrlKey || e.metaKey) && e.key === 'n') { e.preventDefault(); newChat() }
      if (e.key === 'Escape' && isStreaming) { abortRef.current?.abort(); stopStream() }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [isStreaming])

  const stopStream = () => { setIsStreaming(false); setStreamingMessage(''); setStreamProgress('') }

  const newChat = async () => {
    try {
      const s = await api.chat.createSession({ title: 'New Chat' })
      const updated = await api.chat.getSessions()
      setSessions(updated)
      setActiveSession(s.id)
      setMessages([])
    } catch (e: any) {
      toast.error('Failed to create chat')
    }
  }

  const ensureSession = async () => {
    if (activeSessions) return activeSessions
    const s = await api.chat.createSession({ title: input.slice(0, 40) || 'New Chat', file_path: activeFile?.path || null })
    const updated = await api.chat.getSessions()
    setSessions(updated)
    setActiveSession(s.id)
    return s.id
  }

  const handleSend = useCallback(async () => {
    if (!input.trim() || isStreaming) return
    if (!settings?.openai_api_key_set) {
      setError('Please add your OpenAI API key in Settings first.')
      return
    }
    setError(null)
    const text = input.trim()
    setInput('')

    if (mode === 'surgical') {
      if (!activeFile) { setError('Open a file first to use Surgical mode.'); return }
      setIsStreaming(true); setStreamProgress('Initializing…'); setStreamingMessage('')
      try {
        const sessionId = await ensureSession()
        addMessage({ id: Date.now().toString(), session_id: sessionId, role: 'user', content: `✂️ Surgical: ${text}`, created_at: new Date().toISOString() })
        const ctrl = api.stream.surgical(
          { file_path: activeFile.path, file_content: activeFile.content, request: text, session_id: sessionId },
          (msg) => setStreamProgress(msg),
          (result) => {
            setSurgicalAnalysis(result)
            setSurgicalPanelOpen(true)
            stopStream()
            addMessage({
              id: Date.now().toString() + '_ai', session_id: sessionId, role: 'assistant',
              content: `**Surgical Analysis Complete** ✂️\n\n${result.plan?.summary || ''}\n\n**${result.changes?.length || 0} change(s) identified.** Review them in the Changes tab →`,
              created_at: new Date().toISOString()
            })
            api.chat.getSessions().then(setSessions)
          },
          (err) => { setError(err); stopStream() }
        )
        abortRef.current = ctrl
      } catch (e: any) { setError(e.message); stopStream() }
    } else {
      setIsStreaming(true); setStreamingMessage(''); setStreamProgress('')
      try {
        const sessionId = await ensureSession()
        addMessage({ id: Date.now().toString(), session_id: sessionId, role: 'user', content: text, created_at: new Date().toISOString() })
        let accumulated = ''
        const ctrl = api.stream.chat(
          { session_id: sessionId, message: text, file_content: activeFile?.content, model: settings?.architect_model },
          (chunk) => { if (chunk.type === 'token') { accumulated += chunk.content; setStreamingMessage(accumulated) } },
          (fullText) => {
            stopStream()
            addMessage({ id: Date.now().toString() + '_ai', session_id: sessionId, role: 'assistant', content: fullText, created_at: new Date().toISOString() })
            api.chat.getSessions().then(setSessions)
          },
          (err) => { setError(err); stopStream() }
        )
        abortRef.current = ctrl
      } catch (e: any) { setError(e.message); stopStream() }
    }
  }, [input, isStreaming, settings, mode, activeFile, activeSessions])

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleSend() }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border bg-surface/50 flex-shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          {activeFile ? (
            <>
              <Code2 size={13} className="text-accent flex-shrink-0" />
              <span className="text-sm font-semibold text-ink truncate">{activeFile.path.split('/').pop()}</span>
              <span className="text-[11px] text-faint flex-shrink-0">{activeFile.lines}L</span>
            </>
          ) : (
            <span className="text-sm text-muted">No file open</span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] text-faint">{settings?.architect_model || 'gpt-4.1'}</span>
          <button onClick={newChat} className="btn-icon" title="New Chat (⌘N)">
            <Plus size={14} />
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto">
        {messages.length === 0 && !isStreaming ? (
          <EmptyState mode={mode} activeFile={activeFile} />
        ) : (
          messages.map((msg, i) => <Message key={msg.id || i} msg={msg} />)
        )}
        {isStreaming && (streamingMessage || streamProgress) && (
          <StreamingBubble content={streamingMessage} progress={streamProgress} />
        )}
        {error && (
          <div className="mx-4 my-3 flex items-start gap-2.5 px-3.5 py-3 bg-danger/10 border border-danger/30 rounded-xl text-sm text-danger">
            <AlertTriangle size={15} className="flex-shrink-0 mt-0.5" />
            <span>{error}</span>
            <button onClick={() => setError(null)} className="ml-auto btn-icon w-5 h-5 text-danger/70 hover:text-danger flex-shrink-0">
              <X size={11} />
            </button>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input area */}
      <div className="border-t border-border p-3 flex-shrink-0 relative">
        {showTemplates && (
          <TemplatesPicker
            onSelect={(t) => { setInput(t.prompt); setMode(t.mode as 'chat' | 'surgical'); textareaRef.current?.focus() }}
            onClose={() => setShowTemplates(false)}
          />
        )}

        {/* Mode + actions row */}
        <div className="flex items-center gap-2 mb-2">
          <div className="flex bg-overlay rounded-lg p-0.5 gap-0.5">
            {(['chat', 'surgical'] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-3 py-1 rounded-md text-[12px] font-semibold transition-all ${
                  mode === m
                    ? m === 'surgical'
                      ? 'bg-success text-base shadow-sm'
                      : 'bg-accent text-base shadow-sm'
                    : 'text-muted hover:text-ink'
                }`}
              >
                {m === 'surgical' ? '✂ Surgical' : '💬 Chat'}
              </button>
            ))}
          </div>

          <button
            onClick={() => setShowTemplates((s) => !s)}
            className={`btn-ghost ml-auto gap-1 ${showTemplates ? 'text-accent border-accent/40 bg-accent/10' : ''}`}
          >
            📋 Templates
          </button>
        </div>

        {/* Textarea + send */}
        <div className="flex gap-2 items-end">
          <div className="relative flex-1">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKey}
              placeholder={mode === 'surgical'
                ? 'Describe the surgical change… (⌘↵)'
                : 'Ask about your code… (⌘↵ to send)'}
              rows={3}
              className="input resize-none text-sm leading-relaxed py-2.5 pr-3 font-[inherit]"
              onFocus={(e) => (e.target.style.borderColor = '#58a6ff')}
              onBlur={(e) => (e.target.style.borderColor = '')}
            />
          </div>

          {isStreaming ? (
            <button
              onClick={() => { abortRef.current?.abort(); stopStream() }}
              className="px-4 py-2.5 rounded-lg bg-danger border-none text-white font-bold text-sm flex items-center gap-1.5 hover:bg-danger/90 transition-colors flex-shrink-0"
            >
              <X size={14} /> Stop
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!input.trim()}
              className={`px-4 py-2.5 rounded-lg border-none font-bold text-sm flex items-center gap-1.5 transition-all flex-shrink-0 ${
                !input.trim()
                  ? 'bg-border text-faint cursor-not-allowed'
                  : mode === 'surgical'
                    ? 'bg-success text-base hover:bg-success/90 active:scale-95'
                    : 'bg-accent text-base hover:bg-accent/90 active:scale-95'
              }`}
            >
              {mode === 'surgical' ? <Zap size={14} /> : <Send size={14} />}
            </button>
          )}
        </div>

        <div className="flex justify-between mt-1.5 text-[11px] text-faint">
          <span className={mode === 'surgical' ? 'text-success font-medium' : ''}>
            {mode === 'surgical' ? '✂ Surgical mode — atomic edits only' : '💬 Chat mode'}
          </span>
          <span>⌘↵ send · ⌘/ toggle</span>
        </div>
      </div>
    </div>
  )
}
