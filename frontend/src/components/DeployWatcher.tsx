import React, { useState, useEffect, useRef, useCallback } from 'react'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'

// ── Types ──────────────────────────────────────────────────────────────────────

export type WatchTarget = 'vercel' | 'railway'
type Phase = 'waiting' | 'building' | 'ready' | 'error' | 'stuck' | 'stopped'

interface TargetStatus {
  phase: Phase
  url: string
  dashboardUrl: string
  errorLines: string[]
  lastFingerprint: string
  sameCount: number
  startedAt: number
  deploymentId: string
}

export interface DeployWatcherProps {
  targets: WatchTarget[]
  vercelProjectId?: string
  onDismiss: () => void
}

// ── Constants ─────────────────────────────────────────────────────────────────

const POLL_MS: Record<WatchTarget, number> = { vercel: 8000, railway: 10000 }
const TERMINAL: Phase[] = ['ready', 'stuck', 'stopped']
const ICON: Record<WatchTarget, string> = { vercel: '▲', railway: '⬡' }

// ── Helpers ───────────────────────────────────────────────────────────────────

function fingerprint(lines: string[], fallback: string): string {
  if (!lines.length) return fallback
  return lines.slice(0, 8).join('\n')
    .replace(/:\d+:\d+/g, ':N:N')
    .replace(/\d+/g, '#')
}

function initStatus(): TargetStatus {
  return {
    phase: 'waiting',
    url: '',
    dashboardUrl: '',
    errorLines: [],
    lastFingerprint: '',
    sameCount: 0,
    startedAt: Date.now(),
    deploymentId: '',
  }
}

/** Color-classify a single build log line for the terminal display */
function lineColor(text: string): string {
  if (/error\s+ts\d+/i.test(text))                   return 'text-red-300 font-medium'
  if (/\.(tsx?|jsx?|py|go|rs):\d+:\d+/.test(text))   return 'text-amber-300'
  if (/^\s*[~^]+\s*$/.test(text))                    return 'text-red-500/60'
  if (/\b(error|failed)\b/i.test(text))              return 'text-red-400'
  return 'text-zinc-400'
}

// ── ElapsedBadge ──────────────────────────────────────────────────────────────

function ElapsedBadge({ startedAt }: { startedAt: number }) {
  const [secs, setSecs] = useState(() => Math.floor((Date.now() - startedAt) / 1000))
  useEffect(() => {
    const id = setInterval(() => setSecs(Math.floor((Date.now() - startedAt) / 1000)), 1000)
    return () => clearInterval(id)
  }, [startedAt])
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return (
    <span className="text-[9px] font-mono text-faint tabular-nums">
      {m > 0 ? `${m}m ${s}s` : `${s}s`}
    </span>
  )
}

// ── DeployWatcher ─────────────────────────────────────────────────────────────

