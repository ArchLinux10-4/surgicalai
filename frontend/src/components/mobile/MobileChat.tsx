import React, { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Paperclip, Send, X, Zap, FileCode, Trash2, Sparkles, Image as ImageIcon, FileText, Files,
} from 'lucide-react'
import { useAppStore } from '../../stores/appStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'
import { MobileDiffCard } from './MobileDiffCard'
import { NewFileCard } from '../NewFileCard'
import { MarkdownCode } from '../CodeBlock'
import { MermaidDiagram } from '../MermaidDiagram'
import type { SessionFile, SmartResult } from '../../types'

// File type detection
const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp']
const BINARY_EXTS = ['pdf', 'xlsx', 'xls']
const ACCEPT =
  '.py,.js,.ts,.tsx,.jsx,.go,.rs,.java,.cs,.rb,.php,.swift,.kt,.html,.css,.json,.yaml,.yml,.toml,.md,.sh,.sql,.cpp,.c,.h,.png,.jpg,.jpeg,.webp,.gif,.bmp,.pdf,.csv,.xlsx,.xls,.txt,.zip'

const LANG_BY_EXT: Record<string, string> = {
  ts: 'typescript', tsx: 'typescript', js: 'javascript', jsx: 'javascript',
  py: 'python', go: 'go', rs: 'rust', html: 'html', css: 'css',
  json: 'json', md: 'markdown', sh: 'bash', sql: 'sql',
}

function langOf(name: string): string {
  return LANG_BY_EXT[name.split('.').pop()?.toLowerCase() || ''] || 'text'
}

// ── Markdown component overrides — same approach as desktop, slightly larger ───
const mdComponents = {
  code: (({ className, children, ...props }: any) => {
    const lang = /language-(\w+)/.exec(className || '')?.[1] || ''
    if (lang === 'mermaid') {
      return <MermaidDiagram chart={String(children).replace(/\n$/, '')} />
    }
    return <MarkdownCode className={className} {...props}>{children}</MarkdownCode>
  }) as any,
  h1: ({ children }: any) => <h1 className="text-base font-semibold text-ink mt-4 mb-2 pb-1.5 border-b border-border/40">{children}</h1>,
  h2: ({ children }: any) => <h2 className="text-[15px] font-semibold text-ink mt-3 mb-1.5">{children}</h2>,
  h3: ({ children }: any) => <h3 className="text-[14px] font-semibold text-muted mt-3 mb-1">{children}</h3>,
  p:  ({ children }: any) => <p className="text-[15px] text-ink/90 leading-[1.65] mb-2.5 last:mb-0">{children}</p>,
  ul: ({ children }: any) => <ul className="my-2 space-y-1.5 pl-0">{children}</ul>,
  ol: ({ children }: any) => <ol className="my-2 space-y-0 pl-0">{children}</ol>,
  li: ({ children, ...props }: any) => {
    const ordered = (props as any).ordered
    return ordered ? (
      <li className="flex items-start gap-3 py-1.5 border-b border-border/30 last:border-b-0 text-[14px] text-ink/85 leading-[1.6] list-none">
        <span className="flex-shrink-0 w-5 h-5 rounded-full bg-surface border border-border/60 text-[10px] font-semibold text-muted flex items-center justify-center mt-0.5">
          {(props as any).index != null ? (props as any).index + 1 : ''}
        </span>
        <span className="flex-1">{children}</span>
      </li>
    ) : (
      <li className="flex items-start gap-2.5 text-[14px] text-ink/85 leading-[1.6] list-none">
        <span className="mt-[7px] w-1 h-1 rounded-full bg-muted/50 flex-shrink-0" />
        <span className="flex-1">{children}</span>
      </li>
    )
  },
  blockquote: ({ children }: any) => (
    <blockquote className="my-3 pl-3 border-l-2 border-accent/40 text-muted text-[14px] leading-[1.65] italic">{children}</blockquote>
  ),
  strong: ({ children }: any) => <strong className="font-semibold text-ink">{children}</strong>,
  em: ({ children }: any) => <em className="text-ink/80 italic">{children}</em>,
  a: ({ children, href }: any) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent underline underline-offset-2">{children}</a>
  ),
  hr: () => <hr className="my-4 border-border/40" />,
  table: ({ children }: any) => (
    <div className="my-3 overflow-x-auto rounded-lg border border-border/50">
      <table className="w-full text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }: any) => <thead className="bg-surface">{children}</thead>,
  th: ({ children }: any) => <th className="px-3 py-2 text-left text-[11px] font-semibold text-muted uppercase tracking-wide">{children}</th>,
  td: ({ children }: any) => <td className="px-3 py-2 text-[14px] text-ink/80 border-t border-border/30">{children}</td>,
}

