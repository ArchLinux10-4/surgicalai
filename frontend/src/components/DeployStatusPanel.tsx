import React, { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../api/client'

// ─── Inline SVGs ────────────────────────────────────────────
const IconCircle = ({ color }: { color: string }) => (
  <span className={`inline-block w-2 h-2 rounded-full ${color} flex-shrink-0`} />
)
const IconExternal = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
    <polyline points="15 3 21 3 21 9"/><line x1="10" x2="21" y1="14" y2="3"/>
  </svg>
)
const IconRefresh = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>
    <path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>
    <path d="M8 16H3v5"/>
  </svg>
)

export interface DeployStatus {
  vercel?: {
    status: 'building' | 'ready' | 'error' | 'unknown'
    url?: string
    created_at?: string
    error?: string
  }
  railway?: {
    status: 'building' | 'success' | 'failed' | 'unknown'
    url?: string
    created_at?: string
    error?: string
  }
}

function statusDot(s: string) {
  if (s === 'ready' || s === 'success') return 'bg-green-400'
  if (s === 'building') return 'bg-yellow-400 animate-pulse'
  if (s === 'error' || s === 'failed') return 'bg-red-400'
  return 'bg-gray-400'
}

function statusLabel(s: string) {
  const map: Record<string, string> = {
    ready: 'Live', success: 'Deployed', building: 'Building…', error: 'Failed', failed: 'Failed', unknown: 'Unknown'
  }
  return map[s] || s
}

