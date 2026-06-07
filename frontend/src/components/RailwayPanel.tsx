import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api/client'
import { DeployWatcher } from './DeployWatcher'
import { OpenInNew, Refresh, Train } from '@mui/icons-material'

// ── Status dot helpers ────────────────────────────────────────────────────────
const STATUS_DOT: Record<string, string> = {
  SUCCESS:     'bg-green-500',
  COMPLETE:    'bg-green-500',
  DEPLOYING:   'bg-yellow-400 animate-pulse',
  BUILDING:    'bg-yellow-400 animate-pulse',
  INITIALIZING:'bg-yellow-400 animate-pulse',
  QUEUED:      'bg-blue-400 animate-pulse',
  FAILED:      'bg-red-500',
  CRASHED:     'bg-red-500',
  REMOVED:     'bg-gray-500',
}
const STATUS_LABEL: Record<string, string> = {
  SUCCESS:     'Success',
  COMPLETE:    'Complete',
  DEPLOYING:   'Deploying',
  BUILDING:    'Building',
  INITIALIZING:'Starting',
  QUEUED:      'Queued',
  FAILED:      'Failed',
  CRASHED:     'Crashed',
  REMOVED:     'Removed',
}
const ACTIVE_STATES = new Set(['DEPLOYING', 'BUILDING', 'INITIALIZING', 'QUEUED'])

function StatusDot({ status }: { status: string }) {
  const upper = (status || '').toUpperCase()
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${STATUS_DOT[upper] ?? 'bg-gray-400'}`}
      title={STATUS_LABEL[upper] ?? status}
    />
  )
}

function relTime(iso: string) {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  return d === 1 ? 'yesterday' : `${d}d ago`
}

// ── Project row ───────────────────────────────────────────────────────────────
function ProjectRow({ project, selected, onClick }: {
  project: any
  selected: boolean
  onClick: () => void
}) {
  const upper = (project.latest_status || '').toUpperCase()
  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-3 py-2.5 rounded-lg border transition-all ${
        selected
          ? 'border-red-500/50 bg-red-500/10'
          : 'border-border hover:border-border/80 hover:bg-overlay'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13px] font-medium text-ink truncate">{project.name}</span>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          {upper && <StatusDot status={upper} />}
          {upper && (
            <span className={`text-[11px] ${
              upper === 'FAILED' || upper === 'CRASHED' ? 'text-red-400' :
              ACTIVE_STATES.has(upper) ? 'text-yellow-400' :
              upper === 'SUCCESS' || upper === 'COMPLETE' ? 'text-green-400' :
              'text-muted'
            }`}>{STATUS_LABEL[upper] ?? upper}</span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-3 mt-1">
        {project.services?.length > 0 && (
          <span className="text-[11px] text-faint">{project.services.length} service{project.services.length !== 1 ? 's' : ''}</span>
        )}
        {project.latest_created && (
          <span className="text-[11px] text-faint">{relTime(project.latest_created)}</span>
        )}
      </div>
    </button>
  )
}

// ── Deployment row ────────────────────────────────────────────────────────────
function DeploymentRow({ dep, projectId }: { dep: any; projectId: string }) {
  const upper = (dep.status || '').toUpperCase()
  const dashUrl = `https://railway.com/project/${projectId}`
  return (
    <div className="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-overlay transition-colors group">
      <StatusDot status={upper} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className={`text-[12px] font-medium ${
            upper === 'FAILED' || upper === 'CRASHED' ? 'text-red-400' :
            ACTIVE_STATES.has(upper) ? 'text-yellow-400' :
            upper === 'SUCCESS' || upper === 'COMPLETE' ? 'text-green-400' :
            'text-muted'
          }`}>{STATUS_LABEL[upper] ?? upper}</span>
          {dep.service_name && (
            <span className="text-[11px] text-faint truncate">{dep.service_name}</span>
          )}
        </div>
        <div className="flex items-center gap-2 mt-0.5">
          {dep.environment_name && (
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-overlay border border-border text-faint">{dep.environment_name}</span>
          )}
          {dep.created_at && (
            <span className="text-[11px] text-faint">{relTime(dep.created_at)}</span>
          )}
        </div>
      </div>
      <a
        href={dashUrl}
        target="_blank"
        rel="noreferrer"
        className="opacity-0 group-hover:opacity-100 text-faint hover:text-ink transition-all"
        title="Open in Railway"
      >
        <OpenInNew sx={{ fontSize: 12 }} />
      </a>
    </div>
  )
}

// ── Main panel ────────────────────────────────────────────────────────────────
interface RailwayPanelProps {
  onOpenSettings: () => void
}

