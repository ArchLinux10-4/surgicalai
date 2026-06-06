import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import {
  CheckCircle, ErrorOutline, KeyboardArrowDown, KeyboardArrowUp,
  Refresh, Settings, Sync,
} from '@mui/icons-material'

// ── Types ─────────────────────────────────────────────────────────────────────

interface VercelStatus {
  connected: boolean
  username?: string
  email?: string
  avatar_url?: string
}

interface VercelProject {
  id: string
  name: string
  framework?: string
  updated_at?: number
  latest_state?: string
  latest_url?: string
  latest_created?: number
}

interface VercelDeployment {
  id: string
  url?: string
  name: string
  state: string
  created_at?: number
  ready_at?: number
  target?: string
  creator?: string
}

interface VercelLog {
  created?: number
  type: string
  text: string
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function stateBadge(state?: string) {
  const s = (state || '').toUpperCase()
  if (s === 'READY')
    return <span className="flex items-center gap-1 text-[10px] font-semibold text-emerald-400"><span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />Ready</span>
  if (s === 'ERROR' || s === 'FAILED')
    return <span className="flex items-center gap-1 text-[10px] font-semibold text-red-400"><span className="w-1.5 h-1.5 rounded-full bg-red-400 inline-block" />Failed</span>
  if (s === 'BUILDING' || s === 'INITIALIZING' || s === 'QUEUED')
    return <span className="flex items-center gap-1 text-[10px] font-semibold text-yellow-400"><Sync sx={{ fontSize: 10 }} className="animate-spin" />Building</span>
  if (s === 'CANCELED')
    return <span className="flex items-center gap-1 text-[10px] font-semibold text-faint">Canceled</span>
  return <span className="text-[10px] text-faint">{state || '—'}</span>
}

function relTime(ts?: number) {
  if (!ts) return ''
  const diff = Date.now() - ts
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

// ── Component ─────────────────────────────────────────────────────────────────

export function VercelPanel({ onOpenSettings }: { onOpenSettings?: () => void }) {
  const [status, setStatus] = useState<VercelStatus | null>(null)
  const [loadingStatus, setLoadingStatus] = useState(true)

  const [projects, setProjects] = useState<VercelProject[]>([])
  const [loadingProjects, setLoadingProjects] = useState(false)

  const [selectedProject, setSelectedProject] = useState<VercelProject | null>(null)
  const [deployments, setDeployments] = useState<VercelDeployment[]>([])
  const [loadingDeploys, setLoadingDeploys] = useState(false)

  const [expandedDeploy, setExpandedDeploy] = useState<string | null>(null)
  const [logs, setLogs] = useState<Record<string, VercelLog[]>>({})
  const [loadingLogs, setLoadingLogs] = useState<string | null>(null)

  // Load status on mount
  useEffect(() => {
    setLoadingStatus(true)
    ;(api as any).vercel.status()
      .then((s: VercelStatus) => setStatus(s))
      .catch(() => setStatus({ connected: false }))
      .finally(() => setLoadingStatus(false))
  }, [])

  // Load projects when connected
  useEffect(() => {
    if (!status?.connected) return
    setLoadingProjects(true)
    ;(api as any).vercel.projects()
      .then((d: { projects: VercelProject[] }) => setProjects(d.projects || []))
      .catch(() => toast.error('Failed to load Vercel projects'))
      .finally(() => setLoadingProjects(false))
  }, [status?.connected])

  const selectProject = useCallback(async (project: VercelProject) => {
    setSelectedProject(project)
    setDeployments([])
    setExpandedDeploy(null)
    setLoadingDeploys(true)
    try {
      const d: any = await (api as any).vercel.deployments(project.id)
      setDeployments(d.deployments || [])
    } catch (e: any) {
      toast.error('Failed to load deployments', e.message)
    } finally {
      setLoadingDeploys(false)
    }
  }, [])

  const toggleLogs = useCallback(async (deployId: string) => {
    if (expandedDeploy === deployId) {
      setExpandedDeploy(null)
      return
    }
    setExpandedDeploy(deployId)
    if (logs[deployId]) return
    setLoadingLogs(deployId)
    try {
      const d: any = await (api as any).vercel.logs(deployId)
      setLogs(prev => ({ ...prev, [deployId]: d.logs || [] }))
    } catch (e: any) {
      toast.error('Failed to load logs', e.message)
    } finally {
      setLoadingLogs(null)
    }
  }, [expandedDeploy, logs])

  const refreshProjects = () => {
    if (!status?.connected) return
    setLoadingProjects(true)
    setSelectedProject(null)
    setDeployments([])
    ;(api as any).vercel.projects()
      .then((d: { projects: VercelProject[] }) => setProjects(d.projects || []))
      .catch(() => {})
      .finally(() => setLoadingProjects(false))
  }

  // ── Loading ────────────────────────────────────────────────────────────────
  if (loadingStatus) {
    return (
      <div className="flex items-center justify-center h-20">
        <Sync sx={{ fontSize: 16 }} className="animate-spin text-faint" />
      </div>
    )
  }

  // ── Not connected ──────────────────────────────────────────────────────────
  if (!status?.connected) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4 px-5 py-8 text-center">
        <div className="w-14 h-14 rounded-2xl bg-surface-alt border border-border flex items-center justify-center">
          <span className="text-2xl">▲</span>
        </div>
        <div>
          <p className="text-[14px] font-semibold text-ink mb-1">Connect Vercel</p>
          <p className="text-[12px] text-faint leading-relaxed">
            Browse your projects, monitor deployments, and read build logs directly in SurgicalAI.
          </p>
        </div>
        <button
          onClick={onOpenSettings}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-accent text-white text-[13px] font-semibold hover:bg-accent/90 transition-colors"
        >
          <Settings sx={{ fontSize: 13 }} />
          Add Token in Settings
        </button>
        <p className="text-[11px] text-faint">
          Uses a personal access token — generate one at vercel.com/account/tokens
        </p>
      </div>
    )
  }

