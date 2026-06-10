import React, { useEffect, useState, useRef } from 'react'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { api } from '../api/client'
import { FileNode, SessionFile } from '../types'
import { toast } from '../lib/toast'
import { ContextPanel } from './ContextPanel'
import { GitHubPanel } from './GitHubPanel'
import { LinearPanel } from './LinearPanel'
import { VercelPanel } from './VercelPanel'
import { RailwayPanel } from './RailwayPanel'
import { useThemeStore } from '../stores/themeStore'
import { Add, Chat, Close, Code, DarkMode, Delete, Description, Download, Edit, FileUpload, GitHub, KeyboardArrowDown, KeyboardArrowLeft, KeyboardArrowRight, LightMode, Logout, PushPin, Search, Settings } from '@mui/icons-material';
import { FileFilterTabs, NewBadge, FileKindGlyph, matchesFileFilter, fileCounts, isCreatedFile, isEditedFile } from '../lib/fileClassify'

// ── File icon helper ─────────────────────────────────────────────────────────
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

// ── File Tree Node ────────────────────────────────────────────────────────────
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
            {expanded ? <KeyboardArrowDown sx={{ fontSize: 11 }} /> : <KeyboardArrowRight sx={{ fontSize: 11 }} />}
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

// ── Timestamp helper ──────────────────────────────────────────────────────────
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

// ── Session Item ──────────────────────────────────────────────────────────────
function SessionItem({ session, active, onLoad, onDelete, onRename, selectMode, selected, onToggleSelect }: {
  session: any; active: boolean
  onLoad: () => void; onDelete: () => void; onRename: (title: string) => void
  selectMode?: boolean; selected?: boolean; onToggleSelect?: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(session.title)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { if (editing) inputRef.current?.focus() }, [editing])

  const commitRename = () => {
    if (draft.trim() && draft !== session.title) onRename(draft.trim())
    setEditing(false)
  }

  const handleClick = () => {
    if (editing) return
    if (selectMode && onToggleSelect) {
      onToggleSelect()
      return
    }
    onLoad()
  }

  return (
    <div
      className={`group relative flex items-start gap-2.5 px-3 py-2.5 cursor-pointer border-l-2 transition-all ${
        selected
          ? 'bg-accent/10 border-accent text-ink'
          : active
            ? 'bg-overlay border-accent text-ink'
            : 'border-transparent hover:bg-overlay/60 text-muted hover:text-ink'
      }`}
      onClick={handleClick}
    >
      {selectMode ? (
        <div className={`flex-shrink-0 mt-0.5 w-4 h-4 rounded border-2 flex items-center justify-center transition-all ${
          selected
            ? 'bg-accent border-accent'
            : 'border-faint hover:border-accent/60'
        }`}>
          {selected && (
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          )}
        </div>
      ) : (
        <Chat sx={{ fontSize: 13 }} className={`flex-shrink-0 mt-0.5 ${active ? 'text-accent' : 'text-faint'}`} />
      )}
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
      {!editing && !selectMode && (
        <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
          <button
            onClick={(e) => { e.stopPropagation(); setEditing(true) }}
            className="btn-icon w-6 h-6" title="Rename"
          ><Edit sx={{ fontSize: 11 }} /></button>
          <button
            onClick={(e) => { e.stopPropagation(); onDelete() }}
            className="btn-icon w-6 h-6 hover:text-danger" title="Delete"
          ><Delete sx={{ fontSize: 11 }} /></button>
        </div>
      )}
    </div>
  )
}

