/**
 * MobileSessionsPanel — session list for mobile.
 * Load, create, delete sessions. Tapping a session navigates back to Chat.
 */
import React, { useState, useEffect } from 'react'
import { useAppStore } from '../../stores/appStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'

interface Props {
  onSelectSession: () => void
}

export function MobileSessionsPanel({ onSelectSession }: Props) {
  const {
    sessions, setSessions, activeSessions,
    setActiveSession, setMessages, setSessionFiles,
  } = useAppStore()

  const [search, setSearch]   = useState('')
  const [loading, setLoading] = useState(false)
  const [deleting, setDeleting] = useState<string | null>(null)

  useEffect(() => {
    api.chat.getSessions().then(setSessions).catch(() => {})
  }, [])

  const loadSession = async (id: string) => {
    setActiveSession(id)
    try {
      const msgs = await api.chat.getMessages(id)
      setMessages(msgs)
      const files = await api.sessionFiles.list(id)
      setSessionFiles(files)
    } catch {
      setMessages([])
      setSessionFiles([])
    }
    onSelectSession()
  }

  const createSession = async () => {
    setLoading(true)
    try {
      const s = await api.chat.createSession({ title: 'New Chat' })
      const updated = await api.chat.getSessions()
      setSessions(updated)
      setActiveSession(s.id)
      setMessages([])
      setSessionFiles([])
      onSelectSession()
    } catch (e: any) {
      toast.error('Could not create session')
    } finally {
      setLoading(false)
    }
  }

  const deleteSession = async (id: string) => {
    setDeleting(id)
    try {
      await api.chat.deleteSession(id)
      if (activeSessions === id) {
        setActiveSession(null)
        setMessages([])
        setSessionFiles([])
      }
      const updated = await api.chat.getSessions()
      setSessions(updated)
      toast.success('Deleted')
    } catch {
      toast.error('Delete failed')
    } finally {
      setDeleting(null)
    }
  }

  const filtered = search
    ? sessions.filter(s => (s.title || '').toLowerCase().includes(search.toLowerCase()))
    : sessions

  const formatDate = (d: string) => {
    if (!d) return ''
    const date = new Date(d)
    const now   = new Date()
    const diff  = now.getTime() - date.getTime()
    const mins  = Math.floor(diff / 60000)
    if (mins < 1)   return 'just now'
    if (mins < 60)  return `${mins}m ago`
    if (mins < 1440) return `${Math.floor(mins / 60)}h ago`
    return `${Math.floor(mins / 1440)}d ago`
  }

  return (
    <div className="flex flex-col h-full bg-base">
      {/* Header */}
      <div className="flex-shrink-0 flex items-center justify-between px-4 py-3 border-b border-border bg-surface/80">
        <span className="text-sm font-semibold text-ink/80">Sessions</span>
        <button
          onClick={createSession}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-orange/20 border border-orange/30 rounded-lg text-[12px] text-orange font-medium hover:bg-orange/30 active:scale-95 transition-all disabled:opacity-50"
        >
          {loading ? (
            <span className="w-3 h-3 border-2 border-orange/40 border-t-orange rounded-full animate-spin" />
          ) : (
            <span>+</span>
          )}
          New
        </button>
      </div>

      {/* Search */}
      <div className="flex-shrink-0 px-4 py-2 border-b border-border/50">
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search sessions..."
          className="w-full bg-surface/60 border border-border rounded-xl px-3 py-2 text-sm text-ink placeholder:text-muted/40 focus:outline-none focus:border-orange/40 transition-colors"
        />
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-center px-6">
            <p className="text-sm text-muted/50">No sessions yet.</p>
            <button
              onClick={createSession}
              className="text-orange text-sm font-medium"
            >
              Start a new chat →
            </button>
          </div>
        ) : (
          <div className="py-2">
            {filtered.map(session => (
              <div
                key={session.id}
                className={`flex items-center gap-3 px-4 py-3 border-b border-border/30 active:bg-surface/60 transition-colors ${
                  activeSessions === session.id ? 'bg-orange/5 border-l-2 border-l-orange' : ''
                }`}
              >
                <button
                  className="flex-1 text-left min-w-0"
                  onClick={() => loadSession(session.id)}
                >
                  <p className="text-sm font-medium text-ink truncate">
                    {session.title || 'Untitled Chat'}
                  </p>
                  <p className="text-[11px] text-muted/50 mt-0.5">
                    {formatDate(session.updated_at)}
                    {session.message_count > 0 && ` · ${session.message_count} msg${session.message_count !== 1 ? 's' : ''}`}
                  </p>
                </button>
                <button
                  onClick={() => deleteSession(session.id)}
                  disabled={deleting === session.id}
                  className="flex-shrink-0 w-7 h-7 flex items-center justify-center rounded-lg text-muted/40 hover:text-red-400 hover:bg-red-400/10 transition-colors"
                >
                  {deleting === session.id ? (
                    <span className="w-3 h-3 border-2 border-red-400/40 border-t-red-400 rounded-full animate-spin" />
                  ) : (
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
                    </svg>
                  )}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
