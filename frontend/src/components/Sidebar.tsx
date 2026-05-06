import React, { useEffect, useState, useRef } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { FileNode } from '../types'
import { toast } from '../lib/toast'
import {
  Settings, MessageSquare, FolderOpen, RefreshCw, Plus,
  Search, Trash2, Pencil, Check, X, ChevronRight, ChevronDown,
  Pin, BookOpen, Zap, MoreHorizontal,
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

// ── File Browser ────────────────────────────────────
function FileBrowser() {
  const { fileTree, setFileTree, workspacePath, setWorkspacePath, settings } = useAppStore()
  const [loading, setLoading] = useState(false)
  const [pathInput, setPathInput] = useState('')
  const [search, setSearch] = useState('')

  useEffect(() => {
    if (settings?.workspace_path) {
      setWorkspacePath(settings.workspace_path)
      setPathInput(settings.workspace_path)
      loadTree(settings.workspace_path)
    }
  }, [settings?.workspace_path])

  const loadTree = async (p?: string) => {
    const target = p || workspacePath
    if (!target) return
    setLoading(true)
    try {
      const tree = await api.files.getTree(target)
      setFileTree(tree)
    } catch (e: any) {
      toast.error('Cannot load folder', e.message)
    }
    setLoading(false)
  }

  // Filter tree nodes by search text
  function filterTree(node: FileNode, q: string): FileNode | null {
    if (node.type === 'file') {
      return node.name.toLowerCase().includes(q.toLowerCase()) ? node : null
    }
    const filteredChildren = (node.children || []).map((c) => filterTree(c, q)).filter(Boolean) as FileNode[]
    if (filteredChildren.length === 0) return null
    return { ...node, children: filteredChildren }
  }

  const displayTree = search && fileTree ? filterTree(fileTree, search) : fileTree

  return (
    <div className="flex flex-col h-full">
      {/* Path input */}
      <div className="p-2 border-b border-border flex gap-1.5">
        <input
          value={pathInput}
          onChange={(e) => setPathInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && loadTree(pathInput)}
          placeholder="/path/to/project"
          className="input text-[12px] py-1.5 flex-1"
        />
        <button onClick={() => loadTree(pathInput)} className="btn-icon" title="Refresh">
          <RefreshCw size={13} className={loading ? 'spin' : ''} />
        </button>
      </div>

      {/* Search */}
      {fileTree && (
        <div className="px-2 py-1.5 border-b border-border">
          <div className="relative">
            <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter files..."
              className="input text-[12px] py-1 pl-7"
            />
            {search && (
              <button onClick={() => setSearch('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-ink">
                <X size={11} />
              </button>
            )}
          </div>
        </div>
      )}

      {/* Tree */}
      <div className="flex-1 overflow-y-auto py-1">
        {loading ? (
          <div className="flex items-center justify-center h-24 text-faint text-xs gap-2">
            <span className="spin inline-block">◌</span> Loading...
          </div>
        ) : displayTree ? (
          <FileTreeNode node={displayTree} depth={0} />
        ) : (
          <div className="flex flex-col items-center justify-center h-32 gap-2 text-faint px-4 text-center">
            <FolderOpen size={24} className="opacity-40" />
            <span className="text-xs">Enter a folder path above to browse files</span>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Sidebar Root ────────────────────────────────────
export function Sidebar() {
  const { sidebarTab, setSidebarTab, setSettingsOpen } = useAppStore()

  const tabs = [
    { id: 'files' as const, icon: FolderOpen, label: 'Files' },
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
        <button onClick={() => setSettingsOpen(true)} className="btn-icon" title="Settings (⌘,)">
          <Settings size={15} />
        </button>
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
        {sidebarTab === 'files' && <FileBrowser />}
        {sidebarTab === 'sessions' && <SessionList />}
        {sidebarTab === 'context' && <ContextPanel />}
      </div>
    </div>
  )
}
