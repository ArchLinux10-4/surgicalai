import { useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import type { PlanTask, PlanPhase, AgentTaskStatus } from '../types'

/**
 * Chat Plan checklist. Separate from AgentMissionControl — no auto-run,
 * no 8s dismiss, own store slice (planTasks / planRunId).
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
  if (phase === 'implementing') return `Implementing — ${done}/${total}`
  if (phase === 'complete') return `Complete — ${total} of ${total}`
  if (phase === 'blocked') return `Blocked — ${done}/${total} covered`
  if (phase === 'ready') return `Ready — ${total} step${total !== 1 ? 's' : ''}`
  return ''
}

export function PlanTracker() {
  const planTasks = useAppStore(s => s.planTasks)
  const planRunId = useAppStore(s => s.planRunId)
  const planPhase = useAppStore(s => s.planPhase)
  const applyPlanEvent = useAppStore(s => s.applyPlanEvent)
  const activeSessions = useAppStore(s => s.activeSessions)
  const addMessage = useAppStore(s => s.addMessage)
  const setSessionFiles = useAppStore(s => s.setSessionFiles)
  const [busy, setBusy] = useState(false)

  if (planPhase === 'idle' || planTasks.length === 0) return null

  const total = planTasks.length
  const done = planTasks.filter(t => t.status === 'done').length
  const pct = total ? Math.round((done / total) * 100) : 0
  const canImplement = (planPhase === 'ready' || planPhase === 'blocked')
    && planTasks.some(t => t.status === 'pending' || t.status === 'blocked')
    && !busy

  const implement = () => {
    if (!activeSessions || !planRunId || busy) return
    setBusy(true)
    applyPlanEvent({ type: 'plan_updated', run_id: planRunId, phase: 'implementing', tasks: planTasks.map(t => ({ ...t, status: t.status === 'pending' ? 'running' : t.status })) })
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
          message_type: 'natural_result',
          surgical_data: JSON.stringify(result),
        } as any)
      },
      () => {
        setBusy(false)
        api.sessionFiles.list(activeSessions).then(setSessionFiles).catch(() => {})
      },
      (err) => {
        setBusy(false)
        toast.error('Implement failed', err)
      },
      applyPlanEvent,
    )
  }

  return (
    <div className="mx-3 mb-2 rounded-xl border border-border bg-surface/70 backdrop-blur-sm overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-border/60">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[11px] font-bold uppercase tracking-wide text-accent">Plan</span>
          <span className="text-[11px] text-muted tabular-nums">
            {phaseLabel(planPhase, done, total)}
          </span>
        </div>
        {canImplement && (
          <button
            onClick={implement}
            disabled={busy}
            className="text-[11px] font-semibold px-2.5 py-1 rounded-lg bg-accent/15 text-accent border border-accent/30 hover:bg-accent/25 disabled:opacity-50"
          >
            {busy ? 'Implementing…' : 'Implement this plan'}
          </button>
        )}
      </div>
      <div className="h-1 bg-overlay/60">
        <div className="h-full bg-accent transition-all duration-500" style={{ width: `${pct}%` }} />
      </div>
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
              {t.status === 'blocked' && (
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
          Stay in Plan and tell me what to change — I will replace this checklist.
        </div>
      )}
      {planPhase === 'complete' && (
        <div className="px-3 py-2 border-t border-border/60 bg-success/5 text-[11px] font-semibold text-success">
          All planned steps covered
        </div>
      )}
      {planPhase === 'blocked' && (
        <div className="px-3 py-2 border-t border-border/60 text-[11px] text-danger">
          Some planned steps were not produced. Implement again for remaining, or stay in Plan to revise.
        </div>
      )}
      {planPhase === 'ready' && planTasks.some(t => t.status === 'pending' && t.result_summary) && (
        <div className="px-3 py-2 border-t border-border/60 text-[11px] text-danger">
          Implement failed — try again
          {planTasks.find(t => t.result_summary)?.result_summary
            ? `: ${planTasks.find(t => t.result_summary)?.result_summary}`
            : ''}
        </div>
      )}
    </div>
  )
}
