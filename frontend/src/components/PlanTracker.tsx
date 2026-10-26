import { useEffect, useRef, useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { AssistantMarkdown } from './AssistantMarkdown'
import { ProgressMeter } from './ProgressMeter'
import type { PlanTask, PlanPhase, AgentTaskStatus } from '../types'
import { FileDownload } from '@mui/icons-material'

/**
 * Chat Plan checklist + saved markdown document. Separate from
 * AgentMissionControl — no auto-run, no 8s dismiss, own store slice.
 */
function StatusDot({ status }: { status: AgentTaskStatus }) {
  if (status === 'running') {
    return <span className="inline-block w-2 h-2 rounded-full bg-accent animate-pulse" />
  }
  if (status === 'done') {
    return <span className="inline-block w-2 h-2 rounded-full bg-success" />
  }
  if (status === 'blocked' || status === 'error') {
    return <span className="inline-block w-2 h-2 rounded-full bg-danger" />
  }
  if (status === 'cancelled') {
    return <span className="inline-block w-2 h-2 rounded-full bg-faint" />
  }
  return <span className="inline-block w-2 h-2 rounded-full border border-faint" />
}

function phaseLabel(phase: PlanPhase, done: number, total: number) {
  if (phase === 'implementing') return `Working — ${done} of ${total} done`
  if (phase === 'complete') return `Complete — ${total} of ${total}`
  if (phase === 'blocked') return `Needs work — ${done}/${total} covered`
  if (phase === 'ready') return `Ready — ${total} step${total !== 1 ? 's' : ''}`
  return ''
}

// Module-level so a second mounted tracker cannot start the same build twice,
// and a request made before the Plan tab mounts is still picked up.
let buildRequested = false
let buildInFlight = false
const PLAN_BUILD_EVENT = 'surgicalai:plan-build'

/** Start the saved plan build from outside the Plan tab (the composer button). */
export function requestPlanBuild() {
  buildRequested = true
  window.dispatchEvent(new Event(PLAN_BUILD_EVENT))
}

/** Strip the machine fence from the displayed document — checklist owns steps. */
function displayMarkdown(raw: string): string {
  return (raw || '')
    .replace(/```(?:implementation_plan|plan-json)[\s\S]*?```/gi, '')
    .trim()
}

export function PlanTracker({
  emptyHint = false,
  hintOnly = false,
  docked = false,
  collapsed = false,
  onToggleCollapsed,
}: {
  emptyHint?: boolean
  hintOnly?: boolean
  docked?: boolean
  collapsed?: boolean
  onToggleCollapsed?: () => void
} = {}) {
  const planTasks = useAppStore(s => s.planTasks)
  const planRunId = useAppStore(s => s.planRunId)
  const planPhase = useAppStore(s => s.planPhase)
  const planMarkdown = useAppStore(s => s.planMarkdown)
  const planTitle = useAppStore(s => s.planTitle)
  const planVersion = useAppStore(s => s.planVersion)
  const applyPlanEvent = useAppStore(s => s.applyPlanEvent)
  const activeSessions = useAppStore(s => s.activeSessions)
  const addMessage = useAppStore(s => s.addMessage)
  const setSessionFiles = useAppStore(s => s.setSessionFiles)
  const [busy, setBusy] = useState(false)
  const [docOpen, setDocOpen] = useState(true)
  const [downloading, setDownloading] = useState(false)
  const [stepStartedAt, setStepStartedAt] = useState<number | null>(null)
  const [elapsedSec, setElapsedSec] = useState(0)
  const runningId = planTasks.find(t => t.status === 'running')?.id || ''
  useEffect(() => {
    if (!runningId) {
      setStepStartedAt(null)
      return
    }
    setStepStartedAt(Date.now())
    setElapsedSec(0)
  }, [runningId])
  useEffect(() => {
    if (!stepStartedAt) return
    const timer = window.setInterval(() => {
      setElapsedSec(Math.max(0, Math.round((Date.now() - stepStartedAt) / 1000)))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [stepStartedAt])
  const implementRef = useRef<() => void>(() => {})
  useEffect(() => {
    if (hintOnly) return
    const run = () => {
      if (!buildRequested) return
      buildRequested = false
      implementRef.current()
    }
    run()
    window.addEventListener(PLAN_BUILD_EVENT, run)
    return () => window.removeEventListener(PLAN_BUILD_EVENT, run)
  }, [hintOnly])

  const hasDoc = !!(planMarkdown || '').trim()
  const hasTasks = planPhase !== 'idle' && planTasks.length > 0

  if (hintOnly) {
    if (hasTasks || hasDoc || !emptyHint) return null
  }

  if (!hasTasks && !hasDoc) {
    if (!emptyHint) return null
    return (
      <div className="mx-3 mb-2 rounded-xl border border-purple/40 bg-purple/10 px-3 py-2.5">
        <div className="text-[11px] font-bold uppercase tracking-wide text-purple">Plan</div>
        <p className="mt-1 text-[12px] text-muted leading-snug">
          Your next reply is saved as this session&apos;s plan document — not only a chat message.
          It can include best practices and code examples, then an implementation checklist.
        </p>
      </div>
    )
  }

  const total = planTasks.length
  const done = planTasks.filter(t => t.status === 'done').length
  const delivered = planTasks.filter(t => t.delivered).length
  const meterDone = planPhase === 'implementing' ? delivered : done
  const needsWork = (t: { status: string; result_summary?: string }) =>
    t.status === 'blocked' || t.status === 'error'
    || (t.status === 'pending' && (t.result_summary || '').trim().length > 0)
  const failed = planPhase === 'implementing'
    ? planTasks.filter(t => t.status === 'blocked' || t.status === 'error').length
    : planTasks.filter(needsWork).length
  const runningTask = planTasks.find(t => t.status === 'running')
  const runningIndex = runningTask ? planTasks.indexOf(runningTask) + 1 : 0
  const canImplement = (planPhase === 'ready' || planPhase === 'blocked')
    && planTasks.some(t => t.status === 'pending' || t.status === 'blocked')
    && !busy
  const retryRemaining = planTasks.some(t => t.status === 'done')
    && planTasks.some(t => t.status === 'pending' || t.status === 'blocked')

  const body = displayMarkdown(planMarkdown)

  const implement = () => {
    if (!activeSessions || !planRunId || busy || buildInFlight) return
    buildInFlight = true
    setBusy(true)
    const turnStarted = Date.now()
    api.stream.implementPlan(
      activeSessions,
      planRunId,
      (progress) => { /* progress already shown on Plan rows via events */ void progress },
      (result) => {
        addMessage({
          id: Date.now().toString() + '_plan_impl',
          session_id: activeSessions,
          role: 'assistant',
          content: '',
          created_at: new Date().toISOString(),
          _turn_ms: Date.now() - turnStarted,
          message_type: 'natural_result',
          surgical_data: JSON.stringify(result),
        } as any)
      },
      () => {
        buildInFlight = false
        setBusy(false)
        api.sessionFiles.list(activeSessions).then(setSessionFiles).catch(() => {})
      },
      (err) => {
        buildInFlight = false
        setBusy(false)
        toast.error('Implement failed', err)
      },
      applyPlanEvent,
    )
  }
  implementRef.current = () => { if (canImplement) implement() }

  const download = async () => {
    if (!activeSessions || downloading) return
    setDownloading(true)
    try {
      const safe = (planTitle || 'plan').replace(/[^\w\- ]+/g, '_').slice(0, 80)
      await api.chat.exportPlan(activeSessions, `${safe || 'plan'}.md`)
    } catch (e: any) {
      toast.error('Download failed', String(e?.message || e))
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className={docked
      ? `flex flex-col min-h-0 overflow-hidden bg-surface/70 ${collapsed ? '' : 'flex-1'}`
      : 'mx-3 mb-2 rounded-xl border border-purple/40 bg-surface/70 backdrop-blur-sm overflow-hidden'}>
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-border/60 bg-purple/10 shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[11px] font-bold uppercase tracking-wide text-purple">Plan</span>
          {planVersion != null && (
            <span className="text-[10px] font-mono text-purple/80 tabular-nums">v{planVersion}</span>
          )}
          {hasTasks && (
            <span className="text-[11px] text-muted tabular-nums truncate">
              {phaseLabel(planPhase, done, total)}
            </span>
          )}
          {!hasTasks && planTitle && (
            <span className="text-[11px] text-muted truncate">{planTitle}</span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {hasDoc && (
            <button
              type="button"
              onClick={download}
              disabled={downloading}
              title="Download plan as Markdown"
              className="text-[11px] font-semibold px-2 py-1 rounded-lg text-purple/90 border border-purple/25 hover:bg-purple/15 disabled:opacity-50 flex items-center gap-1"
            >
              <FileDownload sx={{ fontSize: 13 }} />
              {downloading ? '…' : '.md'}
            </button>
          )}
          {canImplement && (
            <button
              type="button"
              onClick={implement}
              disabled={busy}
              title="Build the saved plan in Edit mode"
              className="text-[12px] font-semibold px-3 py-1.5 rounded-lg bg-accent/15 text-accent border border-accent/30 hover:bg-accent/25 disabled:opacity-50"
            >
              {busy ? 'Building…' : (retryRemaining ? 'Retry remaining' : 'Build from plan')}
            </button>
          )}
          {onToggleCollapsed && (
            <button
              type="button"
              onClick={onToggleCollapsed}
              className="text-[11px] font-semibold px-2 py-1 rounded-lg text-muted hover:text-ink hover:bg-overlay/60"
            >
              {collapsed ? 'Show' : 'Hide'}
            </button>
          )}
        </div>
      </div>

      {hasTasks && !collapsed && (
        <ProgressMeter
          done={meterDone}
          failed={failed}
          total={total}
          running={!!runningTask}
          hold
          holdKey={planRunId || ''}
        />
      )}

      {collapsed ? null : (
      <div className={docked ? 'flex-1 min-h-0 overflow-y-auto overscroll-contain' : ''}>
      {hasDoc && body && (
        <div className="border-b border-border/60">
          <button
            type="button"
            onClick={() => setDocOpen(o => !o)}
            className="w-full flex items-center justify-between px-3 py-1.5 text-left hover:bg-overlay/40"
          >
            <span className="text-[11px] font-semibold text-ink truncate">
              {planTitle || 'Plan document'}
            </span>
            <span className="text-[10px] text-muted">{docOpen ? 'Collapse' : 'Expand'}</span>
          </button>
          {docOpen && (
            <div className="px-3 pb-3">
              <AssistantMarkdown text={body} density="compact" />
            </div>
          )}
        </div>
      )}

      {hasTasks && (
        <>
          {runningTask && (
            <div className="px-3 py-1.5 text-[12px] text-accent border-b border-border/40">
              Step {runningIndex} of {total} · {runningTask.filename || runningTask.title} · {runningTask.symbol} · {elapsedSec}s
            </div>
          )}
          <ul className="divide-y divide-border/40">
            {planTasks.map((t: PlanTask, i) => (
              <li key={t.id} className="flex items-start gap-2.5 px-3 py-2">
                <span className="mt-1 shrink-0"><StatusDot status={t.status} /></span>
                <div className="min-w-0 flex-1">
                  <div className="text-[13px] font-medium text-ink truncate">
                    <span className="text-muted mr-1 tabular-nums">{i + 1}.</span>
                    {t.filename || t.title} · {t.symbol}
                  </div>
                  {t.detail && (
                    <div className="text-[11px] text-muted/80 mt-0.5 leading-snug">{t.detail}</div>
                  )}
                  {t.status === 'running' && (
                    <div className="text-[11px] text-accent mt-0.5">Working on this</div>
                  )}
                  {t.delivered && (
                    <div className="text-[11px] text-success mt-0.5">
                      edited +{t.added || 0} −{t.removed || 0}
                    </div>
                  )}
                  {t.delivered && t.diff && (
                    <details className="mt-1">
                      <summary className="text-[11px] text-muted cursor-pointer">Diff</summary>
                      <pre className="mt-1 text-[10px] leading-snug whitespace-pre-wrap text-muted max-h-40 overflow-auto">{t.diff}</pre>
                    </details>
                  )}
                  {(t.status === 'blocked' || (t.status === 'pending' && t.result_summary)) && (
                    <div className="text-[11px] text-danger/90 mt-0.5">
                      {t.result_summary || 'This planned step was not produced.'}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
          {planPhase === 'ready' && (
            <div className="px-3 py-2 border-t border-border/60 text-[11px] text-muted">
              Stay in Plan and tell me what to change — I will replace this checklist and the plan document.
            </div>
          )}
          {planPhase === 'complete' && (
            <div className="px-3 py-2 border-t border-border/60 bg-success/5 text-[11px] font-semibold text-success">
              All planned steps covered
            </div>
          )}
          {planPhase === 'blocked' && (
            <div className="px-3 py-2 border-t border-border/60 text-[11px] text-danger">
              {failed} step{failed === 1 ? '' : 's'} need work. Retry remaining, or stay in Plan to revise.
            </div>
          )}
          {planPhase === 'ready' && planTasks.some(t => /build crashed|implement failed/i.test(t.result_summary || '')) && (
            <div className="px-3 py-2 border-t border-border/60 text-[11px] text-danger">
              Build failed — retry
            </div>
          )}
        </>
      )}
      </div>
      )}
    </div>
  )
}
