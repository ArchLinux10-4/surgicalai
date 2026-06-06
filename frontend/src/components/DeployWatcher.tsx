import React, { useState, useEffect, useRef } from 'react'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import {
  BugReport, CheckCircle, Error as ErrorIcon,
  HourglassEmpty, OpenInNew, StopCircle,
} from '@mui/icons-material'

// ── Types ──────────────────────────────────────────────────────────────────────

type Target = 'vercel' | 'railway'
type TargetState = 'waiting' | 'building' | 'ready' | 'error' | 'stuck' | 'stopped'

interface TargetStatus {
  state: TargetState
  url: string
  errorLines: string[]
  lastFingerprint: string
  sameCount: number
  dashboardUrl?: string
}

export interface DeployWatcherProps {
  /** Which platforms to watch */
  targets: Target[]
  /** Optional Vercel project ID filter */
  vercelProjectId?: string
  onDismiss: () => void
}

// ── Constants ─────────────────────────────────────────────────────────────────

const POLL_MS: Record<Target, number> = { vercel: 8000, railway: 10000 }

const STATE_LABEL: Record<TargetState, string> = {
  waiting:  'Waiting for deploy…',
  building: 'Building…',
  ready:    'Ready ✓',
  error:    'Build failed',
  stuck:    'Stuck — same error 3× in a row',
  stopped:  'Stopped',
}

const TERMINAL: TargetState[] = ['ready', 'stuck', 'stopped']

// ── Fingerprint helper ────────────────────────────────────────────────────────

function fingerprint(lines: string[], fallback: string): string {
  if (!lines.length) return fallback
  // Strip column/line numbers so the same error matches across deploys
  return lines
    .slice(0, 8)
    .join('\n')
    .replace(/:\d+:\d+/g, ':N:N')
    .replace(/\d+/g, '#')
}

// ── Default status ────────────────────────────────────────────────────────────

function defaultStatus(state: TargetState = 'waiting'): TargetStatus {
  return { state, url: '', errorLines: [], lastFingerprint: '', sameCount: 0 }
}

// ── Component ─────────────────────────────────────────────────────────────────