// ── Session List ──────────────────────────────────────────────────────────────
function SessionList() {
  const { sessions, setSessions, activeSessions, setActiveSession, setMessages } = useAppStore()
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<any[]>([])
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)
  const [pendingDeleteFileCount, setPendingDeleteFileCount] = useState(0)
  const [deleteConfirmLoading, setDeleteConfirmLoading] = useState(false)

  // Multi-select state
  const [selectMode, setSelectMode] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [pendingBulkDelete, setPendingBulkDelete] = useState(false)
  const [bulkDeleteFileCount, setBulkDeleteFileCount] = useState(0)
  const [bulkDeleteLoading, setBulkDeleteLoading] = useState(false)

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

  // ── Multi-select handlers ───────────────────────────────────
  const toggleSelectMode = () => {
    if (selectMode) {
      setSelectedIds(new Set())
    }
    setSelectMode(!selectMode)
  }

  const toggleSelect = (id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectAll = () => {
    setSelectedIds(new Set(filteredSessions.map(s => s.id)))
  }

  const deselectAll = () => {
    setSelectedIds(new Set())
  }

  const promptBulkDelete = async () => {
    if (selectedIds.size === 0) return
    let totalFiles = 0
    for (const id of selectedIds) {
      try {
        const files = await api.sessionFiles.list(id)
        totalFiles += files.length
      } catch { /* ignore */ }
    }
    setBulkDeleteFileCount(totalFiles)
    setPendingBulkDelete(true)
  }

  const confirmBulkDelete = async () => {
    setBulkDeleteLoading(true)
    try {
      for (const id of selectedIds) {
        await api.chat.deleteSession(id)
        if (activeSessions === id) { setActiveSession(null); setMessages([]) }
      }
      await loadSessions()
      const count = selectedIds.size
      toast.success(`Deleted ${count} chat${count > 1 ? 's' : ''}`)
      setSelectedIds(new Set())
      setSelectMode(false)
    } catch (e: any) {
      toast.error('Bulk delete failed', e.message)
    } finally {
      setBulkDeleteLoading(false)
      setPendingBulkDelete(false)
      setBulkDeleteFileCount(0)
    }
  }

  useEffect(() => {
    if (searchQuery.trim() === '') { setSearchResults([]); return }
    const timer = setTimeout(async () => {
      try {
        const response = await api.chat.search(searchQuery)
        const data = response as any
        setSearchResults(data.results ?? data ?? [])
      } catch { setSearchResults([]) }
    }, 400)
    return () => clearTimeout(timer)
  }, [searchQuery])

  const filteredSessions = searchQuery.trim()
    ? sessions.filter(s => (s.title || '').toLowerCase().includes(searchQuery.toLowerCase()))
    : sessions

  return (
    <div className="flex flex-col h-full">
      <div className="p-2 border-b border-border flex gap-2">
        <button onClick={newSession} className="btn-primary flex-1 gap-2">
          <Add sx={{ fontSize: 14 }} /> New Chat
          <kbd className="ml-auto text-[10px] opacity-50 font-mono bg-accent-dark/30 px-1 rounded">⌘N</kbd>
        </button>
        <button
          onClick={toggleSelectMode}
          className={`flex items-center justify-center px-2.5 rounded-lg text-[12px] font-semibold transition-all ${
            selectMode
              ? 'bg-accent/15 text-accent border border-accent/30'
              : 'bg-overlay text-muted border border-border hover:text-ink hover:bg-surface'
          }`}
          title={selectMode ? 'Cancel selection' : 'Select chats'}
        >
          {selectMode ? 'Done' : 'Select'}
        </button>
      </div>

      {/* Multi-select action bar */}
      {selectMode && (
        <div className="px-3 py-2 border-b border-border bg-surface/80 flex items-center gap-2">
          <button
            onClick={selectedIds.size === filteredSessions.length && filteredSessions.length > 0 ? deselectAll : selectAll}
            className="text-[11px] text-accent font-semibold hover:underline"
          >
            {selectedIds.size === filteredSessions.length && filteredSessions.length > 0 ? 'Deselect All' : 'Select All'}
          </button>
          <span className="text-[11px] text-muted ml-auto">
            {selectedIds.size} selected
          </span>
          {selectedIds.size > 0 && (
            <button
              onClick={promptBulkDelete}
              className="flex items-center gap-1 text-[11px] font-semibold text-danger hover:bg-danger/10 px-2 py-1 rounded-md transition-colors"
            >
              <Delete sx={{ fontSize: 12 }} /> Delete
            </button>
          )}
        </div>
      )}

      <div className="relative px-2 py-1.5">
        <span className="absolute left-4 top-1/2 -translate-y-1/2 text-muted-foreground">
          <Search sx={{ fontSize: 13 }} className="opacity-60" />
        </span>
        <input
          type="text"
          placeholder="Search sessions..."
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          className="w-full pl-8 pr-7 py-1 text-[12px] rounded border bg-background border-border focus:outline-none"
        />
        {searchQuery && (
          <button
            type="button"
            className="absolute right-4 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            onClick={() => setSearchQuery('')}
            tabIndex={-1}
          ><Close sx={{ fontSize: 13 }} /></button>
        )}
      </div>
      <div className="flex-1 overflow-y-auto py-1">
        {searchQuery.trim() !== '' && filteredSessions.length === 0 && searchResults.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-20 gap-2 text-faint">
            <span className="text-xs">No matches</span>
          </div>
        ) : filteredSessions.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-32 gap-2 text-faint">
            <Chat sx={{ fontSize: 24 }} className="opacity-40" />
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
              selectMode={selectMode}
              selected={selectedIds.has(s.id)}
              onToggleSelect={() => toggleSelect(s.id)}
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
                onClick={() => { setActiveSession(result.session_id ?? result.id); setMessages([]) }}
              >
                <div className="text-sm font-normal truncate">{result.session_name ?? result.name ?? 'Untitled'}</div>
                <div className="text-xs text-muted-foreground line-clamp-2">{result.content_snippet ?? result.snippet ?? ''}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Delete confirmation modal */}
      {pendingDeleteId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-surface border border-border rounded-2xl shadow-2xl shadow-black/40 p-6 w-full max-w-sm mx-4">
            <div className="flex items-start gap-3 mb-4">
              <div className="shrink-0 w-10 h-10 rounded-full bg-red-500/15 border border-red-500/25 flex items-center justify-center">
                <svg className="w-5 h-5 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
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
              >Cancel</button>
              <button
                onClick={confirmDeleteSession}
                disabled={deleteConfirmLoading}
                className="px-4 py-2 rounded-lg text-[13px] font-semibold bg-red-500/90 hover:bg-red-500 text-white border border-red-500/50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >{deleteConfirmLoading ? 'Deleting…' : 'Delete Chat'}</button>
            </div>
          </div>
        </div>
      )}

      {/* Bulk delete confirmation modal */}
      {pendingBulkDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-surface border border-border rounded-2xl shadow-2xl shadow-black/40 p-6 w-full max-w-sm mx-4">
            <div className="flex items-start gap-3 mb-4">
              <div className="shrink-0 w-10 h-10 rounded-full bg-red-500/15 border border-red-500/25 flex items-center justify-center">
                <svg className="w-5 h-5 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                </svg>
              </div>
              <div>
                <h3 className="text-[15px] font-semibold text-ink mb-1">Delete {selectedIds.size} chat{selectedIds.size > 1 ? 's' : ''}?</h3>
                <p className="text-[13px] text-muted leading-relaxed">
                  This will permanently delete all chat history
                  {bulkDeleteFileCount > 0 && (
                    <> and <span className="text-red-400 font-medium">{bulkDeleteFileCount} file{bulkDeleteFileCount !== 1 ? 's' : ''}</span> across {selectedIds.size} session{selectedIds.size > 1 ? 's' : ''}</>
                  )}{bulkDeleteFileCount === 0 && <> for {selectedIds.size} session{selectedIds.size > 1 ? 's' : ''}</>}.
                  {' '}This cannot be undone.
                </p>
              </div>
            </div>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => { setPendingBulkDelete(false); setBulkDeleteFileCount(0) }}
                className="px-4 py-2 rounded-lg text-[13px] font-medium text-muted border border-border hover:bg-surface-alt transition-colors"
              >Cancel</button>
              <button
                onClick={confirmBulkDelete}
                disabled={bulkDeleteLoading}
                className="px-4 py-2 rounded-lg text-[13px] font-semibold bg-red-500/90 hover:bg-red-500 text-white border border-red-500/50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >{bulkDeleteLoading ? 'Deleting…' : `Delete ${selectedIds.size} Chat${selectedIds.size > 1 ? 's' : ''}`}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Session Files Panel ───────────────────────────────────────────────────────
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

