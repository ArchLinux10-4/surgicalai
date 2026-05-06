import React, { useState, useRef, useEffect, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { InlineDiffCard } from './InlineDiffCard'
import {
  Send, X, Plus, Paperclip, FileCode, AlertTriangle, Zap, Trash2
} from 'lucide-react'
import type { SessionFile, SmartResult } from '../types'

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

// ── Message bubble ────────────────────────────────────────
function Message({ msg, sessionId }: { msg: any; sessionId: string }) {
  const isUser = msg.role === 'user'
  const isSurgical = msg.message_type === 'surgical_result'

  let surgicalResult: SmartResult | null = null
  if (isSurgical && msg.surgical_data) {
    try { surgicalResult = JSON.parse(msg.surgical_data) } catch {}
  }

  return (
    <div className={`px-4 py-3 border-b border-zinc-800/50 ${isUser ? 'bg-zinc-800/30' : 'bg-zinc-900/30'}`}>
      <div className="flex items-center gap-2 mb-1.5">
        <div className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0 ${
          isUser ? 'bg-blue-500/20 text-blue-400' : 'bg-green-500/20 text-green-400'
        }`}>
          {isUser ? 'U' : 'AI'}
        </div>
        <span className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
          {isUser ? 'You' : 'SurgicalAI'}
        </span>
        <span className="text-[10px] text-zinc-600 ml-auto">
          {msg.created_at ? new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}
        </span>
      </div>

      <div className="pl-7">
        {isSurgical && surgicalResult ? (
          <InlineDiffCard result={surgicalResult} sessionId={sessionId} />
        ) : isUser ? (
          <div className="text-sm text-zinc-200 leading-relaxed whitespace-pre-wrap">{msg.content}</div>
        ) : (
          <div className="markdown-body text-sm text-zinc-200">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Streaming bubble ──────────────────────────────────────
function StreamingBubble({ content, progress }: { content: string; progress: string }) {
  return (
    <div className="px-4 py-3 border-b border-zinc-800/50 bg-zinc-900/30">
      <div className="flex items-center gap-2 mb-1.5">
        <div className="w-5 h-5 rounded-full bg-green-500/20 text-green-400 flex items-center justify-center text-[10px] font-bold">AI</div>
        <span className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">SurgicalAI</span>
        {progress && (
          <span className="text-[11px] text-blue-400 ml-2 flex items-center gap-1">
            <span className="animate-spin inline-block text-xs">◌</span> {progress}
          </span>
        )}
      </div>
      {content && (
        <div className="markdown-body text-sm text-zinc-200 pl-7">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      )}
      <span className="inline-block w-2 h-3.5 bg-blue-400 rounded-sm ml-7 align-text-bottom mt-1 animate-pulse" />
    </div>
  )
}

// ── File chip ─────────────────────────────────────────────
function FileChip({ file, onRemove }: { file: SessionFile; onRemove: () => void }) {
  const langColors: Record<string, string> = {
    python: 'text-blue-400', typescript: 'text-cyan-400', javascript: 'text-yellow-400',
    go: 'text-cyan-300', rust: 'text-orange-400', java: 'text-red-400',
  }
  const color = langColors[file.language] || 'text-zinc-400'

  return (
    <div className="flex items-center gap-1.5 px-2.5 py-1 bg-zinc-800 border border-zinc-700 rounded-lg group">
      <FileCode size={11} className={color} />
      <span className="text-[12px] font-medium text-zinc-200">{file.filename}</span>
      <span className="text-[10px] text-zinc-500">{file.lines}L</span>
      {file.symbol_count > 0 && (
        <span className="text-[10px] text-zinc-600">{file.symbol_count}⚡</span>
      )}
      <button
        onClick={onRemove}
        className="ml-0.5 opacity-0 group-hover:opacity-100 transition-opacity text-zinc-500 hover:text-red-400"
      >
        <X size={10} />
      </button>
    </div>
  )
}

// ── Empty state ───────────────────────────────────────────
function EmptyState({ onUpload }: { onUpload: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full px-8 py-12 text-center">
      <div className="w-16 h-16 rounded-2xl bg-blue-500/10 border border-blue-500/20 flex items-center justify-center mb-5">
        <Zap size={28} className="text-blue-400" />
      </div>
      <h2 className="text-base font-bold text-zinc-100 mb-2">SurgicalAI</h2>
      <p className="text-sm text-zinc-400 leading-relaxed mb-6 max-w-xs">
        Upload your code files, then describe what you want to change. The AI reads all your files and figures out exactly what to edit.
      </p>

      <button
        onClick={onUpload}
        className="flex items-center gap-2 px-4 py-2.5 bg-blue-500/20 text-blue-400 border border-blue-500/30 rounded-xl text-sm font-semibold hover:bg-blue-500/30 transition-colors mb-6"
      >
        <Paperclip size={15} /> Upload files to get started
      </button>

      <div className="w-full space-y-2 text-left">
        {[
          { ex: 'Upload settings.py', desc: 'then ask: "Add GPT-5 as a model option"' },
          { ex: 'Upload auth.ts, api.ts', desc: 'then ask: "Add rate limiting to all endpoints"' },
          { ex: 'Upload any code file', desc: 'then ask anything — edit, explain, review' },
        ].map(({ ex, desc }) => (
          <div key={ex} className="flex items-start gap-2.5 px-3 py-2 bg-zinc-800/50 rounded-lg border border-zinc-700/50">
            <FileCode size={12} className="text-blue-400 mt-0.5 flex-shrink-0" />
            <div>
              <div className="text-[12px] font-semibold text-zinc-300">{ex}</div>
              <div className="text-[11px] text-zinc-500">{desc}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-6 w-full space-y-1.5 text-left">
        <div className="text-[10px] text-zinc-600 uppercase tracking-wider font-semibold mb-1">Keyboard shortcuts</div>
        {[
          ['⌘↵', 'Send'], ['⌘K', 'Focus input'], ['⌘N', 'New chat'], ['Esc', 'Stop'],
        ].map(([key, desc]) => (
          <div key={key} className="flex items-center gap-2">
            <kbd className="px-1.5 py-0.5 rounded bg-zinc-800 border border-zinc-700 text-[10px] font-mono text-zinc-400">{key}</kbd>
            <span className="text-[11px] text-zinc-500">{desc}</span>
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
    streamProgress, setStreamProgress, sessions, setSessions, settings,
    sessionFiles, setSessionFiles, addSessionFile, removeSessionFile,
  } = useAppStore()

  const [input, setInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [uploadingFiles, setUploadingFiles] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingMessage])

  // Load session files when session changes
  useEffect(() => {
    if (activeSessions) {
      api.sessionFiles.list(activeSessions)
        .then(files => setSessionFiles(files))
        .catch(() => {})
    } else {
      setSessionFiles([])
    }
  }, [activeSessions])

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); textareaRef.current?.focus() }
      if ((e.ctrlKey || e.metaKey) && e.key === 'n') { e.preventDefault(); newChat() }
      if (e.key === 'Escape' && isStreaming) { abortRef.current?.abort(); stopStream() }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [isStreaming])

  const stopStream = () => {
    setIsStreaming(false)
    setStreamingMessage('')
    setStreamProgress('')
  }

  const newChat = async () => {
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
    const files = Array.from(fileList)
    if (!files.length) return

    const sessionId = await ensureSession()
    setUploadingFiles(true)
    const promises = files.map(async (file) => {
      const content = await file.text()
      const language = getLanguage(file.name)
      try {
        const result = await api.sessionFiles.upload(sessionId, {
          filename: file.name,
          content,
          language,
        })
        addSessionFile(result)
        return result
      } catch (e: any) {
        toast.error(`Failed to upload ${file.name}: ${e.message}`)
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
      return ['py','js','ts','tsx','jsx','go','rs','java','cs','rb','php','swift','kt','html','css','json','yaml','yml','toml','md','sh','sql','cpp','c','h'].includes(ext)
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
    if (!settings?.openai_api_key_set) {
      setError('Add your OpenAI API key in Settings first.')
      return
    }
    setError(null)
    const text = input.trim()
    setInput('')

    const sessionId = await ensureSession()

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

    let accumulated = ''
    let gotResult = false

    const ctrl = api.stream.smart(
      { session_id: sessionId, message: text },
      (progress) => setStreamProgress(progress),
      (token) => { accumulated += token; setStreamingMessage(accumulated) },
      (result) => {
        // Surgical result — show inline diff card
        gotResult = true
        stopStream()
        addMessage({
          id: Date.now().toString() + '_ai',
          session_id: sessionId,
          role: 'assistant',
          message_type: 'surgical_result',
          surgical_data: JSON.stringify(result),
          content: '',
          created_at: new Date().toISOString(),
        })
        api.chat.getSessions().then(setSessions).catch(() => {})
      },
      (fullText) => {
        if (gotResult) return
        stopStream()
        if (fullText.trim()) {
          addMessage({
            id: Date.now().toString() + '_ai',
            session_id: sessionId,
            role: 'assistant',
            content: fullText,
            created_at: new Date().toISOString(),
          })
        }
        api.chat.getSessions().then(setSessions).catch(() => {})
      },
      (err) => { setError(err); stopStream() }
    )

    abortRef.current = ctrl
  }, [input, isStreaming, settings, activeSessions, sessionFiles])

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
        <div className="absolute inset-0 z-50 bg-blue-500/10 border-2 border-dashed border-blue-400 rounded-xl flex items-center justify-center pointer-events-none">
          <div className="text-center">
            <Paperclip size={32} className="text-blue-400 mx-auto mb-2" />
            <p className="text-blue-300 font-semibold text-sm">Drop files here</p>
            <p className="text-blue-400/60 text-xs mt-1">py, ts, js, go, rs, and more</p>
          </div>
        </div>
      )}

      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept=".py,.js,.ts,.tsx,.jsx,.go,.rs,.java,.cs,.rb,.php,.swift,.kt,.html,.css,.json,.yaml,.yml,.toml,.md,.sh,.sql,.cpp,.c,.h"
        className="hidden"
        onChange={handleFileInput}
      />

      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-zinc-800 bg-zinc-900/50 flex-shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <Zap size={13} className="text-blue-400 flex-shrink-0" />
          <span className="text-sm font-semibold text-zinc-200">
            {hasFiles ? `${sessionFiles.length} file${sessionFiles.length > 1 ? 's' : ''} in context` : 'SurgicalAI'}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] text-zinc-500">{settings?.architect_model || 'gpt-4.1'}</span>
          <button onClick={() => fileInputRef.current?.click()} className="p-1.5 rounded-lg hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors" title="Upload files">
            <Paperclip size={13} />
          </button>
          <button onClick={newChat} className="p-1.5 rounded-lg hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors" title="New chat (⌘N)">
            <Plus size={14} />
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto">
        {messages.length === 0 && !isStreaming ? (
          <EmptyState onUpload={() => fileInputRef.current?.click()} />
        ) : (
          messages.map((msg, i) => (
            <Message key={msg.id || i} msg={msg} sessionId={activeSessions || ''} />
          ))
        )}
        {isStreaming && (streamingMessage || streamProgress) && (
          <StreamingBubble content={streamingMessage} progress={streamProgress} />
        )}
        {error && (
          <div className="mx-4 my-3 flex items-start gap-2.5 px-3.5 py-3 bg-red-500/10 border border-red-500/30 rounded-xl text-sm text-red-400">
            <AlertTriangle size={15} className="flex-shrink-0 mt-0.5" />
            <span>{error}</span>
            <button onClick={() => setError(null)} className="ml-auto p-0.5 hover:text-red-300">
              <X size={11} />
            </button>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input area */}
      <div className="border-t border-zinc-800 p-3 flex-shrink-0 bg-zinc-900/50">
        {/* File chips */}
        {hasFiles && (
          <div className="flex flex-wrap gap-1.5 mb-2.5">
            {sessionFiles.map(file => (
              <FileChip
                key={file.id}
                file={file}
                onRemove={() => removeFile(file.id)}
              />
            ))}
            {uploadingFiles && (
              <div className="flex items-center gap-1.5 px-2.5 py-1 bg-zinc-800/60 border border-zinc-700 rounded-lg">
                <span className="text-[11px] text-zinc-500 animate-pulse">Uploading...</span>
              </div>
            )}
          </div>
        )}

        {/* Textarea + buttons */}
        <div className="flex gap-2 items-end">
          <button
            onClick={() => fileInputRef.current?.click()}
            className="p-2.5 rounded-xl bg-zinc-800 border border-zinc-700 text-zinc-400 hover:text-zinc-200 hover:border-zinc-600 transition-colors flex-shrink-0"
            title="Attach files"
          >
            <Paperclip size={15} />
          </button>

          <div className="flex-1 relative">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKey}
              placeholder={
                hasFiles
                  ? `Ask about your ${sessionFiles.length} file${sessionFiles.length > 1 ? 's' : ''} — "Add X", "Fix Y", "Explain Z"…`
                  : 'Ask anything, or drop files here to edit code…'
              }
              rows={3}
              className="w-full bg-zinc-800 border border-zinc-700 rounded-xl text-sm text-zinc-200 placeholder:text-zinc-500 resize-none px-3 py-2.5 focus:outline-none focus:border-blue-500/60 leading-relaxed font-[inherit]"
            />
          </div>

          {isStreaming ? (
            <button
              onClick={() => { abortRef.current?.abort(); stopStream() }}
              className="px-3 py-2.5 rounded-xl bg-red-500/20 border border-red-500/30 text-red-400 font-bold text-sm flex items-center gap-1.5 hover:bg-red-500/30 transition-colors flex-shrink-0"
            >
              <X size={14} /> Stop
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!input.trim()}
              className={`px-3 py-2.5 rounded-xl border font-bold text-sm flex items-center gap-1.5 transition-all flex-shrink-0 ${
                !input.trim()
                  ? 'bg-zinc-800 border-zinc-700 text-zinc-600 cursor-not-allowed'
                  : 'bg-blue-500/20 border-blue-500/30 text-blue-400 hover:bg-blue-500/30 active:scale-95'
              }`}
            >
              <Send size={14} />
            </button>
          )}
        </div>

        <div className="flex items-center justify-between mt-1.5">
          <span className="text-[11px] text-zinc-600">
            {hasFiles ? `AI sees all ${sessionFiles.length} file(s) — just describe what you want` : 'Drag & drop files or click 📎'}
          </span>
          <span className="text-[11px] text-zinc-600">⌘↵ send</span>
        </div>
      </div>
    </div>
  )
}