function relativeTime(iso?: string) {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

interface Props {
  /** If provided, auto-starts polling immediately (e.g. after a commit) */
  autoStart?: boolean
  onClose?: () => void
}

export function DeployStatusPanel({ autoStart = false, onClose }: Props) {
  const [data, setData] = useState<DeployStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [polling, setPolling] = useState(autoStart)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const fetchStatus = useCallback(async () => {
    try {
      const res = await (api as any).deploy.status()
      setData(res)
      return res
    } catch {
      return null
    }
  }, [])

  // Initial load
  useEffect(() => {
    setLoading(true)
    fetchStatus().finally(() => setLoading(false))
  }, [fetchStatus])

  // Polling loop when active
  useEffect(() => {
    if (!polling) { if (pollRef.current) clearTimeout(pollRef.current); return }
    const tick = async () => {
      const res = await fetchStatus()
      const vDone = !res?.vercel || ['ready', 'error'].includes(res.vercel.status)
      const rDone = !res?.railway || ['success', 'failed'].includes(res.railway.status)
      if (vDone && rDone) { setPolling(false); return }
      pollRef.current = setTimeout(tick, 8000)
    }
    pollRef.current = setTimeout(tick, 5000)
    return () => { if (pollRef.current) clearTimeout(pollRef.current) }
  }, [polling, fetchStatus])

  const handleRefresh = async () => {
    setLoading(true)
    await fetchStatus()
    setLoading(false)
  }

  return (
    <div className="rounded-xl border border-border bg-surface p-3 space-y-2.5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-ink">Deploy Status</span>
        <div className="flex items-center gap-1">
          <button
            onClick={handleRefresh}
            className={`p-1 rounded hover:bg-overlay text-faint hover:text-ink transition ${loading ? 'animate-spin' : ''}`}
            title="Refresh"
          >
            <IconRefresh />
          </button>
          {onClose && (
            <button onClick={onClose} className="p-1 rounded hover:bg-overlay text-faint hover:text-ink transition text-xs">✕</button>
          )}
        </div>
      </div>

      {loading && !data && (
        <div className="text-xs text-faint py-2">Checking deploy status…</div>
      )}

      {data && (
        <div className="space-y-2">
          {/* Vercel */}
          {data.vercel && (
            <div className="flex items-center gap-2 rounded-lg bg-overlay px-2.5 py-2">
              <IconCircle color={statusDot(data.vercel.status)} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="text-xs font-medium text-ink">Vercel</span>
                  <span className={`text-[10px] font-medium ${
                    data.vercel.status === 'ready' ? 'text-green-400' :
                    data.vercel.status === 'building' ? 'text-yellow-400' :
                    data.vercel.status === 'error' ? 'text-red-400' : 'text-muted'
                  }`}>{statusLabel(data.vercel.status)}</span>
                  {data.vercel.created_at && (
                    <span className="text-[10px] text-faint">{relativeTime(data.vercel.created_at)}</span>
                  )}
                </div>
                {data.vercel.error && (
                  <div className="text-[10px] text-red-400 mt-0.5 truncate">{data.vercel.error}</div>
                )}
              </div>
              {data.vercel.url && (
                <a href={data.vercel.url} target="_blank" rel="noopener" className="text-accent hover:text-accent/80 flex-shrink-0">
                  <IconExternal />
                </a>
              )}
            </div>
          )}

          {/* Railway */}
          {data.railway && (
            <div className="flex items-center gap-2 rounded-lg bg-overlay px-2.5 py-2">
              <IconCircle color={statusDot(data.railway.status)} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="text-xs font-medium text-ink">Railway</span>
                  <span className={`text-[10px] font-medium ${
                    data.railway.status === 'success' ? 'text-green-400' :
                    data.railway.status === 'building' ? 'text-yellow-400' :
                    data.railway.status === 'failed' ? 'text-red-400' : 'text-muted'
                  }`}>{statusLabel(data.railway.status)}</span>
                  {data.railway.created_at && (
                    <span className="text-[10px] text-faint">{relativeTime(data.railway.created_at)}</span>
                  )}
                </div>
                {data.railway.error && (
                  <div className="text-[10px] text-red-400 mt-0.5 truncate">{data.railway.error}</div>
                )}
              </div>
              {data.railway.url && (
                <a href={data.railway.url} target="_blank" rel="noopener" className="text-accent hover:text-accent/80 flex-shrink-0">
                  <IconExternal />
                </a>
              )}
            </div>
          )}

          {!data.vercel && !data.railway && (
            <div className="text-xs text-faint py-1">
              No deploy services configured. Add Railway/Vercel tokens in Settings.
            </div>
          )}
        </div>
      )}

      {polling && (
        <div className="text-[10px] text-muted flex items-center gap-1">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-yellow-400 animate-pulse" />
          Watching for updates…
        </div>
      )}
    </div>
  )
}

/** Compact inline chip shown in chat after a commit */
export function DeployStatusChip() {
  const [data, setData] = useState<DeployStatus | null>(null)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    const load = async () => {
      try {
        const res = await (api as any).deploy.status()
        setData(res)
      } catch {}
    }
    load()
    const id = setInterval(load, 10000)
    return () => clearInterval(id)
  }, [])

  if (!data) return null

  const vercelOk = data.vercel?.status === 'ready'
  const railwayOk = data.railway?.status === 'success'
  const anyBuilding = data.vercel?.status === 'building' || data.railway?.status === 'building'
  const anyFailed = data.vercel?.status === 'error' || data.railway?.status === 'failed'

  const summaryColor = anyFailed ? 'text-red-400' : anyBuilding ? 'text-yellow-400' : 'text-green-400'
  const summaryDot = anyFailed ? 'bg-red-400' : anyBuilding ? 'bg-yellow-400 animate-pulse' : 'bg-green-400'
  const summaryText = anyFailed ? 'Deploy failed' : anyBuilding ? 'Deploying…' : 'Deployed'

  return (
    <div className="mt-2">
      <button
        onClick={() => setExpanded(e => !e)}
        className="flex items-center gap-1.5 text-[11px] text-muted hover:text-ink transition"
      >
        <span className={`w-1.5 h-1.5 rounded-full ${summaryDot}`} />
        <span className={summaryColor}>{summaryText}</span>
        {data.vercel && <span className="text-faint">(Vercel {statusLabel(data.vercel.status)})</span>}
        {data.railway && <span className="text-faint">(Railway {statusLabel(data.railway.status)})</span>}
        <span className="text-faint">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && (
        <div className="mt-1.5">
          <DeployStatusPanel />
        </div>
      )}
    </div>
  )
}