  // ── Connected ──────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2.5 px-3 py-2.5 border-b border-border bg-surface-alt/40">
        {status.avatar_url ? (
          <img src={status.avatar_url} alt="" className="w-6 h-6 rounded-full" />
        ) : (
          <div className="w-6 h-6 rounded-full bg-surface border border-border flex items-center justify-center text-[11px] font-bold text-ink">
            ▲
          </div>
        )}
        <div className="flex-1 min-w-0">
          <p className="text-[12px] font-semibold text-ink truncate">{status.username || status.email}</p>
          <p className="text-[10px] text-faint">Vercel</p>
        </div>
        <span className="flex items-center gap-1 text-[10px] text-emerald-400 font-medium">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
          Connected
        </span>
        <button
          onClick={refreshProjects}
          className="p-1 rounded hover:bg-overlay transition-colors"
          title="Refresh projects"
        >
          <Refresh sx={{ fontSize: 13 }} className="text-faint" />
        </button>
      </div>

      {/* Project list */}
      {!selectedProject && (
        <div className="flex-1 overflow-y-auto">
          {loadingProjects ? (
            <div className="flex items-center justify-center h-20">
              <Sync sx={{ fontSize: 14 }} className="animate-spin text-faint" />
            </div>
          ) : projects.length === 0 ? (
            <div className="flex items-center justify-center h-20 text-faint">
              <p className="text-[12px]">No projects found</p>
            </div>
          ) : (
            <div className="px-2 py-1 space-y-0.5">
              {projects.map(proj => (
                <button
                  key={proj.id}
                  onClick={() => selectProject(proj)}
                  className="w-full text-left px-2.5 py-2.5 rounded-lg hover:bg-overlay transition-colors group"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-[12px] font-semibold text-ink truncate flex-1">{proj.name}</span>
                    {stateBadge(proj.latest_state)}
                  </div>
                  <div className="flex items-center gap-3 mt-0.5">
                    {proj.framework && (
                      <span className="text-[10px] text-faint">{proj.framework}</span>
                    )}
                    {proj.latest_created && (
                      <span className="text-[10px] text-faint">{relTime(proj.latest_created)}</span>
                    )}
                    {proj.latest_url && (
                      <a
                        href={`https://${proj.latest_url}`}
                        target="_blank"
                        rel="noreferrer"
                        onClick={e => e.stopPropagation()}
                        className="text-[10px] text-accent hover:underline truncate"
                      >
                        {proj.latest_url}
                      </a>
                    )}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Deployment list */}
      {selectedProject && (
        <div className="flex flex-col flex-1 overflow-hidden">
          {/* Project header + back */}
          <div className="flex items-center gap-1.5 px-2 py-2 border-b border-border bg-surface-alt/20">
            <button
              onClick={() => { setSelectedProject(null); setDeployments([]) }}
              className="p-1 rounded hover:bg-overlay transition-colors text-muted hover:text-ink"
              title="Back to projects"
            >
              ←
            </button>
            <span className="text-[12px] font-semibold text-ink truncate flex-1">{selectedProject.name}</span>
            {loadingDeploys && <Sync sx={{ fontSize: 12 }} className="animate-spin text-faint" />}
          </div>

          <div className="flex-1 overflow-y-auto">
            {deployments.length === 0 && !loadingDeploys ? (
              <div className="flex items-center justify-center h-20 text-faint">
                <p className="text-[12px]">No deployments</p>
              </div>
            ) : (
              <div className="py-1">
                {deployments.map(dep => (
                  <div key={dep.id} className="border-b border-border/40 last:border-0">
                    {/* Deployment row */}
                    <button
                      onClick={() => toggleLogs(dep.id)}
                      className="w-full text-left px-3 py-2.5 hover:bg-overlay transition-colors group"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="flex-1 min-w-0">
                          <span className="text-[11px] font-semibold text-ink block truncate">
                            {dep.target === 'production' ? '🚀 Production' : `Preview`}
                          </span>
                          <span className="text-[10px] text-faint">{relTime(dep.created_at)}</span>
                        </span>
                        {stateBadge(dep.state)}
                        {expandedDeploy === dep.id
                          ? <KeyboardArrowUp sx={{ fontSize: 12 }} className="text-faint flex-shrink-0" />
                          : <KeyboardArrowDown sx={{ fontSize: 12 }} className="text-faint flex-shrink-0" />
                        }
                      </div>
                      {dep.url && (
                        <a
                          href={`https://${dep.url}`}
                          target="_blank"
                          rel="noreferrer"
                          onClick={e => e.stopPropagation()}
                          className="text-[10px] text-accent hover:underline truncate block mt-0.5"
                        >
                          {dep.url}
                        </a>
                      )}
                    </button>

                    {/* Logs panel */}
                    {expandedDeploy === dep.id && (
                      <div className="bg-[#0d0d0d] border-t border-border/40 px-3 py-2 max-h-64 overflow-y-auto">
                        {loadingLogs === dep.id ? (
                          <div className="flex items-center gap-2 text-faint text-[11px] py-2">
                            <Sync sx={{ fontSize: 11 }} className="animate-spin" />
                            Loading logs…
                          </div>
                        ) : (logs[dep.id] || []).length === 0 ? (
                          <p className="text-[11px] text-faint py-2">No log output</p>
                        ) : (
                          <div className="font-mono text-[10px] leading-relaxed space-y-0.5">
                            {(logs[dep.id] || []).map((log, i) => (
                              <div
                                key={i}
                                className={`${log.type === 'stderr' || log.text.toLowerCase().includes('error') ? 'text-red-400' : 'text-emerald-300/80'}`}
                              >
                                {log.text}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