export function RailwayPanel({ onOpenSettings }: RailwayPanelProps) {
  const [status, setStatus]               = useState<any>(null)
  const [loading, setLoading]             = useState(true)
  const [projects, setProjects]           = useState<any[]>([])
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [selectedProject, setSelectedProject] = useState<any>(null)
  const [deployments, setDeployments]     = useState<any[]>([])
  const [depsLoading, setDepsLoading]     = useState(false)
  const [watching, setWatching]           = useState(false)
  const [error, setError]                 = useState('')

  // Load status on mount
  useEffect(() => {
    setLoading(true)
    ;(api as any).railway.status()
      .then((s: any) => {
        setStatus(s)
        if (s?.connected) loadProjects()
      })
      .catch(() => setStatus({ connected: false }))
      .finally(() => setLoading(false))
  }, [])

  const loadProjects = useCallback(() => {
    setProjectsLoading(true)
    setError('')
    ;(api as any).railway.projects()
      .then((d: any) => {
        const list = d.projects || []
        setProjects(list)
        // Auto-watch if any project is actively deploying
        const active = list.find((p: any) => ACTIVE_STATES.has((p.latest_status || '').toUpperCase()))
        if (active) setWatching(true)
      })
      .catch((e: any) => setError(e.message || 'Failed to load projects'))
      .finally(() => setProjectsLoading(false))
  }, [])

  const selectProject = useCallback((project: any) => {
    if (selectedProject?.id === project.id) {
      setSelectedProject(null)
      setDeployments([])
      return
    }
    setSelectedProject(project)
    setDepsLoading(true)
    ;(api as any).railway.projectDeployments(project.id)
      .then((d: any) => setDeployments(d.deployments || []))
      .catch((e: any) => setError(e.message || 'Failed to load deployments'))
      .finally(() => setDepsLoading(false))
  }, [selectedProject])

  // ── Not connected ──────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center justify-center h-24 text-faint text-[12px]">
        Loading…
      </div>
    )
  }

  if (!status?.connected) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 p-6 h-full text-center">
        <div className="w-10 h-10 rounded-xl bg-red-500/15 flex items-center justify-center">
          <Train sx={{ fontSize: 20 }} className="text-red-400" />
        </div>
        <div>
          <div className="text-[13px] font-semibold text-ink mb-1">Railway not connected</div>
          <div className="text-[11px] text-muted leading-relaxed">
            Add your Railway token in Settings to monitor deployments and services.
          </div>
        </div>
        <button
          onClick={onOpenSettings}
          className="btn-primary text-[12px] px-4 py-2"
        >
          Open Settings → Railway
        </button>
      </div>
    )
  }

  // ── Deploy watcher overlay ─────────────────────────────────────────────────
  if (watching) {
    return (
      <div className="flex flex-col h-full overflow-hidden">
        <DeployWatcher
          targets={['railway']}
          railwayProjectId={selectedProject?.id || undefined}
          onDismiss={() => { setWatching(false); loadProjects() }}
        />
      </div>
    )
  }

  // ── Connected view ─────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-3 pt-3 pb-2 flex-shrink-0">
        <div>
          <div className="text-[12px] font-semibold text-ink">
            {status.name || status.email || 'Railway'}
          </div>
          {status.email && status.name && (
            <div className="text-[11px] text-faint">{status.email}</div>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={loadProjects}
            disabled={projectsLoading}
            className="flex items-center justify-center w-7 h-7 rounded-lg text-muted hover:text-ink hover:bg-overlay transition disabled:opacity-40"
            title="Refresh"
          >
            <Refresh sx={{ fontSize: 14, ...(projectsLoading ? { className: 'animate-spin' } : {}) }} />
          </button>
          <a
            href="https://railway.com/dashboard"
            target="_blank"
            rel="noreferrer"
            className="flex items-center justify-center w-7 h-7 rounded-lg text-muted hover:text-ink hover:bg-overlay transition"
            title="Open Railway dashboard"
          >
            <OpenInNew sx={{ fontSize: 13 }} />
          </a>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="mx-3 mb-2 text-[11px] text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
          {error}
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto min-h-0 px-2 pb-3 space-y-1.5">
        {projectsLoading && projects.length === 0 && (
          <div className="text-center text-[12px] text-faint py-6">Loading projects…</div>
        )}

        {!projectsLoading && projects.length === 0 && (
          <div className="text-center text-[12px] text-faint py-6">No projects found</div>
        )}

        {projects.map((project) => (
          <div key={project.id}>
            <ProjectRow
              project={project}
              selected={selectedProject?.id === project.id}
              onClick={() => selectProject(project)}
            />

            {/* Expanded: deployments list + watch button */}
            {selectedProject?.id === project.id && (
              <div className="mt-1 ml-2 mr-0 border-l-2 border-red-500/20 pl-2">
                {/* Watch button */}
                <button
                  onClick={() => setWatching(true)}
                  className="w-full flex items-center justify-center gap-1.5 text-[11px] text-red-400 border border-red-500/30 rounded-lg py-1.5 mb-2 hover:bg-red-500/10 transition-colors"
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
                  Watch latest deploy
                </button>

                {/* Deployments */}
                {depsLoading ? (
                  <div className="text-[11px] text-faint text-center py-3">Loading deployments…</div>
                ) : deployments.length === 0 ? (
                  <div className="text-[11px] text-faint text-center py-3">No deployments found</div>
                ) : (
                  <div className="space-y-0.5">
                    {deployments.map((dep) => (
                      <DeploymentRow key={dep.id} dep={dep} projectId={project.id} />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Footer: disconnect link */}
      <div className="px-3 py-2 border-t border-border flex-shrink-0">
        <button
          onClick={onOpenSettings}
          className="text-[11px] text-faint hover:text-muted transition-colors"
        >
          Manage in Settings →
        </button>
      </div>
    </div>
  )
}