export function DeployWatcher({ targets, vercelProjectId, onDismiss }: DeployWatcherProps) {
  const setPendingChatInput = useAppStore(s => s.setPendingChatInput)

  const [statuses, setStatuses] = useState<Record<string, TargetStatus>>(
    () => Object.fromEntries(targets.map(t => [t, defaultStatus()]))
  )

  // Mutable refs to control polling without triggering re-renders
  const activeRef = useRef<Record<string, boolean>>({})
  const intervalRefs = useRef<Record<string, ReturnType<typeof setInterval>>>({})

  const stopTarget = (target: string) => {
    activeRef.current[target] = false
    clearInterval(intervalRefs.current[target])
    delete intervalRefs.current[target]
  }

  const stopAll = () => {
    targets.forEach(t => {
      stopTarget(t)
      setStatuses(prev => ({
        ...prev,
        [t]: { ...(prev[t] || defaultStatus()), state: 'stopped' },
      }))
    })
  }

  // Single poll tick for a target
  const pollTarget = async (target: Target) => {
    if (!activeRef.current[target]) return
    try {
      const data: any = target === 'vercel'
        ? await (api as any).deployWatch.vercel(vercelProjectId)
        : await (api as any).deployWatch.railway()

      if (!activeRef.current[target]) return  // stopped while awaiting

      setStatuses(prev => {
        const cur = prev[target] || defaultStatus()
        if (!data.found) return { ...prev, [target]: { ...cur, state: 'waiting' } }

        const raw = (data.state || '').toUpperCase()

        if (['READY', 'SUCCESS'].includes(raw)) {
          return { ...prev, [target]: { ...cur, state: 'ready', url: data.url || cur.url } }
        }

        if (['BUILDING', 'QUEUED', 'DEPLOYING', 'INITIALIZING'].includes(raw)) {
          return { ...prev, [target]: { ...cur, state: 'building', url: data.url || cur.url } }
        }

        if (['ERROR', 'FAILED', 'CRASHED'].includes(raw)) {
          const lines: string[] = data.error_lines || []
          const fp = fingerprint(lines, raw)
          const sameCount =
            fp === cur.lastFingerprint && cur.lastFingerprint !== ''
              ? cur.sameCount + 1
              : 1
          const newState: TargetState = sameCount >= 3 ? 'stuck' : 'error'
          return {
            ...prev,
            [target]: {
              ...cur,
              state: newState,
              url: data.url || cur.url,
              errorLines: lines,
              lastFingerprint: fp,
              sameCount,
              dashboardUrl: data.dashboard_url,
            },
          }
        }

        return prev
      })
    } catch {
      // Transient network error — keep polling
    }
  }

  // Auto-stop targets that reach a terminal state
  useEffect(() => {
    for (const target of targets) {
      const st = statuses[target]
      if (st && TERMINAL.includes(st.state) && activeRef.current[target]) {
        stopTarget(target)
      }
    }
  }, [statuses])

  // Start polling on mount; clean up on unmount
  useEffect(() => {
    for (const target of targets) {
      activeRef.current[target] = true
      pollTarget(target)  // immediate first poll
      intervalRefs.current[target] = setInterval(
        () => pollTarget(target),
        POLL_MS[target]
      )
    }
    return () => { targets.forEach(stopTarget) }
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  // Pre-fill chat input with error context so user can review before sending
  const askClaude = (target: Target, lines: string[]) => {
    const label = target === 'vercel' ? 'Vercel' : 'Railway'
    const excerpt = lines.slice(0, 25).join('\n')
    setPendingChatInput(
      `🚨 ${label} build failed. Please diagnose and fix:\n\n\`\`\`\n${excerpt}\n\`\`\``
    )
  }

  const allDone = targets.every(t => TERMINAL.includes(statuses[t]?.state || 'waiting'))

  return (
    <div className="mt-2 border border-border rounded-xl bg-surface/50 p-2.5 space-y-2.5">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold text-ink">Deploy Watch</span>
        <div className="flex items-center gap-1.5">
          {!allDone && (
            <button
              onClick={stopAll}
              className="flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] text-danger/80 hover:text-danger border border-danger/20 hover:border-danger/40 transition-colors"
            >
              <StopCircle sx={{ fontSize: 10 }} /> Stop
            </button>
          )}
          <button
            onClick={onDismiss}
            className="text-faint hover:text-ink text-[11px] px-1 leading-none transition-colors"
          >
            ✕
          </button>
        </div>
      </div>

      {/* Target status rows */}
      {targets.map(target => {
        const st = statuses[target] || defaultStatus()
        const isBuilding = ['waiting', 'building'].includes(st.state)
        const isFailed   = ['error', 'stuck'].includes(st.state)

        return (
          <div key={target} className="space-y-1">
            {/* Status line */}
            <div className="flex items-center gap-2 min-w-0">
              <span className="text-[10px] font-semibold text-muted capitalize w-12 flex-shrink-0">
                {target}
              </span>
              <span className={`flex items-center gap-1 text-[10px] font-medium flex-1 min-w-0 truncate ${
                st.state === 'ready'  ? 'text-emerald-400' :
                isFailed             ? 'text-danger'       :
                isBuilding           ? 'text-amber-400'    : 'text-faint'
              }`}>
                {isBuilding           && <HourglassEmpty sx={{ fontSize: 9 }} className="animate-spin flex-shrink-0" />}
                {st.state === 'ready' && <CheckCircle    sx={{ fontSize: 9 }} className="flex-shrink-0" />}
                {isFailed             && <ErrorIcon      sx={{ fontSize: 9 }} className="flex-shrink-0" />}
                {STATE_LABEL[st.state]}
              </span>
              {st.url && (
                <a
                  href={`https://${st.url}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-0.5 text-[10px] text-accent hover:underline flex-shrink-0"
                  title="Open deployment"
                >
                  <OpenInNew sx={{ fontSize: 9 }} />
                </a>
              )}
            </div>

            {/* Error lines for Vercel */}
            {isFailed && st.errorLines.length > 0 && (
              <div className="ml-14 space-y-1">
                <div className="bg-danger/5 border border-danger/20 rounded-lg p-1.5 max-h-28 overflow-y-auto">
                  {st.errorLines.slice(0, 8).map((line, i) => (
                    <p key={i} className="text-[10px] font-mono text-danger/90 leading-[1.4] break-words">
                      {line}
                    </p>
                  ))}
                  {st.errorLines.length > 8 && (
                    <p className="text-[9px] text-faint mt-0.5">
                      +{st.errorLines.length - 8} more lines
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                  <button
                    onClick={() => askClaude(target, st.errorLines)}
                    className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium bg-accent/10 text-accent border border-accent/20 hover:bg-accent/20 transition-colors"
                  >
                    <BugReport sx={{ fontSize: 10 }} /> Ask Claude to fix
                  </button>
                  {st.dashboardUrl && (
                    <a
                      href={st.dashboardUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-0.5 text-[10px] text-faint hover:text-accent transition-colors"
                    >
                      <OpenInNew sx={{ fontSize: 9 }} /> Dashboard
                    </a>
                  )}
                </div>
                {st.state === 'stuck' && (
                  <p className="text-[9px] text-danger/60 leading-tight">
                    Same error across 3 consecutive deploys — Claude may need a different approach.
                  </p>
                )}
              </div>
            )}

            {/* Failed but no error lines (Railway or unavailable) */}
            {isFailed && st.errorLines.length === 0 && (
              <div className="ml-14 space-y-0.5">
                <p className="text-[10px] text-muted">Build failed. Check dashboard for full logs.</p>
                {st.dashboardUrl && (
                  <a
                    href={st.dashboardUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-0.5 text-[10px] text-accent hover:underline"
                  >
                    <OpenInNew sx={{ fontSize: 9 }} /> Open Railway dashboard
                  </a>
                )}
              </div>
            )}
          </div>
        )
      })}

      {/* All terminal */}
      {allDone && (
        <p className="text-[9px] text-faint text-center pt-0.5 border-t border-border/40">
          All targets finished — polling stopped
        </p>
      )}
    </div>
  )
}
