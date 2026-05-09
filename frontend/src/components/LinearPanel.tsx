import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'

// ─── Inline SVGs (no CDN) ───────────────────────────────────
const IconLink = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>
    <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>
  </svg>
)
const IconUnlink = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="m18.84 12.25 1.72-1.71h-.02a5.004 5.004 0 0 0-.12-7.07 5.006 5.006 0 0 0-6.95 0l-1.72 1.71"/>
    <path d="m5.17 11.75-1.71 1.71a5.004 5.004 0 0 0 .12 7.07 5.006 5.006 0 0 0 6.95 0l1.71-1.71"/>
    <line x1="8" x2="8" y1="2" y2="5"/><line x1="2" x2="5" y1="8" y2="8"/>
    <line x1="16" x2="16" y1="19" y2="22"/><line x1="19" x2="22" y1="16" y2="16"/>
  </svg>
)
const IconCheck = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="20 6 9 17 4 12"/>
  </svg>
)
const IconRefresh = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>
    <path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>
    <path d="M8 16H3v5"/>
  </svg>
)
const IconPlus = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="12" x2="12" y1="5" y2="19"/><line x1="5" x2="19" y1="12" y2="12"/>
  </svg>
)
const IconEye = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
    <circle cx="12" cy="12" r="3"/>
  </svg>
)

// ─── State badge colours ────────────────────────────────────
function StateBadge({ state }: { state: string }) {
  const lower = state?.toLowerCase() || ''
  let cls = 'bg-overlay text-muted'
  if (lower.includes('progress') || lower.includes('started')) cls = 'bg-blue-500/15 text-blue-400'
  else if (lower.includes('done') || lower.includes('complete')) cls = 'bg-green-500/15 text-green-400'
  else if (lower.includes('cancel') || lower.includes('won')) cls = 'bg-red-500/15 text-red-400'
  else if (lower.includes('todo') || lower.includes('backlog')) cls = 'bg-overlay text-muted'
  return (
    <span className={`inline-block text-[10px] px-1.5 py-0.5 rounded font-medium ${cls}`}>
      {state || 'Unknown'}
    </span>
  )
}

// ─── Priority dot ────────────────────────────────────────────
function PriorityDot({ priority }: { priority: number }) {
  const colors = ['text-faint', 'text-red-400', 'text-orange-400', 'text-yellow-400', 'text-muted']
  return <span className={`text-[10px] font-bold ${colors[priority] || colors[0]}`} title={['No', 'Urgent', 'High', 'Medium', 'Low'][priority] || ''}>●</span>
}

interface Issue {
  id: string
  identifier: string
  title: string
  state: string
  priority: number
  url: string
  description?: string
  team?: string
}

