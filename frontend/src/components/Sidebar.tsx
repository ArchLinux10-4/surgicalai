import React, { useEffect, useState, useRef } from 'react'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { api } from '../api/client'
import { FileNode } from '../types'
import { toast } from '../lib/toast'
import {
  Settings, MessageSquare, FolderOpen, RefreshCw, Plus,
  Search, Trash2, Pencil, Check, X, ChevronRight, ChevronDown,
  Pin, BookOpen, Zap, MoreHorizontal, Upload, FileCode, File, LogOut, Sun, Moon, Github,
} from 'lucide-react'
import { ContextPanel } from './ContextPanel'
import { GitHubPanel } from './GitHubPanel'
import { useThemeStore } from '../stores/themeStore'

// ── File icon helper ────────────────────────────────
const FILE_ICONS: Record<string, string> = {
  '.py': '🐍', '.js': '🟨', '.ts': '🔷', '.tsx': '⚛️', '.jsx': '⚛️',
  '.go': '🐹', '.rs': '🦀', '.java': '☕', '.cs': '💜', '.cpp': '🔵', '.c': '🔵',
  '.html': '🌐', '.css': '🎨', '.scss': '🎨', '.json': '📋', '.md': '📝',
  '.sh': '⚡', '.bash': '⚡', '.sql': '🗄️', '.yaml': '⚙️', '.yml': '⚙️',
  '.toml': '⚙️', '.env': '🔑', '.gitignore': '🙈', '.lock': '🔒',
}
function fileIcon(node: FileNode) {
  if (node.type === 'dir') return null
  return FILE_ICONS[node.extension || ''] || '📄'
}

// ── File Tree Node ──────────────────────────────────
function FileTreeNode({ node, depth = 0 }: { node: FileNode; depth?: number }) {
  const [expanded, setExpanded] = useState(depth < 1)
  const { setActiveFile, setRightTab } = useAppStore()
  const isDir = node.type === 'dir'
  const indent = depth * 14

  const handleClick = async () => {
    if (isDir) { setExpanded(!expanded); return }
    try {
      const file = await api.files.read(node.path)
      setActiveFile(file)
      setRightTab('editor')
    } catch (e: any) {
      toast.error('Cannot open file', e.message)
    }
  }

  return (
    <div>
      <div
        onClick={handleClick}
        className="flex items-center gap-1.5 py-[3px] pr-2 rounded-md text-[13px] cursor-pointer select-none group transition-colors hover:bg-overlay"
        style={{ paddingLeft: `${8 + indent}px` }}
      >
        {isDir ? (
          <span className="text-faint w-3 flex-shrink-0">
            {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          </span>
        ) : (
          <span className="w-3 flex-shrink-0" />
        )}
        <span className="text-[11px] flex-shrink-0">{isDir ? (expanded ? '📂' : '📁') : fileIcon(node)}</span>
        <span className={`truncate ${isDir ? 'text-accent font-medium' : 'text-ink'}`}>{node.name}</span>
        {node.size && node.size > 50000 && (
          <span className="ml-auto text-[10px] text-faint flex-shrink-0">{Math.round(node.size / 1024)}k</span>
        )}
      </div>
      {isDir && expanded && node.children?.map((child) => (
        <FileTreeNode key={child.path} node={child} depth={depth + 1} />
      ))}
    </div>
  )
}

// ── Timestamp helper ────────────────────────────────
function relativeTime(iso: string): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 7) return days === 1 ? 'yesterday' : `${days}d ago`
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