function SessionFilesPanel() {
  const { sessionFiles, removeSessionFile, addSessionFile, fileFilter } = useAppStore()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const counts = fileCounts(sessionFiles)
  const visibleFiles = sessionFiles.filter(f => matchesFileFilter(f, fileFilter))

  const handleDownload = async (file: SessionFile) => {
    try {
      if (file.session_id) {
        await api.sessionFiles.download(file.session_id, file.id, file.filename)
      } else if (file.content != null) {
        const blob = new Blob([file.content], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a'); a.href = url; a.download = file.filename; a.click()
        URL.revokeObjectURL(url)
      }
    } catch { /* silent */ }
  }

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
      <div className="px-3 py-2 border-b border-border flex items-center justify-between">
        <span className="text-[11px] text-muted">
          {sessionFiles.length > 0
            ? `${sessionFiles.length} file${sessionFiles.length > 1 ? 's' : ''}`
            : 'No files yet'}
        </span>
        <button
          onClick={() => fileInputRef.current?.click()}
          className="flex items-center gap-1 px-2 py-1 rounded-lg bg-accent/10 text-accent text-[11px] font-semibold hover:bg-accent/20 transition-colors"
          title="Upload files"
        >
          <FileUpload sx={{ fontSize: 11 }} /> Add
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
      {sessionFiles.length > 0 && (
        <div className="px-3 py-2 border-b border-border/60">
          <FileFilterTabs counts={counts} size="sm" />
        </div>
      )}
      <div className="flex-1 overflow-y-auto py-1">
        {sessionFiles.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 px-4 text-center pb-8">
            <div className="w-12 h-12 rounded-2xl bg-surface flex items-center justify-center">
              <Description sx={{ fontSize: 22 }} className="text-muted/70" />
            </div>
            <div>
              <p className="text-[13px] font-semibold text-muted">No files in this chat</p>
              <p className="text-[11px] text-faint mt-1 leading-relaxed">
                Drop files into the chat or click <strong className="text-muted/70">Add</strong> above
              </p>
            </div>
            <button
              onClick={() => fileInputRef.current?.click()}
              className="mt-1 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent/10 text-accent text-[12px] font-semibold hover:bg-accent/20 transition-colors border border-accent/20"
            >
              <FileUpload sx={{ fontSize: 12 }} /> Upload files
            </button>
          </div>
        ) : visibleFiles.length === 0 ? (
          <div className="px-4 py-6 text-center text-[12px] text-muted/60">
            No {fileFilter === 'new' ? 'new' : 'current'} files in this chat
          </div>
        ) : (
          <div className="px-2 py-1 space-y-0.5">
            {visibleFiles.map(file => (
              <div
                key={file.id}
                className="group flex items-center gap-2 px-2.5 py-2 rounded-lg hover:bg-overlay transition-colors"
              >
                <FileKindGlyph file={file} size={16} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <p className="text-[12px] font-medium text-ink truncate">{file.filename}</p>
                    {isCreatedFile(file) && <NewBadge compact />}
                    {!isCreatedFile(file) && isEditedFile(file) && (
                      <span className="flex items-center gap-1 text-[9px] font-medium px-1.5 py-0.5 bg-success/10 text-success border border-success/20 rounded-md shrink-0">
                        <span className="w-1.5 h-1.5 rounded-full bg-success inline-block" /> Edited
                      </span>
                    )}
                  </div>
                  {file.lines > 0 && (
                    <p className="text-[10px] text-faint">{file.lines} lines{file.symbol_count > 0 ? ` · ${file.symbol_count} symbols` : ''}</p>
                  )}
                </div>
                <button
                  onClick={() => handleDownload(file)}
                  className="opacity-0 group-hover:opacity-100 text-faint hover:text-accent transition-all flex-shrink-0"
                  title="Download file"
                ><Download sx={{ fontSize: 14 }} /></button>
                <button
                  onClick={() => removeSessionFile(file.id)}
                  className="opacity-0 group-hover:opacity-100 text-faint hover:text-red-400 transition-all flex-shrink-0"
                  title="Remove from chat"
                ><Close sx={{ fontSize: 13 }} /></button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Rail items config ─────────────────────────────────────────────────────────
type TabId = 'sessions' | 'files' | 'github' | 'context' | 'linear' | 'vercel' | 'railway'
const RAIL_ITEMS: { id: TabId; icon: any; label: string; tooltip: string }[] = [
  { id: 'sessions', icon: Chat, label: 'Chats',  tooltip: 'Chats' },
  { id: 'files',    icon: Code,      label: 'Files',  tooltip: 'Session Files' },
  { id: 'github',   icon: GitHub,        label: 'GitHub', tooltip: 'GitHub' },
  { id: 'linear',   icon: LinearIcon,    label: 'Linear', tooltip: 'Linear Issues' },
  { id: 'vercel',   icon: VercelIcon,         label: 'Vercel', tooltip: 'Vercel Deployments' },
  { id: 'railway',  icon: RailwayIcon,        label: 'Railway', tooltip: 'Railway Services' },
  { id: 'context',  icon: PushPin,           label: 'Pinned', tooltip: 'Pinned & Memory' },
]

// ── Vercel icon (inline SVG) ──────────────────────────────────────────────────
function VercelIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 512 512" fill="currentColor">
      <path d="M256 48L496 464H16L256 48Z" />
    </svg>
  )
}

// ── Railway icon (inline SVG) ─────────────────────────────────────────────────
function RailwayIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="currentColor">
      <path d="M16 2C8.268 2 2 8.268 2 16s6.268 14 14 14 14-6.268 14-14S23.732 2 16 2zm0 2c6.627 0 12 5.373 12 12S22.627 28 16 28 4 22.627 4 16 9.373 4 16 4zm-5 5v14h3v-5h4l3 5h3.5l-3.2-5.3C23 17 24 15.2 24 13c0-2.2-1.8-4-4-4h-9zm3 2.5h6c.8 0 1.5.7 1.5 1.5s-.7 1.5-1.5 1.5h-6v-3z"/>
    </svg>
  )
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
// Linear icon (inline SVG)
function LinearIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" fill="currentColor">
      <path d="M1.22541 61.5228c-.2225-.9485.90748-1.5459 1.59638-.857L39.3342 97.1782c.6889.6889.0915 1.8189-.857 1.5964C20.0515 94.2409 5.75949 79.9489 1.22541 61.5228zM.00189135 46.8891c-.01764375.2833.08887225.5599.28957985.7606L52.3503 99.7085c.2007.2007.4773.3072.7606.2896 2.3692-.1476 4.6938-.46 6.9624-.9259.7645-.157 1.0301-1.0963.4782-1.6481L2.57595 39.4485c-.55186-.5519-1.49117-.2863-1.648174.4782-.465666 2.2686-.77832 4.5932-.92588 6.9624zM4.21111 32.217c-.16499.3086-.10307.6839.15059.9278L66.8827 95.7893c.2439.2537.6192.3156.9278.1506 1.8364-.9832 3.6014-2.0639 5.2888-3.2349.5151-.3631.5636-1.0957.1024-1.5569L9.97262 30.8852c-.46113-.4612-1.19376-.4127-1.55688.1024-1.17101 1.6874-2.25171 3.4524-3.23463 5.2294zM12.6587 22.2145c-.3762-.3762-.3762-.9864 0-1.3625C23.1581 10.3514 37.4823 3.48085 53.3921 2.67806c.7845-.04036 1.4361.59114 1.4321 1.37753l-.1701 34.69671c-.0027.5533-.3462 1.0452-.8675 1.2518l-1.7984.7013c-.4897.191-1.0527.0861-1.4368-.2981L12.6587 22.2145zM29.8038 86.8099 13.1901 70.1962c-.5145-.5145-.3991-1.3745.2381-1.7235l18.8163-10.5469c.5501-.3082 1.2311-.1673 1.624.3368l13.2584 17.0328c.3929.5041.3241 1.2137-.1594 1.6327l-15.5549 13.4605c-.4998.4325-1.2729.3819-1.7088-.1287z"/>
    </svg>
  )
}

export function Sidebar() {
  const { sidebarTab, setSidebarTab, setSettingsOpen, sessionFiles, sidebarPanelOpen, setSidebarPanelOpen } = useAppStore()
  const { theme, toggleTheme } = useThemeStore()
  const { user, logout } = useAuthStore()
  const [panelOpen, setPanelOpen] = useState(true)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const userMenuRef = useRef<HTMLDivElement>(null)

  // Sync external open requests (e.g. from NewFileCard "Add to session")
  useEffect(() => {
    if (sidebarPanelOpen) {
      setPanelOpen(true)
      setSidebarPanelOpen(false)
    }
  }, [sidebarPanelOpen, setSidebarPanelOpen])

  // Close user menu on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (userMenuRef.current && !userMenuRef.current.contains(e.target as Node)) {
        setUserMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  const handleRailClick = (id: TabId) => {
    if (sidebarTab === id && panelOpen) {
      setPanelOpen(false)
    } else {
      setSidebarTab(id)
      setPanelOpen(true)
    }
  }

  const fileCount = sessionFiles.length
  const initials = (user?.username ?? 'U').slice(0, 2).toUpperCase()

  const panelLabel = RAIL_ITEMS.find(r => r.id === sidebarTab)?.label ?? ''

  return (
    <div className="flex h-full">

      {/* ── Rail (always 44px) ── */}
      <div className="flex flex-col items-center pt-3 pb-3 gap-1 w-[44px] bg-surface border-r border-border flex-shrink-0">

        {/* Logo mark */}
        <div className="mb-2 flex items-center justify-center w-8 h-8">
          <img src="/otter.png" alt="SurgicalAI" className="w-7 h-7 rounded-md" />
        </div>

        {/* Nav icons */}
        {RAIL_ITEMS.map(({ id, icon: Icon, tooltip }) => {
          const isActive = sidebarTab === id && panelOpen
          const badge = id === 'files' && fileCount > 0 ? fileCount : null
          return (
            <button
              key={id}
              onClick={() => handleRailClick(id)}
              title={tooltip}
              className={`relative flex items-center justify-center w-8 h-8 rounded-lg transition-all ${
                isActive
                  ? 'bg-accent/15 text-accent'
                  : 'text-muted hover:text-ink hover:bg-overlay'
              }`}
            >
              <Icon sx={{ fontSize: 17 }} />
              {badge !== null && (
                <span className="absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] flex items-center justify-center rounded-full bg-accent text-white text-[9px] font-bold px-0.5 leading-none">
                  {badge > 99 ? '99+' : badge}
                </span>
              )}
            </button>
          )
        })}

        {/* Spacer */}
        <div className="flex-1" />

        {/* Theme toggle */}
        <button
          onClick={toggleTheme}
          title={theme === 'dark' ? 'Light mode' : 'Dark mode'}
          className="flex items-center justify-center w-8 h-8 rounded-lg text-muted hover:text-ink hover:bg-overlay transition-all"
        >
          {theme === 'dark'
            ? <LightMode sx={{ fontSize: 16 }} />
            : <DarkMode sx={{ fontSize: 16 }} />}
        </button>

        {/* Settings */}
        <button
          onClick={() => setSettingsOpen(true)}
          title="Settings"
          className="flex items-center justify-center w-8 h-8 rounded-lg text-muted hover:text-ink hover:bg-overlay transition-all"
        >
          <Settings sx={{ fontSize: 16 }} />
        </button>

        {/* Avatar + user menu */}
        <div ref={userMenuRef} className="relative mt-1 mb-0.5">
          <button
            onClick={() => setUserMenuOpen(v => !v)}
            title={user?.username ?? 'Account'}
            className="w-7 h-7 rounded-full bg-indigo-600 hover:bg-indigo-500 flex items-center justify-center text-white text-[11px] font-bold transition"
          >
            {initials}
          </button>
          {userMenuOpen && (
            <div className="absolute left-9 bottom-0 w-48 bg-surface border border-border rounded-xl shadow-xl z-50 py-1">
              <div className="px-3 py-2 border-b border-border">
                <p className="text-xs font-semibold text-ink truncate">{user?.username}</p>
                {user?.email && <p className="text-[11px] text-faint truncate">{user.email}</p>}
                {user?.is_admin && (
                  <span className="inline-block mt-0.5 text-[10px] px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-400 font-medium">Admin</span>
                )}
              </div>
              <button
                onClick={() => { logout(); setUserMenuOpen(false) }}
                className="w-full flex items-center gap-2 px-3 py-2 text-xs text-red-400 hover:bg-overlay transition"
              >
                <Logout sx={{ fontSize: 13 }} /> Sign out
              </button>
            </div>
          )}
        </div>
      </div>

      {/* ── Sliding panel ── */}
      <div
        className={`flex flex-col bg-surface overflow-hidden transition-all duration-200 ${
          panelOpen ? 'flex-1 min-w-0' : 'w-0'
        }`}
      >
        {/* Panel header */}
        <div className="flex items-center justify-between px-3 py-2.5 border-b border-border flex-shrink-0">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-muted select-none">
            {panelLabel}
          </span>
          <button
            onClick={() => setPanelOpen(false)}
            className="flex items-center justify-center w-5 h-5 rounded text-faint hover:text-ink hover:bg-overlay transition"
            title="Collapse panel"
          >
            <KeyboardArrowLeft sx={{ fontSize: 13 }} />
          </button>
        </div>

        {/* Panel content */}
        <div className="flex-1 overflow-hidden flex flex-col min-h-0">
          {sidebarTab === 'sessions' && <SessionList />}
          {sidebarTab === 'files'    && <SessionFilesPanel />}
          {sidebarTab === 'context'  && <ContextPanel />}
          {sidebarTab === 'github'   && (
            <GitHubPanel onOpenSettings={() => { setSidebarTab('sessions'); setSettingsOpen(true) }} />
          )}
          {sidebarTab === 'linear'   && (
            <LinearPanel onOpenSettings={() => { setSidebarTab('sessions'); setSettingsOpen(true) }} />
          )}
          {sidebarTab === 'vercel'   && (
            <VercelPanel onOpenSettings={() => { setSidebarTab('sessions'); setSettingsOpen(true) }} />
          )}
          {sidebarTab === 'railway'  && (
            <RailwayPanel onOpenSettings={() => { setSidebarTab('sessions'); setSettingsOpen(true) }} />
          )}
        </div>
      </div>

    </div>
  )
}
