import React, { useState, useRef, useEffect, useCallback, Component } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { InlineDiffCard } from './InlineDiffCard'
import { MarkdownCode } from './CodeBlock'
import {
  Send, X, Plus, Paperclip, FileCode, AlertTriangle, Zap, Trash2, Brain
} from 'lucide-react'
import type { SessionFile, SmartResult } from '../types'

// ── Markdown component overrides ──────────────────────────
const mdComponents = {
  code: MarkdownCode as any,
  // Beautiful prose overrides
  h1: ({ children }: any) => (
    <h1 className="text-lg font-bold text-zinc-100 mt-5 mb-2 border-b border-zinc-700/50 pb-1.5">{children}</h1>
  ),
  h2: ({ children }: any) => (
    <h2 className="text-base font-bold text-zinc-100 mt-4 mb-1.5">{children}</h2>
  ),
  h3: ({ children }: any) => (
    <h3 className="text-sm font-semibold text-zinc-200 mt-3 mb-1">{children}</h3>
  ),
  p: ({ children }: any) => (
    <p className="text-sm text-zinc-300 leading-7 mb-3 last:mb-0">{children}</p>
  ),
  ul: ({ children }: any) => (
    <ul className="my-2 space-y-1 pl-1">{children}</ul>
  ),
  ol: ({ children }: any) => (
    <ol className="my-2 space-y-1 pl-1 list-decimal list-inside">{children}</ol>
  ),
  li: ({ children }: any) => (
    <li className="flex items-start gap-2 text-sm text-zinc-300 leading-6">
      <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-blue-400/70 flex-shrink-0 list-none" />
      <span>{children}</span>
    </li>
  ),
  blockquote: ({ children }: any) => (
    <blockquote className="my-3 pl-4 border-l-2 border-blue-500/50 text-zinc-400 italic text-sm leading-6">{children}</blockquote>
  ),
  strong: ({ children }: any) => (
    <strong className="font-semibold text-zinc-100">{children}</strong>
  ),
  em: ({ children }: any) => (
    <em className="text-zinc-300 italic">{children}</em>
  ),
  a: ({ children, href }: any) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:text-blue-300 underline underline-offset-2 transition-colors">{children}</a>
  ),
  hr: () => <hr className="my-4 border-zinc-700/50" />,
  table: ({ children }: any) => (
    <div className="my-3 overflow-x-auto rounded-lg border border-zinc-700/60">
      <table className="w-full text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }: any) => <thead className="bg-zinc-800/80">{children}</thead>,
  th: ({ children }: any) => <th className="px-3 py-2 text-left text-xs font-semibold text-zinc-300 uppercase tracking-wide">{children}</th>,
  td: ({ children }: any) => <td className="px-3 py-2 text-zinc-400 text-sm border-t border-zinc-700/40">{children}</td>,
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
    <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-blue-500 to-violet-600 flex items-center justify-center flex-shrink-0 shadow-md shadow-blue-500/20">
      <Zap size={14} className="text-white" />
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
        <div className="border border-red-500/40 rounded-xl p-4 bg-red-500/10 text-sm text-red-300">
          <strong>Diff card error:</strong> {this.state.error}
          <br /><span className="text-xs text-red-400 mt-1 block">The change was planned but could not be displayed. Check the console for details.</span>
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
  const time = msg.created_at
    ? new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : ''

  let surgicalResult: SmartResult | null = null
  if (isSurgical && msg.surgical_data) {
    try { surgicalResult = JSON.parse(msg.surgical_data) } catch {}
  }

  // ── User bubble (right-aligned) ──
  if (isUser) {
    return (
      <div className="flex justify-end px-4 py-3 group">
        <div className="max-w-[78%]">
          <div className="flex items-center justify-end gap-2 mb-1">
            <span className="text-[10px] text-zinc-600 opacity-0 group-hover:opacity-100 transition-opacity">{time}</span>
            <span className="text-[11px] font-medium text-zinc-500">You</span>
          </div>
          <div className="bg-zinc-700/60 border border-zinc-600/40 rounded-2xl rounded-tr-sm px-4 py-2.5 text-sm text-zinc-100 leading-relaxed whitespace-pre-wrap shadow-sm">
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
          <span className="text-[12px] font-semibold text-zinc-300">SurgicalAI</span>
          <span className="text-[10px] text-zinc-600 opacity-0 group-hover:opacity-100 transition-opacity">{time}</span>
        </div>

        {isSurgical && surgicalResult ? (
          <DiffCardBoundary>
            <InlineDiffCard result={surgicalResult} sessionId={sessionId} />
          </DiffCardBoundary>
        ) : (
          <div className="prose-ai">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
              {msg.content}
            </ReactMarkdown>
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
  }, [isStreaming])

  if (!text && !isStreaming) return null

  return (
    <div className="mb-3">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-xs text-violet-400 hover:text-violet-300 transition-colors"
      >
        <span className={`transform transition-transform duration-200 ${expanded ? 'rotate-90' : ''}`}>▶</span>
        <Brain size={12} />
        {isStreaming ? (
          <span className="flex items-center gap-1.5">
            Thinking<span className="animate-pulse">…</span>
          </span>
        ) : (
          <span>Claude's reasoning ({text.length > 1000 ? `${Math.round(text.length / 100)} steps` : 'click to view'})</span>
        )}
      </button>
      {expanded && text && (
        <div className="mt-2 ml-5 pl-3 border-l-2 border-violet-500/30 text-[12px] text-zinc-400/90 whitespace-pre-wrap max-h-80 overflow-y-auto leading-relaxed font-mono">
          {text}
          {isStreaming && <span className="inline-block w-1.5 h-3 bg-violet-400/60 rounded-sm ml-0.5 animate-pulse" />}
        </div>
      )}
    </div>
  )
}

function StreamingBubble({ content, progress, progressHistory, thinkingText, isThinking }: { content: string; progress: string; progressHistory: string[]; thinkingText?: string; isThinking?: boolean }) {
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
      <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-blue-500 to-violet-600 flex items-center justify-center flex-shrink-0 shadow-md shadow-blue-500/20 animate-pulse">
        <Zap size={14} className="text-white" />
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-[12px] font-semibold text-zinc-300">SurgicalAI</span>
          {/* Current progress badge */}
          {progress && (
            <span className="text-[11px] text-blue-400 flex items-center gap-1.5 bg-blue-500/10 px-2 py-0.5 rounded-full border border-blue-500/20">
              <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse" />
              {progress}
            </span>
          )}
          {/* Elapsed timer */}
          {elapsed > 0 && !content && (
            <span className="text-[10px] text-zinc-500 tabular-nums">{elapsed}s</span>
          )}
        </div>

        {/* Collapsible thinking trail */}
        {hasSteps && !content && (
          <div className="mb-2">
            <button
              onClick={() => setThinkingExpanded(e => !e)}
              className="text-[11px] text-zinc-500 hover:text-zinc-300 flex items-center gap-1 transition-colors"
            >
              <span>{thinkingExpanded ? '▾' : '▸'}</span>
              <span>{completedSteps.length} step{completedSteps.length !== 1 ? 's' : ''} completed</span>
            </button>
            {thinkingExpanded && (
              <div className="mt-1.5 pl-3 border-l-2 border-zinc-700/60 space-y-1">
                {completedSteps.map((step, i) => (
                  <div key={i} className="text-[11px] text-zinc-500 flex items-center gap-1.5">
                    <span className="text-green-400/80">✓</span>
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

        {content ? (
          <div className="prose-ai">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
              {content}
            </ReactMarkdown>
          </div>
        ) : null}

        {/* Blinking cursor */}
        <span className="inline-block w-2 h-[1.1em] bg-blue-400/80 rounded-sm align-text-bottom ml-0.5 animate-pulse" />
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
    python: 'text-blue-400', typescript: 'text-cyan-400', javascript: 'text-yellow-400',
    go: 'text-cyan-300', rust: 'text-orange-400', java: 'text-red-400',
  }
  const color = langColors[file.language] || 'text-zinc-400'
  const emojiIcon = getFileIcon(file)

  return (
    <div className="flex items-center gap-1.5 px-2.5 py-1 bg-zinc-800 border border-zinc-700 rounded-lg group">
      {emojiIcon ? (
        <span className="text-[11px] leading-none">{emojiIcon}</span>
      ) : (
        <FileCode size={11} className={color} />
      )}
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
  const [showAllFiles, setShowAllFiles] = useState(false)
  const [filesCollapsed, setFilesCollapsed] = useState(false)
  const [progressHistory, setProgressHistory] = useState<string[]>([])
  const [thinkingText, setThinkingText] = useState('')
  const [isThinking, setIsThinking] = useState(false)
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
      const language = getLanguage(file.name)
      const ext = file.name.split('.').pop()?.toLowerCase() || ''
      const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'svg']
      const BINARY_EXTS = ['pdf', 'xlsx', 'xls']
      const isImage = IMAGE_EXTS.includes(ext)
      const isBinary = BINARY_EXTS.includes(ext)

      let uploadBody: { filename: string; content: string; language?: string; base64_data?: string; file_type?: string }

      if (isImage) {
        // Read as base64 data URL
        const base64Data = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader()
          reader.onload = () => resolve(reader.result as string)
          reader.onerror = reject
          reader.readAsDataURL(file)
        })
        uploadBody = { filename: file.name, content: '', base64_data: base64Data, language, file_type: 'image' }
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
        // Text / code — existing behavior
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
    setInput('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

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
    setProgressHistory(['Thinking...'])
    setThinkingText('')
    setIsThinking(false)

    let accumulated = ''
    let gotResult = false

    const ctrl = api.stream.smart(
      { session_id: sessionId, message: text, file_ids: sessionFiles.map(f => f.id) },
      (progress) => {
        setStreamProgress(progress)
        setProgressHistory(prev => {
          if (prev[prev.length - 1] !== progress) return [...prev, progress]
          return prev
        })
      },
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
        // Re-fetch session files to keep sidebar in sync
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
        api.chat.getSessions().then(setSessions).catch(() => {})
        // Re-fetch session files to keep sidebar in sync
        api.sessionFiles.list(sessionId).then(setSessionFiles).catch(() => {})
      },
      (err) => { setError(err); stopStream() },
      // onThinking — Claude's extended thinking
      (text, phase) => {
        if (phase === 'start') { setIsThinking(true); setThinkingText('') }
        else if (phase === 'delta') { setThinkingText(prev => prev + text) }
        else if (phase === 'end') { setIsThinking(false) }
      }
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
        accept=".py,.js,.ts,.tsx,.jsx,.go,.rs,.java,.cs,.rb,.php,.swift,.kt,.html,.css,.json,.yaml,.yml,.toml,.md,.sh,.sql,.cpp,.c,.h,.png,.jpg,.jpeg,.webp,.gif,.bmp,.pdf,.csv,.xlsx,.xls,.txt,.zip"
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
          <div className="py-2">
            {messages.map((msg, i) => (
              <Message key={msg.id || i} msg={msg} sessionId={activeSessions || ''} />
            ))}
            {isStreaming && (streamingMessage || streamProgress) && (
              <StreamingBubble content={streamingMessage} progress={streamProgress} progressHistory={progressHistory} thinkingText={thinkingText} isThinking={isThinking} />
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
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input area */}
      <div className="border-t border-zinc-800 p-3 flex-shrink-0 bg-zinc-900/50">
        {/* File chips */}
        {hasFiles && (
          <div className="mb-2.5">
            {/* Collapse toggle row */}
            <button
              onClick={() => setFilesCollapsed(c => !c)}
              className="flex items-center gap-1.5 mb-1.5 text-[11px] text-zinc-500 hover:text-zinc-300 transition-colors select-none"
            >
              <span>{filesCollapsed ? '▸' : '▾'}</span>
              <span>{sessionFiles.length} file{sessionFiles.length !== 1 ? 's' : ''} in context</span>
            </button>
            {/* Chips — hidden when collapsed */}
            {!filesCollapsed && (
              <div className="flex flex-wrap gap-1.5">
                {(showAllFiles ? sessionFiles : sessionFiles.slice(0, 5)).map(file => (
                  <FileChip
                    key={file.id}
                    file={file}
                    onRemove={() => removeFile(file.id)}
                  />
                ))}
                {sessionFiles.length > 5 && !showAllFiles && (
                  <button
                    onClick={() => setShowAllFiles(true)}
                    className="flex items-center gap-1 px-2.5 py-1 bg-zinc-800/60 border border-zinc-700 rounded-lg text-[11px] text-zinc-400 hover:text-zinc-200 hover:border-zinc-600 transition-colors"
                  >
                    +{sessionFiles.length - 5} more
                  </button>
                )}
                {showAllFiles && sessionFiles.length > 5 && (
                  <button
                    onClick={() => setShowAllFiles(false)}
                    className="flex items-center gap-1 px-2.5 py-1 bg-zinc-800/60 border border-zinc-700 rounded-lg text-[11px] text-zinc-400 hover:text-zinc-200 hover:border-zinc-600 transition-colors"
                  >
                    show less
                  </button>
                )}
                {uploadingFiles && (
                  <div className="flex items-center gap-1.5 px-2.5 py-1 bg-zinc-800/60 border border-zinc-700 rounded-lg">
                    <span className="text-[11px] text-zinc-500 animate-pulse">Uploading...</span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Textarea + buttons */}
        <div className="flex gap-2 items-end">
          <button
            onClick={() => fileInputRef.current?.click()}
            className="h-[44px] w-[44px] rounded-xl bg-zinc-800 border border-zinc-700 text-zinc-400 hover:text-zinc-200 hover:border-zinc-600 transition-colors flex-shrink-0 flex items-center justify-center"
            title="Attach files"
          >
            <Paperclip size={16} />
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
              rows={1}
              onInput={(e) => {
                const el = e.currentTarget
                el.style.height = 'auto'
                el.style.height = Math.min(el.scrollHeight, 200) + 'px'
              }}
              className="w-full bg-zinc-800 border border-zinc-700 rounded-xl text-sm text-zinc-200 placeholder:text-zinc-500 resize-none px-3 py-2.5 focus:outline-none focus:border-blue-500/60 leading-relaxed font-[inherit] min-h-[44px] max-h-[200px] overflow-y-auto"
            />
          </div>

          {isStreaming ? (
            <button
              onClick={() => { abortRef.current?.abort(); stopStream() }}
              className="h-[44px] px-4 rounded-xl bg-red-500/20 border border-red-500/30 text-red-400 font-bold text-sm flex items-center gap-1.5 hover:bg-red-500/30 transition-colors flex-shrink-0"
            >
              <X size={14} /> Stop
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!input.trim()}
              className={`h-[44px] w-[44px] rounded-xl border font-bold text-sm flex items-center justify-center transition-all flex-shrink-0 ${
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