export function DeployWatcher({ targets, vercelProjectId, onDismiss }: DeployWatcherProps) {
  const setPendingChatInput = useAppStore(s => s.setPendingChatInput)

  const [statuses, setStatuses] = useState<Record<string, TargetStatus>>(
    () => Object.fromEntries(targets.map(t => [t, initStatus()]))
  )
  const [copied, setCopied] = useState<string | null>(null)

  const activeRef = useRef<Record<string, boolean>>({})
  const timersRef  = useRef<Record<string, ReturnType<typeof setInterval>>>({})

  const stopTarget = useCallback((t: string) => {
    activeRef.current[t] = false
    clearInterval(timersRef.current[t])
    delete timersRef.current[t]
  }, [])

  const stopAll = useCallback(() => {
    targets.forEach(t => {
      stopTarget(t)
      setStatuses(prev => ({
        ...prev,
        [t]: { ...(prev[t] ?? initStatus()), phase: 'stopped' },
      }))
    })
  }, [targets, stopTarget])

  const poll = useCallback(async (target: WatchTarget) => {
    if (!activeRef.current[target]) return
    try {
      const data: any = target === 'vercel'
        ? await (api as any).deployWatch.vercel(vercelProjectId)
        : await (api as any).deployWatch.railway()
      if (!activeRef.current[target]) return

      setStatuses(prev => {
        const cur = prev[target] ?? initStatus()
        if (!data?.found) return { ...prev, [target]: { ...cur, phase: 'waiting' } }

        const raw = (data.state ?? '').toUpperCase()

        if (['READY', 'SUCCESS'].includes(raw)) {
          stopTarget(target)
          return {
            ...prev,
            [target]: {
              ...cur,
              phase: 'ready',
              url: data.url ?? '',
              dashboardUrl: data.dashboard_url ?? '',
            },
          }
        }

        if (['BUILDING', 'QUEUED', 'DEPLOYING', 'INITIALIZING'].includes(raw)) {
          return { ...prev, [target]: { ...cur, phase: 'building', url: data.url ?? '' } }
        }

        if (['ERROR', 'FAILED', 'CRASHED'].includes(raw)) {
          const lines: string[] = data.error_lines ?? []
          const fp   = fingerprint(lines, raw)
          const same =
            fp === cur.lastFingerprint && cur.lastFingerprint !== ''
              ? cur.sameCount + 1
              : 1
          const phase: Phase = same >= 3 ? 'stuck' : 'error'
          if (same >= 3) stopTarget(target)
          return {
            ...prev,
            [target]: {
              ...cur,
              phase,
              url: data.url ?? cur.url,
              dashboardUrl: data.dashboard_url ?? cur.dashboardUrl,
              errorLines: lines,
              lastFingerprint: fp,
              sameCount: same,
              deploymentId: data.deployment_id ?? '',
            },
          }
        }

        return prev
      })
    } catch {
      // Transient network error — keep polling
    }
  }, [vercelProjectId, stopTarget])

  useEffect(() => {
    targets.forEach(t => {
      activeRef.current[t] = true
      poll(t)
      timersRef.current[t] = setInterval(() => poll(t), POLL_MS[t])
    })
    return () => targets.forEach(stopTarget)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const askClaude = (target: WatchTarget, st: TargetStatus) => {
    const label = target === 'vercel' ? 'Vercel' : 'Railway'
    const msg = st.errorLines.length > 0
      ? `My ${label} build failed. Here are the errors:\n\n\`\`\`\n${st.errorLines.slice(0, 30).join('\n')}\n\`\`\`\n\nPlease diagnose and fix.`
      : `My ${label} build just failed (deploy ID: ${st.deploymentId || 'unknown'}). Can you look at recent changes and identify what's causing it?`
    setPendingChatInput(msg)
    onDismiss()
  }

  const copyErrors = async (target: string, lines: string[]) => {
    await navigator.clipboard.writeText(lines.join('\n'))
    setCopied(target)
    setTimeout(() => setCopied(null), 2000)
  }

  const allDone = targets.every(t => TERMINAL.includes(statuses[t]?.phase ?? 'waiting'))

  return (
    <div className="rounded-xl border border-border bg-surface shadow-xl overflow-hidden mt-2">

      {/* ── Header ────────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between px-3 py-2 bg-surface-alt/80 border-b border-border">
        <div className="flex items-center gap-2">
          <span className="text-[9px] font-bold tracking-widest uppercase text-muted">
            Deploy Watch
          </span>
          {!allDone && (
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
          )}
        </div>
        <div className="flex items-center gap-1">
          {!allDone && (
            <button
              onClick={stopAll}
              className="text-[10px] text-danger/70 hover:text-danger px-1.5 py-0.5 rounded border border-danger/20 hover:border-danger/40 transition-colors leading-none"
            >
              Stop
            </button>
          )}
          <button
            onClick={onDismiss}
            className="w-5 h-5 flex items-center justify-center rounded hover:bg-overlay text-faint hover:text-ink transition-colors text-[11px] leading-none"
          >
            ✕
          </button>
        </div>
      </div>

      {/* ── Target rows ───────────────────────────────────────────────────── */}
      <div className="divide-y divide-border/40">
        {targets.map(target => {
          const st         = statuses[target] ?? initStatus()
          const isBuilding = st.phase === 'waiting' || st.phase === 'building'
          const isFailed   = st.phase === 'error'   || st.phase === 'stuck'
          const isReady    = st.phase === 'ready'

          return (
            <div key={target} className="p-3 space-y-2">

              {/* Status row */}
              <div className="flex items-center gap-2.5 min-w-0">
                <span className="text-[13px] leading-none w-4 text-center flex-shrink-0">
                  {ICON[target]}
                </span>
                <div className="flex items-center gap-1.5 flex-1 min-w-0">
                  <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                    isReady    ? 'bg-emerald-400' :
                    isFailed   ? 'bg-red-400' :
                    isBuilding ? 'bg-amber-400 animate-pulse' :
                                 'bg-zinc-500'
                  }`} />
                  <span className={`text-[12px] font-semibold leading-none ${
                    isReady    ? 'text-emerald-400' :
                    isFailed   ? 'text-red-400'    :
                    isBuilding ? 'text-amber-400'  :
                                 'text-faint'
                  }`}>
                    {st.phase === 'waiting'  && 'Waiting for deploy…'}
                    {st.phase === 'building' && 'Building…'}
                    {st.phase === 'ready'    && 'Live ✓'}
                    {st.phase === 'error'    && 'Build failed'}
                    {st.phase === 'stuck'    && 'Stuck — same error 3×'}
                    {st.phase === 'stopped'  && 'Stopped'}
                  </span>
                  {isBuilding && st.startedAt > 0 && (
                    <ElapsedBadge startedAt={st.startedAt} />
                  )}
                </div>
                {isReady && st.url && (
                  <a
                    href={`https://${st.url}`}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[10px] text-accent hover:underline flex-shrink-0"
                  >
                    Open ↗
                  </a>
                )}
              </div>

              {/* ── Error block ─────────────────────────────────────────── */}
              {isFailed && (
                <div className="ml-6 space-y-2">

                  {/* Terminal window — shown when we have parsed lines */}
                  {st.errorLines.length > 0 ? (
                    <div className="rounded-lg overflow-hidden border border-red-900/50 bg-[#100808]">
                      <div className="flex items-center justify-between px-2.5 py-1 bg-[#1a0a0a] border-b border-red-900/30">
                        <span className="text-[9px] font-mono text-red-500/80 uppercase tracking-widest">
                          Build Error
                        </span>
                        <button
                          onClick={() => copyErrors(target, st.errorLines)}
                          className="text-[9px] font-medium text-faint hover:text-ink transition-colors"
                        >
                          {copied === target ? '✓ Copied' : 'Copy all'}
                        </button>
                      </div>
                      <div className="p-2.5 max-h-40 overflow-y-auto space-y-px">
                        {st.errorLines.slice(0, 20).map((line, i) => (
                          <p
                            key={i}
                            className={`text-[10px] font-mono leading-relaxed break-all ${lineColor(line)}`}
                          >
                            {line}
                          </p>
                        ))}
                        {st.errorLines.length > 20 && (
                          <p className="text-[9px] text-zinc-600 pt-1">
                            …{st.errorLines.length - 20} more lines
                          </p>
                        )}
                      </div>
                    </div>
                  ) : (
                    /* Fallback when log parsing yields nothing */
                    <div className="flex items-center justify-between px-2.5 py-2 rounded-lg bg-red-950/20 border border-red-900/30">
                      <span className="text-[10px] text-red-400/80">
                        Build failed — fetching full log…
                      </span>
                      {st.dashboardUrl && (
                        <a
                          href={st.dashboardUrl}
                          target="_blank"
                          rel="noreferrer"
                          className="text-[10px] text-accent hover:underline flex-shrink-0"
                        >
                          View ↗
                        </a>
                      )}
                    </div>
                  )}

                  {/* Circuit-breaker — shows when same error repeats across polls */}
                  {st.sameCount > 0 && (
                    <div className="flex items-center gap-1.5">
                      <span className="text-[9px] text-faint">Retry</span>
                      <div className="flex gap-0.5">
                        {[1, 2, 3].map(n => (
                          <span
                            key={n}
                            className={`h-1 w-4 rounded-full transition-colors ${
                              n <= st.sameCount ? 'bg-red-400' : 'bg-border'
                            }`}
                          />
                        ))}
                      </div>
                      {st.sameCount >= 3 && (
                        <span className="text-[9px] text-danger font-semibold">Blocked</span>
                      )}
                    </div>
                  )}

                  {/* Actions — always visible when build has failed */}
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => askClaude(target, st)}
                      className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[11px] font-semibold bg-accent text-white hover:bg-accent/90 active:scale-95 transition-all shadow-sm"
                    >
                      🔧 Ask Claude to fix
                    </button>
                    {st.dashboardUrl && (
                      <a
                        href={st.dashboardUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[10px] text-faint hover:text-ink transition-colors"
                      >
                        Dashboard ↗
                      </a>
                    )}
                  </div>
                </div>
              )}

            </div>
          )
        })}
      </div>

      {/* ── Footer ────────────────────────────────────────────────────────── */}
      {allDone && (
        <div className="px-3 py-1.5 border-t border-border/40 bg-surface-alt/40">
          <span className="text-[9px] text-faint">
            All targets settled · polling stopped
          </span>
        </div>
      )}

    </div>
  )
}
