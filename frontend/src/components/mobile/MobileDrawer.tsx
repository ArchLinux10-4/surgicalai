import React, { useEffect, useState } from 'react'
import {
  X, MessageSquare, Plus, FileCode, Github, Pin,
  Sun, Moon, Settings, LogOut, Trash2, Edit2, Check,
} from 'lucide-react'
import { useAppStore } from '../../stores/appStore'
import { useAuthStore } from '../../stores/authStore'
import { useThemeStore } from '../../stores/themeStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'

interface Props {
  open: boolean
  onClose: () => void
  onOpenFiles: () => void
}

type Section = 'chats' | 'files' | 'github' | 'linear' | 'pinned'

export function MobileDrawer({ open, onClose, onOpenFiles }: Props) {
  const {
    sessions, setSessions, activeSessions, setActiveSession, setMessages,
    sessionFiles, setSettingsOpen,
  } = useAppStore()
  const { theme, toggleTheme } = useThemeStore()
  const { user, logout } = useAuthStore()

  const [section, setSection] = useState<Section>('chats')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)

  // Re-fetch sessions every time the drawer opens so the list is always fresh
  useEffect(() => {
    if (open) {
      api.chat.getSessions().then(setSessions).catch(() => {})
    }
  }, [open])

  const initials = (user?.username ?? 'U').slice(0, 2).toUpperCase()

  const newChat = async () => {
    try {
      const s = await api.chat.createSession({ title: 'New Chat' })
      const updated = await api.chat.getSessions()
      setSessions(updated)
      setActiveSession(s.id)
      setMessages([])
      onClose()
    } catch {
      toast.error('Failed to create chat')
    }
  }

  const loadSession = async (id: string) => {
    setActiveSession(id)
    try {
      const msgs = await api.chat.getMessages(id)
      setMessages(msgs)
    } catch {
      setMessages([])
    }
    onClose()
  }

  const renameSession = async (id: string) => {
    if (!editValue.trim()) { setEditingId(null); return }
    try {
      await api.chat.renameSession(id, editValue.trim())
      const updated = await api.chat.getSessions()
      setSessions(updated)
    } catch {
      toast.error('Rename failed')
    } finally {
      setEditingId(null)
    }
  }

  const deleteSession = async (id: string) => {
    try {
      await api.chat.deleteSession(id)
      const updated = await api.chat.getSessions()
      setSessions(updated)
      if (activeSessions === id) { setActiveSession(null); setMessages([]) }
    } catch {
      toast.error('Delete failed')
    } finally {
      setConfirmDeleteId(null)
    }
  }

  return (
    <>
      {/* Overlay */}
      <div
        className={`fixed inset-0 z-40 bg-black/50 transition-opacity duration-200 ${
          open ? 'opacity-100' : 'opacity-0 pointer-events-none'
        }`}
        onClick={onClose}
        aria-hidden={!open}
      />

      {/* Drawer */}
      <aside
        className={`fixed top-0 left-0 bottom-0 z-50 w-[85%] max-w-[340px] bg-base border-r border-border flex flex-col transition-transform duration-250 ease-out shadow-2xl ${
          open ? 'translate-x-0' : '-translate-x-full'
        }`}
        aria-hidden={!open}
        style={{ paddingTop: 'env(safe-area-inset-top)', paddingBottom: 'env(safe-area-inset-bottom)' }}
      >
        {/* Header — user + close */}
        <div className="flex items-center justify-between gap-2 px-4 h-14 border-b border-border flex-shrink-0">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="w-8 h-8 rounded-full bg-indigo-600 flex items-center justify-center text-white text-[12px] font-bold flex-shrink-0">
              {initials}
            </div>
            <div className="min-w-0">
              <p className="text-[14px] font-semibold text-ink truncate leading-tight">{user?.username || 'User'}</p>
              {user?.email && <p className="text-[11px] text-muted truncate leading-tight">{user.email}</p>}
            </div>
          </div>
          <button
            onClick={onClose}
            className="flex items-center justify-center w-9 h-9 rounded-lg text-muted hover:text-ink hover:bg-overlay transition-colors"
            aria-label="Close menu"
          >
            <X size={18} />
          </button>
        </div>

        {/* New chat — primary action */}
        <div className="px-3 pt-3 pb-2 flex-shrink-0">
          <button
            onClick={newChat}
            className="w-full flex items-center justify-center gap-2 h-11 rounded-xl bg-accent text-base font-semibold text-[14px] hover:bg-accent/90 active:scale-[0.98] transition-all"
          >
            <Plus size={17} strokeWidth={2.5} />
            New chat
          </button>
        </div>

        {/* Section tabs */}
        <div className="flex gap-0.5 px-3 pt-2 pb-2 flex-shrink-0 border-b border-border-sub">
          {([
            { id: 'chats',  icon: MessageSquare, label: 'Chats',  badge: sessions.length },
            { id: 'files',  icon: FileCode,      label: 'Files',  badge: sessionFiles.length },
            { id: 'github', icon: Github,        label: 'GitHub' },
            { id: 'pinned', icon: Pin,           label: 'Pinned' },
          ] as { id: Section; icon: any; label: string; badge?: number }[]).map(({ id, icon: Icon, label, badge }) => (
            <button
              key={id}
              onClick={() => {
                if (id === 'files') {
                  onOpenFiles()
                  onClose()
                  return
                }
                setSection(id)
              }}
              className={`flex-1 flex flex-col items-center gap-0.5 py-2 rounded-lg transition-colors ${
                section === id ? 'bg-overlay text-ink' : 'text-muted hover:text-ink'
              }`}
            >
              <div className="relative">
                <Icon size={18} strokeWidth={1.75} />
                {badge !== undefined && badge > 0 && (
                  <span className="absolute -top-1 -right-1.5 min-w-[14px] h-[14px] flex items-center justify-center rounded-full bg-accent text-white text-[9px] font-bold px-0.5 leading-none">
                    {badge > 99 ? '99+' : badge}
                  </span>
                )}
              </div>
              <span className="text-[10px] font-medium">{label}</span>
            </button>
          ))}
        </div>

        {/* Section content */}
        <div className="flex-1 overflow-y-auto overscroll-contain">
          {section === 'chats' && (
            sessions.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-full px-8 text-center text-muted gap-2 py-12">
                <MessageSquare size={28} className="opacity-40" />
                <p className="text-[13px]">No chats yet</p>
                <p className="text-[11px] text-faint">Tap New chat to start</p>
              </div>
            ) : (
              <ul className="py-1">
                {sessions.map(s => {
                  const isActive = s.id === activeSessions
                  const isEditing = editingId === s.id
                  const isConfirming = confirmDeleteId === s.id
                  return (
                    <li
                      key={s.id}
                      className={`relative mx-2 my-0.5 rounded-xl ${isActive ? 'bg-overlay' : ''}`}
                    >
                      {isEditing ? (
                        <div className="flex items-center gap-1 px-3 py-2.5">
                          <input
                            autoFocus
                            value={editValue}
                            onChange={e => setEditValue(e.target.value)}
                            onKeyDown={e => {
                              if (e.key === 'Enter') renameSession(s.id)
                              if (e.key === 'Escape') setEditingId(null)
                            }}
                            className="flex-1 bg-base border border-accent/50 text-ink text-[14px] rounded-lg px-2.5 py-1.5 outline-none"
                          />
                          <button
                            onClick={() => renameSession(s.id)}
                            className="w-9 h-9 flex items-center justify-center rounded-lg text-success hover:bg-overlay"
                          >
                            <Check size={16} />
                          </button>
                        </div>
                      ) : isConfirming ? (
                        <div className="flex items-center justify-between gap-2 px-3 py-2.5">
                          <span className="text-[13px] text-danger flex-1 truncate">Delete this chat?</span>
                          <button
                            onClick={() => setConfirmDeleteId(null)}
                            className="px-3 h-8 rounded-lg text-[12px] text-muted hover:bg-overlay"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={() => deleteSession(s.id)}
                            className="px-3 h-8 rounded-lg bg-danger text-white text-[12px] font-semibold"
                          >
                            Delete
                          </button>
                        </div>
                      ) : (
                        <div
                          className="flex items-start gap-3 px-3 py-2.5 cursor-pointer active:bg-overlay/60"
                          onClick={() => loadSession(s.id)}
                        >
                          <MessageSquare
                            size={16}
                            className={`mt-0.5 flex-shrink-0 ${isActive ? 'text-accent' : 'text-muted'}`}
                          />
                          <div className="flex-1 min-w-0">
                            <p className="text-[14px] font-medium text-ink truncate leading-tight">{s.title}</p>
                            <p className="text-[11px] text-muted mt-0.5">
                              {(s as any).message_count ?? 0} msg{((s as any).message_count ?? 0) !== 1 ? 's' : ''}
                            </p>
                          </div>
                          <button
                            onClick={(e) => { e.stopPropagation(); setEditValue(s.title); setEditingId(s.id) }}
                            className="w-9 h-9 flex items-center justify-center rounded-lg text-muted hover:text-ink hover:bg-overlay -mr-1"
                            aria-label="Rename"
                          >
                            <Edit2 size={14} />
                          </button>
                          <button
                            onClick={(e) => { e.stopPropagation(); setConfirmDeleteId(s.id) }}
                            className="w-9 h-9 flex items-center justify-center rounded-lg text-muted hover:text-danger hover:bg-overlay -mr-1"
                            aria-label="Delete"
                          >
                            <Trash2 size={14} />
                          </button>
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            )
          )}

          {section === 'github' && (
            <div className="flex flex-col items-center justify-center h-full px-8 text-center gap-4 py-12">
              <div className="w-12 h-12 rounded-2xl bg-surface border border-border flex items-center justify-center">
                <Github size={24} className="text-muted" />
              </div>
              <div>
                <p className="text-[14px] font-semibold text-ink mb-1">GitHub</p>
                <p className="text-[12px] text-muted leading-relaxed">
                  Add your GitHub Personal Access Token in Settings to browse repos, load files, and sync changes.
                </p>
              </div>
              <button
                onClick={() => { setSettingsOpen(true); onClose() }}
                className="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-accent text-white text-[13px] font-semibold active:scale-95 transition-transform"
              >
                Open Settings
              </button>
              <p className="text-[11px] text-faint max-w-[220px]">
                Needs a Classic PAT with <code className="bg-overlay px-1 rounded">repo</code> + <code className="bg-overlay px-1 rounded">read:user</code> scopes
              </p>
            </div>
          )}

          {section === 'pinned' && (
            <div className="flex flex-col items-center justify-center h-full px-8 text-center text-muted gap-2 py-12">
              <Pin size={28} className="opacity-40" />
              <p className="text-[13px]">No pinned items</p>
              <p className="text-[11px] text-faint">Pin context from the chat to keep it across sessions</p>
            </div>
          )}
        </div>

        {/* Footer — theme + settings + logout */}
        <div className="flex-shrink-0 border-t border-border px-2 py-2 flex items-center gap-1">
          <button
            onClick={toggleTheme}
            className="flex-1 flex items-center justify-center gap-1.5 h-10 rounded-lg text-muted hover:text-ink hover:bg-overlay transition-colors"
          >
            {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
            <span className="text-[12px] font-medium">{theme === 'dark' ? 'Light' : 'Dark'}</span>
          </button>
          <button
            onClick={() => { setSettingsOpen(true); onClose() }}
            className="flex-1 flex items-center justify-center gap-1.5 h-10 rounded-lg text-muted hover:text-ink hover:bg-overlay transition-colors"
          >
            <Settings size={16} />
            <span className="text-[12px] font-medium">Settings</span>
          </button>
          <button
            onClick={() => { logout(); onClose() }}
            className="flex-1 flex items-center justify-center gap-1.5 h-10 rounded-lg text-muted hover:text-danger hover:bg-overlay transition-colors"
          >
            <LogOut size={16} />
            <span className="text-[12px] font-medium">Sign out</span>
          </button>
        </div>
      </aside>
    </>
  )
}