// ── Session Item ────────────────────────────────────
function SessionItem({ session, active, onLoad, onDelete, onRename }: {
  session: any; active: boolean
  onLoad: () => void; onDelete: () => void; onRename: (title: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(session.title)
  const [showMenu, setShowMenu] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { if (editing) inputRef.current?.focus() }, [editing])

  const commitRename = () => {
    if (draft.trim() && draft !== session.title) onRename(draft.trim())
    setEditing(false)
  }

  return (
    <div
      className={`group relative flex items-start gap-2.5 px-3 py-2.5 cursor-pointer border-l-2 transition-all ${
        active
          ? 'bg-overlay border-accent text-ink'
          : 'border-transparent hover:bg-overlay/60 text-muted hover:text-ink'
      }`}
      onClick={() => !editing && onLoad()}
    >
      <MessageSquare size={13} className={`flex-shrink-0 mt-0.5 ${active ? 'text-accent' : 'text-faint'}`} />
      <div className="flex-1 min-w-0">
        {editing ? (
          <input
            ref={inputRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') commitRename(); if (e.key === 'Escape') setEditing(false) }}
            onBlur={commitRename}
            onClick={(e) => e.stopPropagation()}
            className="w-full bg-base border border-accent/50 text-ink text-[13px] rounded px-1.5 py-0.5 outline-none"
          />
        ) : (
          <div className="text-[13px] font-medium truncate leading-snug">{session.title}</div>
        )}
        <div className="text-[11px] text-faint mt-0.5 flex items-center gap-1.5">
          <span>{session.message_count} msgs</span>
          <span className="opacity-40">·</span>
          <span>{relativeTime(session.created_at)}</span>
        </div>
      </div>

      {/* Hover actions */}
      {!editing && (
        <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
          <button
            onClick={(e) => { e.stopPropagation(); setEditing(true) }}
            className="btn-icon w-6 h-6" title="Rename"
          ><Pencil size={11} /></button>
          <button
            onClick={(e) => { e.stopPropagation(); onDelete() }}
            className="btn-icon w-6 h-6 hover:text-danger" title="Delete"
          ><Trash2 size={11} /></button>
        </div>
      )}
    </div>
  )
}

// ── Session List ────────────────────────────────────
function SessionList() {
  const { sessions, setSessions, activeSessions, setActiveSession, setMessages } = useAppStore()
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<any[]>([])
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)
  const [pendingDeleteFileCount, setPendingDeleteFileCount] = useState(0)
  const [deleteConfirmLoading, setDeleteConfirmLoading] = useState(false)

  const loadSessions = () => api.chat.getSessions().then(setSessions).catch(() => {})
  useEffect(() => { loadSessions() }, [])

  const newSession = async () => {
    try {
      const s = await api.chat.createSession({ title: 'New Chat' })
      await loadSessions()
      setActiveSession(s.id)
      setMessages([])
    } catch (e: any) {
      toast.error('Failed to create chat', e.message)
    }
  }

  const loadSession = async (id: string) => {
    setActiveSession(id)
    try {
      const msgs = await api.chat.getMessages(id)
      setMessages(msgs)
    } catch { setMessages([]) }
  }

  const promptDeleteSession = async (id: string) => {
    // Fetch file count so we can warn the user
    try {
      const files = await api.sessionFiles.list(id)
      setPendingDeleteFileCount(files.length)
    } catch {
      setPendingDeleteFileCount(0)
    }
    setPendingDeleteId(id)
  }

  const confirmDeleteSession = async () => {
    if (!pendingDeleteId) return
    setDeleteConfirmLoading(true)
    try {
      await api.chat.deleteSession(pendingDeleteId)
      if (activeSessions === pendingDeleteId) { setActiveSession(null); setMessages([]) }
      await loadSessions()
      toast.success('Chat deleted')
    } catch (e: any) {
      toast.error('Delete failed', e.message)
    } finally {
      setDeleteConfirmLoading(false)
      setPendingDeleteId(null)
      setPendingDeleteFileCount(0)
    }
  }

  const renameSession = async (id: string, title: string) => {
    try {
      await api.chat.renameSession(id, title)
      await loadSessions()
    } catch (e: any) {
      toast.error('Rename failed', e.message)
    }
  }

  useEffect(() => {
    if (searchQuery.trim() === '') {
      setSearchResults([])
      return
    }
    const timer = setTimeout(async () => {
      try {
        const response = await api.chat.search(searchQuery)
        const data = response as any
        setSearchResults(data.results ?? data ?? [])
      } catch {
        setSearchResults([])
      }
    }, 400)
    return () => clearTimeout(timer)
  }, [searchQuery])

  const filteredSessions = searchQuery.trim()
    ? sessions.filter(s => (s.title || '').toLowerCase().includes(searchQuery.toLowerCase()))
    : sessions

  return (
    <div className="flex flex-col h-full">
      <div className="p-2 border-b border-border">
        <button onClick={newSession} className="btn-primary w-full gap-2">
          <Plus size={14} /> New Chat
          <kbd className="ml-auto text-[10px] opacity-50 font-mono bg-accent-dark/30 px-1 rounded">⌘N</kbd>
        </button>
      </div>
      <div className="relative mb-2 px-2">
        <span className="absolute left-4 top-1/2 -translate-y-1/2 text-muted-foreground">
          <Search size={14} className="opacity-60" />
        </span>
        <input
          type="text"
          placeholder="Search sessions..."
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          className="w-full pl-9 pr-8 py-1 text-sm rounded border bg-background border-border focus:outline-none"
        />
        {searchQuery && (
          <button
            type="button"
            className="absolute right-4 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            onClick={() => setSearchQuery('')}
            tabIndex={-1}
          >
            {/* Use X icon from lucide-react if available, else × */}
            <X size={14} />
          </button>
        )}
      </div>
      <div className="flex-1 overflow-y-auto py-1">
        {searchQuery.trim() !== '' && filteredSessions.length === 0 && searchResults.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-20 gap-2 text-faint">
            <span className="text-xs">No matches</span>
          </div>
        ) : filteredSessions.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-32 gap-2 text-faint">
            <MessageSquare size={24} className="opacity-40" />
            <span className="text-xs">No chats yet</span>
          </div>
        ) : (
          filteredSessions.map((s) => (
            <SessionItem
              key={s.id}
              session={s}
              active={activeSessions === s.id}
              onLoad={() => loadSession(s.id)}
              onDelete={() => promptDeleteSession(s.id)}
              onRename={(title) => renameSession(s.id, title)}
            />
          ))
        )}
      </div>
      {searchResults.length > 0 && (
        <div className="px-2 pb-2">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1 font-semibold">Message matches</div>
          <div className="flex flex-col gap-1">
            {searchResults.map((result, index) => (
              <button
                key={result.message_id ?? result.id ?? index}
                type="button"
                className="text-left w-full px-2 py-1 rounded hover:bg-accent transition group"
                onClick={() => {
                  setActiveSession(result.session_id ?? result.id)
                  setMessages([])
                }}
              >
                <div className="text-sm font-normal truncate">{result.session_name ?? result.name ?? 'Untitled'}</div>
                <div className="text-xs text-muted-foreground line-clamp-2">{result.content_snippet ?? result.snippet ?? ''}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── Delete Confirmation Modal ── */}
      {pendingDeleteId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-surface border border-border rounded-2xl shadow-2xl shadow-black/40 p-6 w-full max-w-sm mx-4">
            <div className="flex items-start gap-3 mb-4">
              <div className="shrink-0 w-10 h-10 rounded-full bg-red-500/15 border border-red-500/25 flex items-center justify-center">
                <svg className="w-5 h-5 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                </svg>
              </div>
              <div>
                <h3 className="text-[15px] font-semibold text-ink mb-1">Delete this chat?</h3>
                <p className="text-[13px] text-muted leading-relaxed">
                  This will permanently delete all chat history
                  {pendingDeleteFileCount > 0 && (
                    <> and <span className="text-red-400 font-medium">{pendingDeleteFileCount} file{pendingDeleteFileCount !== 1 ? 's' : ''}</span> attached to this session</>
                  )}.
                  {' '}This cannot be undone.
                </p>
              </div>
            </div>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => { setPendingDeleteId(null); setPendingDeleteFileCount(0) }}
                className="px-4 py-2 rounded-lg text-[13px] font-medium text-muted border border-border hover:bg-surface-alt transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={confirmDeleteSession}
                disabled={deleteConfirmLoading}
                className="px-4 py-2 rounded-lg text-[13px] font-semibold bg-red-500/90 hover:bg-red-500 text-white border border-red-500/50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {deleteConfirmLoading ? 'Deleting…' : 'Delete Chat'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Session Files Panel ──────────────────────────────
const FILE_TYPE_ICONS: Record<string, string> = {
  '.py': '🐍', '.js': '🟨', '.ts': '🔷', '.tsx': '⚛️', '.jsx': '⚛️',
  '.go': '🐹', '.rs': '🦀', '.java': '☕', '.cs': '💜', '.cpp': '🔵', '.c': '🔵',
  '.html': '🌐', '.css': '🎨', '.scss': '🎨', '.json': '📋', '.md': '📝',
  '.sh': '⚡', '.sql': '🗄️', '.yaml': '⚙️', '.yml': '⚙️', '.toml': '⚙️',
}
function getFileIcon(filename: string) {
  const ext = '.' + filename.split('.').pop()?.toLowerCase()
  return FILE_TYPE_ICONS[ext] || '📄'
}
function fmtSize(bytes: number) {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`
}

function SessionFilesPanel() {
  const { sessionFiles, removeSessionFile } = useAppStore()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const { addSessionFile } = useAppStore()

  const handleUpload = (files: FileList | null) => {
    if (!files) return
    Array.from(files).forEach(file => {
      const reader = new FileReader()
      reader.onload = (e) => {
        const content = e.target?.result as string
        const lineCount = content.split('\n').length
        addSessionFile({
          id: `${file.name}-${Date.now()}`,
          session_id: '',
          filename: file.name,
          language: file.name.split('.').pop() || 'text',
          lines: lineCount,
          symbol_count: 0,
          created_at: new Date().toISOString(),
          content,
        })
      }
      reader.readAsText(file)
    })
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header row */}
      <div className="px-3 py-2.5 border-b border-border flex items-center justify-between">
        <span className="text-[11px] font-semibold text-muted uppercase tracking-wider">
          {sessionFiles.length > 0 ? `${sessionFiles.length} file${sessionFiles.length > 1 ? 's' : ''} in chat` : 'No files yet'}
        </span>
        <button
          onClick={() => fileInputRef.current?.click()}
          className="flex items-center gap-1 px-2 py-1 rounded-lg bg-accent/10 text-accent text-[11px] font-semibold hover:bg-accent/20 transition-colors"
          title="Upload files"
        >
          <Upload size={11} /> Add
        </button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".py,.js,.ts,.tsx,.jsx,.go,.rs,.java,.cs,.cpp,.c,.h,.html,.css,.scss,.json,.md,.sh,.sql,.yaml,.yml,.toml,.txt,.env,.rb,.php,.swift,.kt"
          className="hidden"
          onChange={(e) => handleUpload(e.target.files)}
        />
      </div>

      {/* File list */}
      <div className="flex-1 overflow-y-auto py-1">
        {sessionFiles.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 px-4 text-center pb-8">
            <div className="w-12 h-12 rounded-2xl bg-surface flex items-center justify-center">
              <FileCode size={22} className="text-muted/70" />
            </div>
            <div>
              <p className="text-[13px] font-semibold text-muted">No files in this chat</p>
              <p className="text-[11px] text-faint mt-1 leading-relaxed">
                Drop files into the chat or click <strong className="text-muted/70">Add</strong> above to get surgical edits
              </p>
            </div>
            <button
              onClick={() => fileInputRef.current?.click()}
              className="mt-1 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent/10 text-accent text-[12px] font-semibold hover:bg-accent/20 transition-colors border border-accent/20"
            >
              <Upload size={12} /> Upload files
            </button>
          </div>
        ) : (
          <div className="px-2 py-1 space-y-0.5">
            {sessionFiles.map(file => (
              <div
                key={file.id}
                className="group flex items-center gap-2 px-2.5 py-2 rounded-lg hover:bg-overlay transition-colors"
              >
                <span className="text-[14px] flex-shrink-0">{getFileIcon(file.filename)}</span>
                <div className="flex-1 min-w-0">
                  <p className="text-[12px] font-medium text-ink truncate">{file.filename}</p>
                  {file.lines > 0 && (
                    <p className="text-[10px] text-faint">{file.lines} lines{file.symbol_count > 0 ? ` · ${file.symbol_count} symbols` : ''}</p>
                  )}
                </div>
                <button
                  onClick={() => removeSessionFile(file.id)}
                  className="opacity-0 group-hover:opacity-100 text-faint hover:text-red-400 transition-all flex-shrink-0"
                  title="Remove from chat"
                >
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Sidebar Root ────────────────────────────────────
// ── User Menu (avatar + logout) ─────────────────────
function UserMenu() {
  const { user, logout } = useAuthStore()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  if (!user) return null

  const initials = user.username.slice(0, 2).toUpperCase()

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="w-7 h-7 rounded-full bg-indigo-600 hover:bg-indigo-500 flex items-center justify-center text-white text-[11px] font-bold transition"
        title={user.username}
      >
        {initials}
      </button>
      {open && (
        <div className="absolute right-0 top-9 w-48 bg-surface border border-border rounded-xl shadow-xl z-50 py-1">
          <div className="px-3 py-2 border-b border-border">
            <p className="text-xs font-semibold text-ink truncate">{user.username}</p>
            {user.email && <p className="text-[11px] text-faint truncate">{user.email}</p>}
            {user.is_admin && (
              <span className="inline-block mt-0.5 text-[10px] px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-400 font-medium">Admin</span>
            )}
          </div>
          <button
            onClick={() => { logout(); setOpen(false) }}
            className="w-full flex items-center gap-2 px-3 py-2 text-xs text-red-400 hover:bg-overlay transition"
          >
            <LogOut size={13} />
            Sign out
          </button>
        </div>
      )}
    </div>
  )
}

function ThemeToggleBtn() {
  const { theme, toggleTheme } = useThemeStore()
  return (
    <button
      onClick={toggleTheme}
      className="btn-icon"
      title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
    >
      {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
    </button>
  )
}

export function Sidebar() {
  const { sidebarTab, setSidebarTab, setSettingsOpen, sessionFiles, activeSessions } = useAppStore()

  // Auto-switch to FILES tab when loading a chat that has files
  useEffect(() => {
    if (activeSessions && sessionFiles.length > 0 && sidebarTab === 'sessions') {
      setSidebarTab('files')
    }
  }, [activeSessions])

  const fileCount = sessionFiles.length
  const tabs = [
    { id: 'files' as const, icon: FileCode, label: fileCount > 0 ? `Files (${fileCount})` : 'Files' },
    { id: 'sessions' as const, icon: MessageSquare, label: 'Chats' },
    { id: 'context' as const, icon: Pin, label: 'Pinned' },
    { id: 'github' as const, icon: Github, label: 'GitHub' },
  ]

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2">
          <Zap size={16} className="text-accent" />
          <span className="text-sm font-bold text-ink tracking-tight">SurgicalAI</span>
        </div>
        <div className="flex items-center gap-1">
          <ThemeToggleBtn />
          <button onClick={() => setSettingsOpen(true)} className="btn-icon" title="Settings (⌘,)">
            <Settings size={15} />
          </button>
          <UserMenu />
        </div>
      </div>

      {/* Tab switcher */}
      <div className="flex border-b border-border">
        {tabs.map(({ id, icon: Icon, label }) => (
          <button
            key={id}
            onClick={() => setSidebarTab(id)}
            className={`flex-1 flex items-center justify-center gap-1 py-2 text-[11px] font-semibold uppercase tracking-wide border-b-2 transition-colors ${
              sidebarTab === id
                ? 'border-accent text-ink'
                : 'border-transparent text-faint hover:text-muted'
            }`}
          >
            <Icon size={11} />
            {label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden flex flex-col min-h-0">
        {sidebarTab === 'files' && <SessionFilesPanel />}
        {sidebarTab === 'sessions' && <SessionList />}
        {sidebarTab === 'context' && <ContextPanel />}
        {sidebarTab === 'github' && <GitHubPanel onOpenSettings={() => { setSidebarTab('sessions'); setSettingsOpen(true) }} />}
      </div>
    </div>
  )
}
