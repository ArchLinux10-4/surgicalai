import { useEffect, useMemo, useRef, useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import type { AgentTask, AgentTaskStatus } from '../types'

/**
 * AgentMissionControl — dual-agent mission control panel.
 *
 * Replaces the flat TaskListPanel with a two-agent view:
 *   🧠 Architect Agent (indigo/accent)  — plans the work
 *   ⚡ Executor Agent  (emerald/success) — runs each task
 *
 * Connected by a vertical timeline with phase-transition dots.
 * Preserves all existing functionality: cancel buttons, QA badges,
 * live progress text, and useTaskPolling reconciliation.
 *
 * Auto-dismisses after completion so the chat flows naturally.
 */

const ACTIVE: AgentTaskStatus[] = ['pending', 'running']

/* ── Per-task expandable reasoning trail ────────────────────────────────
 * Mirrors chat mode's ThinkingBlock: collapsed by default, purple accent,
 * monospace body, live cursor while the task is still running.
 */
function TaskThinking({ text, isLive }: { text: string; isLive: boolean }) {
  const [expanded, setExpanded] = useState(false)
  if (!text) return null
  return (
    <div className="mt-1">
      <button
        onClick={() => setExpanded(e => !e)}
        className="flex items-center gap-1 text-[10px] text-purple/80 hover:text-purple transition-colors"
        aria-expanded={expanded}
        title="Show this agent's reasoning"
      >
        <span className={`transform transition-transform duration-200 ${expanded ? 'rotate-90' : ''}`}>▶</span>
        <span>{isLive ? 'Thinking…' : 'Reasoning'}</span>
        {!isLive && <span className="text-muted/60">(click to view)</span>}
      </button>
      {expanded && (
        <div className="mt-1 ml-3 pl-2 border-l-2 border-purple/30 text-[11px] text-muted/90 whitespace-pre-wrap max-h-60 overflow-y-auto leading-relaxed font-mono">
          {text}
          {isLive && <span className="inline-block w-1.5 h-3 bg-purple/60 rounded-sm ml-0.5 animate-pulse" />}
        </div>
      )}
    </div>
  )
}

/** How long the "complete" state stays visible before auto-dismissing (ms). */
const AUTO_DISMISS_MS = 8_000

/* ── Derive effective phase from store + task state ────────────────────
 * Handles page reload gracefully: the store resets to 'idle', but if
 * useTaskPolling has already reconciled tasks from the DB we can infer
 * the correct phase from their statuses.
 */
function useEffectivePhase() {
  const storePhase = useAppStore(s => s.agentPhase)
  const tasks = useAppStore(s => s.agentTasks)

  return useMemo(() => {
    // Explicit planning signal from the SSE stream — trust it.
    if (storePhase === 'planning') return 'planning' as const
    // No tasks at all — nothing to show.
    if (tasks.length === 0) return storePhase
    // Tasks exist — derive from their statuses.
    const allTerminal = tasks.every(t =>
      ['done', 'blocked', 'cancelled', 'error'].includes(t.status),
    )
    if (allTerminal) return 'complete' as const
    return 'executing' as const
  }, [storePhase, tasks])
}

/* ── Status indicator dot ──────────────────────────────────────────── */
function StatusDot({ status }: { status: AgentTaskStatus }) {
  if (status === 'running') {
    return (
      <span
        className="inline-block w-3.5 h-3.5 rounded-full border-2 border-success/30 border-t-success animate-spin"
        aria-label="running"
      />
    )
  }
  if (status === 'done') {
    return (
      <span className="inline-flex items-center justify-center w-3.5 h-3.5 text-success text-[13px] font-bold leading-none">
        ✓
      </span>
    )
  }
  if (status === 'blocked') {
    return (
      <span className="inline-flex items-center justify-center w-3.5 h-3.5 text-danger text-[12px] leading-none">
        🚫
      </span>
    )
  }
  if (status === 'cancelled') {
    return (
      <span className="inline-flex items-center justify-center w-3.5 h-3.5 text-muted text-[13px] leading-none">
        ⊘
      </span>
    )
  }
  if (status === 'error') {
    return (
      <span className="inline-flex items-center justify-center w-3.5 h-3.5 text-danger text-[13px] font-bold leading-none">
        !
      </span>
    )
  }
  return (
    <span
      className="inline-block w-3 h-3 rounded-full border border-muted/50"
      aria-label="pending"
    />
  )
}

/* ── QA score badge ────────────────────────────────────────────────── */
function ScoreBadge({
  score,
  verdict,
  kind,
}: {
  score?: number | null
  verdict?: string | null
  kind?: 'code' | 'answer'
}) {
  if (kind === 'answer') {
    return (
      <span
        className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-overlay/60 text-muted border-border/50"
        title="Non-code task — no QA gate"
      >
        note
      </span>
    )
  }
  if (verdict === 'no_edits') {
    // Planning/reasoning task: completed with zero code edits, so the code QA
    // gate has nothing to scan. Show a calm green check with the reason —
    // "skipped" read as if something was missed, which it wasn't.
    return (
      <span
        className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-success/10 text-success border-success/25"
        title="This task produced planning/reasoning output with no code edits — the code QA gate doesn't apply, so there was nothing to scan."
      >
        &#10003; no QA needed
      </span>
    )
  }
  if (score == null) {
    if (!verdict) return null
    const ok = /safe|pass|clean|ok/i.test(verdict)
    return (
      <span
        className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${
          ok
            ? 'bg-success/15 text-success border-success/30'
            : 'bg-overlay/60 text-muted border-border/50'
        }`}
        title={verdict}
      >
        {ok ? 'QA ✓' : verdict}
      </span>
    )
  }
  const tone =
    score >= 8
      ? 'bg-success/15 text-success border-success/30'
      : score >= 5
        ? 'bg-warning/15 text-warning border-warning/30'
        : 'bg-danger/15 text-danger border-danger/30'
  return (
    <span
      className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${tone}`}
      title={verdict || ''}
    >
      QA {score}/10
    </span>
  )
}

/* ═══════════════════════════════════════════════════════════════════════
 * 🧠 Architect Agent card
 * ═══════════════════════════════════════════════════════════════════════ */
function ArchitectCard({
  phase,
  preamble,
  taskCount,
}: {
  phase: 'planning' | 'executing' | 'complete'
  preamble: string
  taskCount: number
}) {
  const isPlanning = phase === 'planning'
  const isComplete = phase === 'complete'

  return (
    <div
      className={`rounded-xl border bg-surface/70 backdrop-blur-sm transition-colors ${
        isPlanning ? 'border-accent/40 shadow-glow-accent' : 'border-border'
      }`}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5">
        <div className="flex items-center gap-2 min-w-0">
          <span className={isPlanning ? 'animate-pulse-soft' : ''}>🧠</span>
          <span className="text-[11px] font-bold uppercase tracking-wide text-accent">
            Architect
          </span>
          {isPlanning && (
            <span className="flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
              <span className="text-[11px] text-accent/80 font-medium">
                Analyzing request…
              </span>
            </span>
          )}
        </div>
        {!isPlanning && (
          <span className="flex items-center gap-1 text-[11px] font-semibold text-success whitespace-nowrap">
            ✓ {isComplete ? 'Mission complete' : 'Plan locked'}
          </span>
        )}
      </div>

      {/* Plan details — visible once the plan has arrived */}
      {!isPlanning && (preamble || taskCount > 0) && (
        <div className="px-3 pb-2.5 space-y-1.5">
          {preamble && (
            <p className="text-[12px] text-muted leading-snug">{preamble}</p>
          )}
          {taskCount > 0 && (
            <span className="inline-block text-[10px] font-semibold text-accent bg-accent/10 border border-accent/20 rounded-full px-2 py-0.5">
              {taskCount} task{taskCount !== 1 ? 's' : ''} planned
            </span>
          )}
        </div>
      )}

      {/* Indeterminate pulse bar while planning */}
      {isPlanning && (
        <div className="h-0.5 bg-accent/10 rounded-b-xl overflow-hidden">
          <div className="h-full w-full bg-accent/30 animate-pulse-soft" />
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════
 * Timeline connector — phase-transition dots between agents
 * ═══════════════════════════════════════════════════════════════════════ */
function TimelineConnector({ complete }: { complete: boolean }) {
  return (
    <div className="flex justify-center py-0.5">
      <div className="flex flex-col items-center">
        <div className="w-0.5 h-1.5 bg-accent/30" />
        <div className="w-2 h-2 rounded-full bg-accent border-2 border-surface ring-1 ring-accent/20" />
        <div className="w-0.5 h-1 bg-gradient-to-b from-accent/30 to-success/30" />
        <div
          className={`w-2 h-2 rounded-full border-2 border-surface ring-1 ring-success/20 ${
            complete ? 'bg-success' : 'bg-success animate-pulse'
          }`}
        />
        <div className="w-0.5 h-1.5 bg-success/30" />
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════
 * ⚡ Executor Agent card
 * ═══════════════════════════════════════════════════════════════════════ */
function ExecutorCard({
  tasks,
  done,
  total,
  pct,
  anyActive,
  complete,
  busy,
  cancelOne,
  cancelAll,
}: {
  tasks: AgentTask[]
  done: number
  total: number
  pct: number
  anyActive: boolean
  complete: boolean
  busy: boolean
  cancelOne: (t: AgentTask) => void
  cancelAll: () => void
}) {
  return (
    <div
      className={`rounded-xl border bg-surface/70 backdrop-blur-sm overflow-hidden transition-colors ${
        anyActive ? 'border-success/30' : 'border-border'
      }`}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-border/60">
        <div className="flex items-center gap-2 min-w-0">
          <span>⚡</span>
          <span className="text-[11px] font-bold uppercase tracking-wide text-success">
            Executor
          </span>
          <span className="text-[11px] text-muted tabular-nums">
            {done}/{total} done — {pct}%
          </span>
        </div>
        {anyActive && (
          <button
            onClick={cancelAll}
            disabled={busy}
            className="text-[11px] font-medium text-danger hover:bg-danger/10 px-2 py-0.5 rounded transition-colors disabled:opacity-50"
          >
            Cancel all
          </button>
        )}
      </div>

      {/* Progress bar */}
      <div className="h-1 bg-overlay/60">
        <div
          className="h-full bg-success transition-all duration-500 ease-out"
          style={{ width: `${pct}%` }}
        />
      </div>

      {/* Task rows */}
      <ul className="divide-y divide-border/40">
        {tasks.map((t, i) => (
          <li key={t.id} className="flex items-start gap-2.5 px-3 py-2">
            <span className="mt-0.5 shrink-0">
              <StatusDot status={t.status} />
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span
                  className={`text-[13px] font-medium truncate ${
                    t.status === 'cancelled'
                      ? 'text-muted line-through'
                      : 'text-ink'
                  }`}
                >
                  <span className="text-muted mr-1 tabular-nums">
                    {i + 1}.
                  </span>
                  {t.title}
                </span>
                <ScoreBadge
                  score={t.qa_score}
                  verdict={t.verdict}
                  kind={t.kind}
                />
              </div>
              {t.status === 'running' && t.progress && (
                <div className="text-[11px] text-muted/80 truncate mt-0.5">
                  {t.progress}
                </div>
              )}
              {t.thinking && (
                <TaskThinking text={t.thinking} isLive={t.status === 'running'} />
              )}
              {t.status === 'blocked' && (
                <div className="text-[11px] text-danger/90 mt-0.5">
                  {t.verdict === 'error'
                    ? 'Paused — this task hit an error. Review before continuing.'
                    : 'Paused — QA flagged blocking issues. Review before continuing.'}
                </div>
              )}
            </div>
            {ACTIVE.includes(t.status) && (
              <button
                onClick={() => cancelOne(t)}
                className="shrink-0 text-muted hover:text-danger hover:bg-danger/10 rounded w-5 h-5 flex items-center justify-center text-[14px] leading-none transition-colors"
                title="Cancel this task"
                aria-label="Cancel task"
              >
                ×
              </button>
            )}
          </li>
        ))}
      </ul>

      {/* Completion banner */}
      {complete && (
        <div className="px-3 py-2 border-t border-border/60 bg-success/5">
          <span className="text-[11px] font-semibold text-success">
            ✓ All tasks complete
          </span>
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════
 * Main export — replaces TaskListPanel
 * ═══════════════════════════════════════════════════════════════════════ */
export function AgentMissionControl() {
  const phase = useEffectivePhase()
  const agentTasks = useAppStore(s => s.agentTasks)
  const taskRunId = useAppStore(s => s.taskRunId)
  const taskPreamble = useAppStore(s => s.taskPreamble)
  const activeSessions = useAppStore(s => s.activeSessions)
  const updateAgentTask = useAppStore(s => s.updateAgentTask)
  const setAgentTasks = useAppStore(s => s.setAgentTasks)
  const clearAgentTasks = useAppStore(s => s.clearAgentTasks)
  const [busy, setBusy] = useState(false)
  const [dismissing, setDismissing] = useState(false)
  const dismissTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // ── Auto-dismiss: fade out after completion ────────────────────────
  // When all tasks finish, wait AUTO_DISMISS_MS then clear the panel.
  // If a new run starts (phase leaves 'complete'), cancel the timer.
  useEffect(() => {
    if (phase === 'complete') {
      dismissTimerRef.current = setTimeout(() => {
        setDismissing(true)
        // Let the CSS fade-out play (~400ms), then fully clear.
        setTimeout(() => { clearAgentTasks(); setDismissing(false) }, 400)
      }, AUTO_DISMISS_MS)
    } else {
      // Phase changed away from complete — cancel any pending dismiss.
      setDismissing(false)
      if (dismissTimerRef.current) {
        clearTimeout(dismissTimerRef.current)
        dismissTimerRef.current = null
      }
    }
    return () => {
      if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current)
    }
  }, [phase, clearAgentTasks])

  const { done, total, anyActive, pct } = useMemo(() => {
    const _total = agentTasks.length
    const _done = agentTasks.filter(t => t.status === 'done').length
    const _anyActive = agentTasks.some(t => ACTIVE.includes(t.status))
    const _pct = _total ? Math.round((_done / _total) * 100) : 0
    return { done: _done, total: _total, anyActive: _anyActive, pct: _pct }
  }, [agentTasks])

  // Nothing to show
  if (phase === 'idle') return null

  const cancelOne = async (t: AgentTask) => {
    updateAgentTask(t.id, { status: 'cancelled' })
    try {
      await api.tasks.cancel(t.id)
    } catch {}
  }

  const cancelAll = async () => {
    if (!activeSessions) return
    setBusy(true)
    setAgentTasks(
      agentTasks.map(t =>
        ACTIVE.includes(t.status)
          ? { ...t, status: 'cancelled' as AgentTaskStatus }
          : t,
      ),
    )
    try {
      await api.tasks.cancelAll(activeSessions, taskRunId || undefined)
    } catch {}
    setBusy(false)
  }

  return (
    <div className={`mx-3 mb-2 animate-slide-up transition-opacity duration-400 ${dismissing ? 'opacity-0' : 'opacity-100'}`}>
      {/* 🧠 Architect Agent */}
      <ArchitectCard
        phase={phase}
        preamble={taskPreamble}
        taskCount={total}
      />

      {/* Timeline connector + ⚡ Executor Agent — hidden during planning */}
      {phase !== 'planning' && (
        <>
          <TimelineConnector complete={phase === 'complete'} />
          <ExecutorCard
            tasks={agentTasks}
            done={done}
            total={total}
            pct={pct}
            anyActive={anyActive}
            complete={phase === 'complete'}
            busy={busy}
            cancelOne={cancelOne}
            cancelAll={cancelAll}
          />
        </>
      )}
    </div>
  )
}
