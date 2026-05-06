import React, { useEffect, useState, useRef } from 'react'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { api } from '../api/client'
import { FileNode } from '../types'
import { toast } from '../lib/toast'
import {
  Settings, MessageSquare, FolderOpen, RefreshCw, Plus,
  Search, Trash2, Pencil, Check, X, ChevronRight, ChevronDown,
  Pin, BookOpen, Zap, MoreHorizontal, Upload, FileCode, File, LogOut,
} from 'lucide-react'
import { ContextPanel } from './ContextPanel'

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
        <div className="text-[11px] text-faint mt-0.5">{session.message_count} msgs · {session.model || 'gpt-4.1'}</div>
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

  const deleteSession = async (id: string) => {
    try {
      await api.chat.deleteSession(id)
      if (activeSessions === id) { setActiveSession(null); setMessages([]) }
      await loadSessions()
      toast.success('Chat deleted')
    } catch (e: any) {
      toast.error('Delete failed', e.message)
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

  return (
    <div className="flex flex-col h-full">
      <div className="p-2 border-b border-border">
        <button onClick={newSession} className="btn-primary w-full gap-2">
          <Plus size={14} /> New Chat
          <kbd className="ml-auto text-[10px] opacity-50 font-mono bg-accent-dark/30 px-1 rounded">⌘N</kbd>
        </button>
      </div>
      <div className="flex-1 overflow-y-auto py-1">
        {sessions.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-32 gap-2 text-faint">
            <MessageSquare size={24} className="opacity-40" />
            <span className="text-xs">No chats yet</span>
          </div>
        ) : (
          sessions.map((s) => (
            <SessionItem
              key={s.id}
              session={s}
              active={activeSessions === s.id}
              onLoad={() => loadSession(s.id)}
              onDelete={() => deleteSession(s.id)}
              onRename={(title) => renameSession(s.id, title)}
            />
          ))
        )}
      </div>
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
            <div className="w-12 h-12 rounded-2xl bg-zinc-800 flex items-center justify-center">
              <FileCode size={22} className="text-zinc-500" />
            </div>
            <div>
              <p className="text-[13px] font-semibold text-zinc-400">No files in this chat</p>
              <p className="text-[11px] text-zinc-600 mt-1 leading-relaxed">
                Drop files into the chat or click <strong className="text-zinc-500">Add</strong> above to get surgical edits
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

export function Sidebar() {
  const { sidebarTab, setSidebarTab, setSettingsOpen } = useAppStore()

  const tabs = [
    { id: 'files' as const, icon: FileCode, label: 'Files' },
    { id: 'sessions' as const, icon: MessageSquare, label: 'Chats' },
    { id: 'context' as const, icon: Pin, label: 'Context' },
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
      </div>
    </div>
  )
}