export function LinearPanel({ onOpenSettings }: { onOpenSettings?: () => void }) {
  const { activeSessions, sendLinearIssue } = useAppStore()
  const [status, setStatus] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [token, setToken] = useState('')
  const [connecting, setConnecting] = useState(false)
  const [connectMsg, setConnectMsg] = useState('')
  const [issues, setIssues] = useState<Issue[]>([])
  const [issuesLoading, setIssuesLoading] = useState(false)
  const [teams, setTeams] = useState<any[]>([])
  const [selectedTeam, setSelectedTeam] = useState<string>('')
  const [stateFilter, setStateFilter] = useState<string>('In Progress')
  const [search, setSearch] = useState('')
  const [completingId, setCompletingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const s = await (api as any).linear.status()
      setStatus(s)
      if (s?.connected) {
        const t = await (api as any).linear.teams()
        setTeams(t?.teams || [])
      }
    } catch { setStatus({ connected: false }) }
    setLoading(false)
  }, [])

  const loadIssues = useCallback(async () => {
    if (!status?.connected) return
    setIssuesLoading(true)
    try {
      const res = await (api as any).linear.issues({ team_id: selectedTeam || undefined, state: stateFilter || undefined, limit: 50 })
      setIssues(res?.issues || [])
    } catch { setIssues([]) }
    setIssuesLoading(false)
  }, [status?.connected, selectedTeam, stateFilter])

  useEffect(() => { load() }, [load])
  useEffect(() => { if (status?.connected) loadIssues() }, [status?.connected, loadIssues])

  const handleConnect = async () => {
    if (!token.trim()) return
    setConnecting(true)
    setConnectMsg('')
    try {
      await (api as any).linear.connect(token.trim())
      setConnectMsg('Connected!')
      setToken('')
      await load()
    } catch (e: any) {
      setConnectMsg(e.message || 'Connection failed')
    }
    setConnecting(false)
  }

  const handleDisconnect = async () => {
    try { await (api as any).linear.disconnect() } catch {}
    setStatus({ connected: false })
    setIssues([])
  }

  const handleLoadIntoChat = (issue: Issue) => {
    if (sendLinearIssue) sendLinearIssue(issue)
  }

  const handleComplete = async (issue: Issue) => {
    setCompletingId(issue.id)
    try {
      await (api as any).linear.complete(issue.id)
      setIssues(prev => prev.filter(i => i.id !== issue.id))
    } catch {}
    setCompletingId(null)
  }

  const filtered = issues.filter(i =>
    !search || i.title.toLowerCase().includes(search.toLowerCase()) || i.identifier.toLowerCase().includes(search.toLowerCase())
  )

  if (loading) return (
    <div className="flex-1 flex items-center justify-center text-faint text-xs">Loading…</div>
  )

  if (!status?.connected) return (
    <div className="flex flex-col gap-3 p-3">
      <div className="text-xs text-muted leading-relaxed">
        Connect Linear to pull issues into context, and mark them done after committing.
      </div>
      <input
        type="password"
        value={token}
        onChange={e => setToken(e.target.value)}
        onKeyDown={e => e.key === 'Enter' && handleConnect()}
        placeholder="Linear API token…"
        className="input text-xs"
        autoComplete="off"
      />
      <button
        onClick={handleConnect}
        disabled={connecting || !token.trim()}
        className="btn-primary text-xs py-1.5 disabled:opacity-40"
      >
        {connecting ? 'Connecting…' : 'Connect Linear'}
      </button>
      {connectMsg && <div className="text-xs text-muted">{connectMsg}</div>}
      <a
        href="https://linear.app/settings/api"
        target="_blank"
        rel="noopener"
        className="text-[11px] text-accent hover:underline"
      >
        Get your Linear API token →
      </a>
    </div>
  )

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border flex-shrink-0">
        <div className="flex items-center gap-1.5 text-xs text-green-400">
          <IconLink />
          <span className="font-medium truncate max-w-[120px]">{status.workspace || 'Linear'}</span>
        </div>
        <div className="flex items-center gap-1">
          <button onClick={loadIssues} className="p-1 rounded hover:bg-overlay text-faint hover:text-ink transition" title="Refresh">
            <IconRefresh />
          </button>
          <button onClick={handleDisconnect} className="p-1 rounded hover:bg-overlay text-faint hover:text-red-400 transition" title="Disconnect">
            <IconUnlink />
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-col gap-1.5 px-3 py-2 border-b border-border flex-shrink-0">
        <input
          type="text"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search issues…"
          className="input text-xs py-1"
        />
        <div className="flex gap-1.5">
          {teams.length > 0 && (
            <select
              value={selectedTeam}
              onChange={e => setSelectedTeam(e.target.value)}
              className="input text-xs py-1 flex-1"
            >
              <option value="">All teams</option>
              {teams.map((t: any) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
          )}
          <select
            value={stateFilter}
            onChange={e => setStateFilter(e.target.value)}
            className="input text-xs py-1 flex-1"
          >
            <option value="">All states</option>
            <option value="In Progress">In Progress</option>
            <option value="Todo">Todo</option>
            <option value="Backlog">Backlog</option>
            <option value="Done">Done</option>
          </select>
        </div>
      </div>

      {/* Issue list */}
      <div className="flex-1 overflow-y-auto">
        {issuesLoading ? (
          <div className="flex items-center justify-center py-8 text-faint text-xs">Loading issues…</div>
        ) : filtered.length === 0 ? (
          <div className="flex items-center justify-center py-8 text-faint text-xs">No issues found</div>
        ) : (
          <div className="divide-y divide-border">
            {filtered.map(issue => (
              <div key={issue.id} className="px-3 py-2.5 hover:bg-overlay transition group">
                <div className="flex items-start justify-between gap-1.5">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <PriorityDot priority={issue.priority} />
                      <span className="text-[10px] text-faint font-mono">{issue.identifier}</span>
                      <StateBadge state={issue.state} />
                    </div>
                    <div className="text-xs text-ink leading-snug line-clamp-2">{issue.title}</div>
                  </div>
                </div>
                <div className="flex items-center gap-1 mt-1.5 opacity-0 group-hover:opacity-100 transition">
                  <button
                    onClick={() => handleLoadIntoChat(issue)}
                    className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent hover:bg-accent/25 transition"
                    title="Load into chat as context"
                  >
                    <IconPlus /> Load
                  </button>
                  <a
                    href={issue.url}
                    target="_blank"
                    rel="noopener"
                    className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-overlay text-muted hover:text-ink transition"
                  >
                    <IconEye /> View
                  </a>
                  <button
                    onClick={() => handleComplete(issue)}
                    disabled={completingId === issue.id}
                    className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-green-500/10 text-green-400 hover:bg-green-500/20 transition disabled:opacity-40"
                    title="Mark done"
                  >
                    <IconCheck /> {completingId === issue.id ? '…' : 'Done'}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