const PROMPT_CHIPS = [
  { label: 'Explain this',  prompt: 'Explain what this code does in detail.' },
  { label: 'Find bugs',     prompt: 'Review this code for bugs and edge cases.' },
  { label: 'Add error handling', prompt: 'Add comprehensive error handling.' },
  { label: 'Write tests',   prompt: 'Write unit tests covering happy path and edge cases.' },
  { label: 'Refactor',      prompt: 'Refactor this code for readability.' },
]

interface Props {
  onOpenFiles: () => void
}

// ──────────────────────────────────────────────────────────────────────────────
// File chip in the input bar
// ──────────────────────────────────────────────────────────────────────────────
function MobileFileChip({ file, onRemove }: { file: SessionFile; onRemove: () => void }) {
  const ext = file.filename.split('.').pop()?.toLowerCase() || ''
  const Icon = IMAGE_EXTS.includes(ext) ? ImageIcon : BINARY_EXTS.includes(ext) ? FileText : FileCode
  return (
    <div className="flex items-center gap-1.5 px-2 py-1 bg-surface border border-border rounded-lg flex-shrink-0 max-w-[180px]">
      <Icon size={11} className="text-muted flex-shrink-0" />
      <span className="text-[11px] text-ink truncate">{file.filename}</span>
      <button
        onClick={onRemove}
        className="text-faint hover:text-danger flex-shrink-0"
        aria-label={`Remove ${file.filename}`}
      >
        <X size={11} />
      </button>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Empty state
// ──────────────────────────────────────────────────────────────────────────────
function MobileEmptyState({ onChip, onAttach }: { onChip: (p: string) => void; onAttach: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center min-h-full px-6 py-12 text-center">
      <div className="w-14 h-14 rounded-2xl bg-accent/10 flex items-center justify-center mb-4">
        <Sparkles size={26} className="text-accent" />
      </div>
      <h2 className="text-[20px] font-semibold text-ink mb-1.5">How can I help?</h2>
      <p className="text-[14px] text-muted mb-8 max-w-[280px]">
        Ask anything, or attach files to edit code with precision.
      </p>

      <button
        onClick={onAttach}
        className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-surface border border-border text-[14px] font-medium text-ink active:bg-overlay mb-6"
      >
        <Paperclip size={15} />
        Attach files
      </button>

      <div className="w-full max-w-sm space-y-2">
        {PROMPT_CHIPS.map(({ label, prompt }) => (
          <button
            key={label}
            onClick={() => onChip(prompt)}
            className="w-full px-4 py-3 rounded-xl bg-surface/60 border border-border/60 text-[14px] text-ink active:bg-surface text-left"
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Single message
// ──────────────────────────────────────────────────────────────────────────────
function MobileMessage({ msg, sessionId }: { msg: any; sessionId: string }) {
  // Compaction marker
  if (msg.message_type === 'compact_marker') {
    return (
      <div className="flex items-center justify-center py-2 px-4">
        <div className="flex items-center gap-1.5 px-3 py-1 bg-surface/60 border border-border/50 rounded-full">
          <Zap size={10} className="text-accent" />
          <span className="text-[10px] font-medium text-muted">History compacted</span>
        </div>
      </div>
    )
  }

  // Surgical result — diff card or new-file card
  if (msg.message_type === 'surgical_result' && msg.surgical_data) {
    let result: SmartResult | null = null
    try { result = JSON.parse(msg.surgical_data) } catch {}
    if (!result) return null
    return (
      <div className="px-3 py-3">
        {result.intent === 'create' ? (
          <NewFileCard result={result} sessionId={sessionId} />
        ) : (
          <MobileDiffCard result={result} sessionId={sessionId} />
        )}
      </div>
    )
  }

  if (msg.role === 'user') {
    return (
      <div className="flex justify-end px-3 py-2">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-accent/10 border border-accent/20 px-4 py-2.5">
          <p className="text-[15px] text-ink whitespace-pre-wrap leading-[1.5]">{msg.content}</p>
        </div>
      </div>
    )
  }

  // Assistant message — full-width prose
  return (
    <div className="px-4 py-3">
      <div className="flex items-center gap-1.5 mb-1.5">
        <Zap size={11} className="text-accent" />
        <span className="text-[11px] font-medium text-muted uppercase tracking-wider">SurgicalAI</span>
      </div>
      <div className="prose-mobile">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
          {msg.content || ''}
        </ReactMarkdown>
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// MobileChat — main screen
// ──────────────────────────────────────────────────────────────────────────────
export function MobileChat({ onOpenFiles }: Props) {
  const {
    activeSessions, setActiveSession, messages, addMessage, setMessages,
    isStreaming, setIsStreaming, streamingMessage, setStreamingMessage,
    streamProgress, setStreamProgress, sessions, setSessions, settings,
    sessionFiles, setSessionFiles, addSessionFile, removeSessionFile,
  } = useAppStore()

  const [input, setInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [uploadingFiles, setUploadingFiles] = useState(false)
  const [progressHistory, setProgressHistory] = useState<string[]>([])
  const [thinkingText, setThinkingText] = useState('')
  const [isThinking, setIsThinking] = useState(false)
  const [isCompacting, setIsCompacting] = useState(false)

  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingMessage, thinkingText, streamProgress])

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

  const stopStream = () => {
    setIsStreaming(false)
    setStreamingMessage('')
    setStreamProgress('')
  }

  const ensureSession = useCallback(async (): Promise<string> => {
    if (activeSessions) return activeSessions
    const s = await api.chat.createSession({ title: 'New Chat' })
    const updated = await api.chat.getSessions()
    setSessions(updated)
    setActiveSession(s.id)
    return s.id
  }, [activeSessions])

  const uploadFiles = useCallback(async (files: FileList | File[]) => {
    setUploadingFiles(true)
    const sessionId = await ensureSession()
    const arr = Array.from(files)
    const promises = arr.map(async (file) => {
      const ext = file.name.split('.').pop()?.toLowerCase() || ''
      const language = langOf(file.name)
      const isImage = IMAGE_EXTS.includes(ext)
      const isBinary = BINARY_EXTS.includes(ext)
      let uploadBody: any
      if (isImage) {
        const base64 = await new Promise<string>((res, rej) => {
          const r = new FileReader(); r.onload = () => res(r.result as string); r.onerror = rej; r.readAsDataURL(file)
        })
        uploadBody = { filename: file.name, content: '', base64_data: base64, language, file_type: 'image' }
      } else if (isBinary) {
        const base64 = await new Promise<string>((res, rej) => {
          const r = new FileReader(); r.onload = () => res(r.result as string); r.onerror = rej; r.readAsDataURL(file)
        })
        uploadBody = { filename: file.name, content: '', base64_data: base64, language, file_type: ext === 'pdf' ? 'pdf' : 'excel' }
      } else {
        const content = await file.text()
        uploadBody = { filename: file.name, content, language }
      }
      try {
        const result = await api.sessionFiles.upload(sessionId, uploadBody)
        addSessionFile(result)
        return result
      } catch (e: any) {
        toast.error(`Failed to upload ${file.name}: ${e.message}`)
        return null
      }
    })
    await Promise.all(promises)
    setUploadingFiles(false)
    toast.success(`${arr.length} file${arr.length > 1 ? 's' : ''} ready`)
  }, [activeSessions, ensureSession])

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) {
      uploadFiles(e.target.files)
      e.target.value = ''
    }
  }

  const removeFile = async (fileId: string) => {
    if (!activeSessions) return
    try { await api.sessionFiles.delete(activeSessions, fileId) } catch {}
    removeSessionFile(fileId)
  }

  // ── Send message ─────────────────────────────────────────────────────────
  const handleSend = useCallback(async () => {
    if (!input.trim() || isStreaming) return
    if (!settings?.openai_api_key_set && !(settings as any)?.anthropic_api_key_set) {
      setError('Add your API key in Settings first.')
      return
    }
    setError(null)
    const text = input.trim()
    setInput('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

    const sessionId = await ensureSession()
    const isFirstMessage = messages.length === 0

    const autoNameSession = () => {
      const title = text.replace(/\s+/g, ' ').trim().slice(0, 55) || 'New Chat'
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
    setStreamProgress('Thinking...')
    setStreamingMessage('')
    setProgressHistory(['Thinking...'])
    setThinkingText('')
    setIsThinking(false)

    let accumulated = ''
    let gotResult = false

    const ctrl = api.stream.smart(
      { session_id: sessionId, message: text, file_ids: sessionFiles.map(f => f.id) },
      (progress) => {
        setStreamProgress(progress)
        setProgressHistory(prev => prev[prev.length - 1] !== progress ? [...prev, progress] : prev)
      },
      (token) => { accumulated += token; setStreamingMessage(accumulated) },
      (result) => {
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
        if (isFirstMessage) autoNameSession()
        else api.chat.getSessions().then(setSessions).catch(() => {})
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
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
        if (isFirstMessage) autoNameSession()
        else api.chat.getSessions().then(setSessions).catch(() => {})
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
      },
      (err) => { setError(err); stopStream() },
      (txt, phase) => {
        if (phase === 'start') { setIsThinking(true); setThinkingText('') }
        else if (phase === 'delta') { setThinkingText(prev => prev + txt) }
        else if (phase === 'end') { setIsThinking(false) }
      },
      (phase) => {
        if (phase === 'start') { setIsCompacting(true) }
        else {
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
      }
    )
    abortRef.current = ctrl
  }, [input, isStreaming, settings, activeSessions, sessionFiles, messages.length])

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleSend() }
  }

  const hasFiles = sessionFiles.length > 0
  const isEmpty = messages.length === 0 && !isStreaming

  return (
    <div className="flex flex-col flex-1 min-h-0 bg-base">
      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={ACCEPT}
        className="hidden"
        onChange={handleFileInput}
      />

      {/* Files context bar — compact, tappable to open files sheet */}
      {hasFiles && (
        <button
          onClick={onOpenFiles}
          className="flex items-center gap-2 px-4 py-2 border-b border-border bg-surface/40 active:bg-surface/70 flex-shrink-0"
        >
          <Files size={14} className="text-accent" />
          <span className="text-[12px] text-ink font-medium">
            {sessionFiles.length} file{sessionFiles.length !== 1 ? 's' : ''} in context
          </span>
          <span className="ml-auto text-[11px] text-muted">Tap to view</span>
        </button>
      )}

      {/* Messages scroll area */}
      <div className="flex-1 overflow-y-auto overscroll-contain" style={{ WebkitOverflowScrolling: 'touch', isolation: 'isolate' }}>
        {isEmpty ? (
          <MobileEmptyState
            onChip={(p) => { setInput(p); setTimeout(() => textareaRef.current?.focus(), 50) }}
            onAttach={() => fileInputRef.current?.click()}
          />
        ) : (
          <div className="pb-4">
            {messages.map(m => (
              <MobileMessage key={m.id} msg={m} sessionId={activeSessions || ''} />
            ))}

            {/* Thinking */}
            {isThinking && thinkingText && (
              <div className="px-4 py-2 mx-3 my-2 rounded-xl bg-violet-500/5 border border-violet-500/20">
                <div className="flex items-center gap-1.5 mb-1">
                  <Sparkles size={11} className="text-violet-400" />
                  <span className="text-[11px] font-medium text-violet-400 uppercase tracking-wider">Thinking</span>
                </div>
                <p className="text-[13px] text-violet-300/80 leading-relaxed whitespace-pre-wrap">{thinkingText}</p>
              </div>
            )}

            {/* Progress */}
            {isStreaming && !streamingMessage && progressHistory.length > 0 && (
              <div className="px-4 py-3">
                <div className="flex items-center gap-1.5 mb-2">
                  <Zap size={11} className="text-accent" />
                  <span className="text-[11px] font-medium text-muted uppercase tracking-wider">SurgicalAI</span>
                </div>
                <ul className="space-y-1.5">
                  {progressHistory.slice(-5).map((p, i, arr) => (
                    <li
                      key={i}
                      className={`flex items-start gap-2 text-[13px] ${
                        i === arr.length - 1 ? 'text-ink' : 'text-muted'
                      }`}
                    >
                      <span className={`mt-1.5 w-1 h-1 rounded-full flex-shrink-0 ${
                        i === arr.length - 1 ? 'bg-accent animate-pulse' : 'bg-muted/50'
                      }`} />
                      <span>{p}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Streaming text */}
            {streamingMessage && (
              <div className="px-4 py-3">
                <div className="flex items-center gap-1.5 mb-1.5">
                  <Zap size={11} className="text-accent" />
                  <span className="text-[11px] font-medium text-muted uppercase tracking-wider">SurgicalAI</span>
                </div>
                <div className="prose-mobile">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                    {streamingMessage}
                  </ReactMarkdown>
                </div>
              </div>
            )}

            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {/* Error */}
      {error && (
        <div className="mx-3 mb-2 px-3 py-2 rounded-lg bg-danger/10 border border-danger/30 flex items-start gap-2">
          <X size={14} className="text-danger mt-0.5 flex-shrink-0" />
          <span className="text-[12px] text-danger flex-1">{error}</span>
          <button onClick={() => setError(null)} className="text-danger" aria-label="Dismiss">
            <X size={14} />
          </button>
        </div>
      )}

      {/* Bottom input area — owns the bottom safe-area inset (MobileLayout root does NOT set paddingBottom) */}
      <div
        className="border-t border-border bg-base flex-shrink-0"
        style={{ paddingBottom: 'max(env(safe-area-inset-bottom), 8px)' }}
      >
        {/* File chips bar */}
        {(hasFiles || uploadingFiles) && (
          <div className="flex items-center gap-1.5 px-3 pt-2 pb-1 overflow-x-auto">
            {sessionFiles.slice(0, 8).map(f => (
              <MobileFileChip key={f.id} file={f} onRemove={() => removeFile(f.id)} />
            ))}
            {sessionFiles.length > 8 && (
              <span className="text-[11px] text-muted px-2 flex-shrink-0">+{sessionFiles.length - 8}</span>
            )}
            {uploadingFiles && (
              <span className="text-[11px] text-muted px-2 animate-pulse flex-shrink-0">Uploading…</span>
            )}
          </div>
        )}

        {/* Input pill */}
        <div className="px-3 pt-2 pb-3">
          <div className="relative bg-surface border border-border rounded-2xl focus-within:border-accent/60 transition-colors">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKey}
              disabled={isCompacting}
              placeholder={
                isCompacting
                  ? 'Compacting history…'
                  : hasFiles
                    ? `Ask about your ${sessionFiles.length} file${sessionFiles.length > 1 ? 's' : ''}…`
                    : 'Ask anything, or attach files…'
              }
              rows={1}
              onInput={(e) => {
                const el = e.currentTarget
                el.style.height = 'auto'
                el.style.height = Math.min(el.scrollHeight, 160) + 'px'
              }}
              className="w-full bg-transparent text-[16px] text-ink placeholder:text-muted/70 resize-none pl-4 pr-4 pt-3 pb-12 focus:outline-none leading-relaxed min-h-[52px] max-h-[160px]"
              style={{ fontSize: '16px' /* prevent iOS zoom on focus */ }}
            />

            {/* Toolbar */}
            <div className="absolute bottom-0 left-0 right-0 flex items-center justify-between px-1.5 pb-1.5">
              <button
                onClick={() => fileInputRef.current?.click()}
                className="w-10 h-10 flex items-center justify-center rounded-xl text-muted active:bg-overlay transition-colors"
                aria-label="Attach files"
              >
                <Paperclip size={17} />
              </button>
              {isStreaming ? (
                <button
                  onClick={() => { abortRef.current?.abort(); stopStream() }}
                  className="h-9 px-4 rounded-xl bg-danger/15 text-danger text-[13px] font-semibold flex items-center gap-1.5 active:bg-danger/25"
                >
                  <X size={14} /> Stop
                </button>
              ) : (
                <button
                  onClick={handleSend}
                  disabled={!input.trim() || isCompacting}
                  className={`w-10 h-10 rounded-xl flex items-center justify-center transition-all ${
                    !input.trim() || isCompacting
                      ? 'text-faint'
                      : 'bg-accent text-white active:scale-95'
                  }`}
                  aria-label="Send"
                >
                  <Send size={16} />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
