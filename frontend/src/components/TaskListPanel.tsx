import { useMemo, useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import type { AgentTask, AgentTaskStatus } from '../types'

/**
 * NOTE: This component is not currently imported or rendered anywhere in the
 * app — AgentMissionControl.tsx fully replaced it. Kept intentionally (not
 * deleted) in case it's needed again; do not wire it up without checking
 * whether AgentMissionControl.tsx should be updated instead.
 *
 * TaskListPanel — live GUI for the agentic task loop.
 *
 * Renders the plan Claude generated, tracks each task's status in real time,
 * shows the QA score per task, and lets the user cancel a single task or all
 * remaining tasks. Cancellation is DB-backed on the server; here we also update
 * optimistically so the UI feels instant.
 *
 * Uses only semantic theme tokens (accent / success / warning / danger / muted /
 * surface / ink) so both light and dark themes render correctly with no extra work.
 */

const ACTIVE: AgentTaskStatus[] = ['pending', 'running']

/* ── Per-task expandable reasoning trail (parity with chat mode) ──────── */
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

function StatusDot({ status }: { status: AgentTaskStatus }) {
  if (status === 'running') {
    return (
      <span
        className="inline-block w-3.5 h-3.5 rounded-full border-2 border-accent/30 border-t-accent animate-spin"
        aria-label="running"
      />
    )
  }
  if (status === 'done') {
    return <span className="inline-flex items-center justify-center w-3.5 h-3.5 text-success text-[13px] font-bold leading-none">✓</span>
  }
  if (status === 'blocked') {
    return <span className="inline-flex items-center justify-center w-3.5 h-3.5 text-danger text-[12px] leading-none">🚫</span>
  }
  if (status === 'cancelled') {
    return <span className="inline-flex items-center justify-center w-3.5 h-3.5 text-muted text-[13px] leading-none">⊘</span>
  }
  if (status === 'error') {
    return <span className="inline-flex items-center justify-center w-3.5 h-3.5 text-danger text-[13px] font-bold leading-none">!</span>
  }
  // pending
  return <span className="inline-block w-3 h-3 rounded-full border border-muted/50" aria-label="pending" />
}

function ScoreBadge({ score, verdict, kind }: { score?: number | null; verdict?: string | null; kind?: 'code' | 'answer' }) {
  if (kind === 'answer') {
    return (
      <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-overlay/60 text-muted border-border/50" title="Non-code task — no QA gate">
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
    // Completed code task that produced no source edits to gate (e.g. created a
    // net-new doc file): no numeric score, but we still surface the QA verdict
    // so the row is never an ambiguous blank.
    if (!verdict) return null
    const ok = /safe|pass|clean|ok/i.test(verdict)
    return (
      <span
        className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${ok ? 'bg-success/15 text-success border-success/30' : 'bg-overlay/60 text-muted border-border/50'}`}
        title={verdict}
      >
        {ok ? 'QA ✓' : verdict}
      </span>
    )
  }
  const tone =
    score >= 8 ? 'bg-success/15 text-success border-success/30'
    : score >= 5 ? 'bg-warning/15 text-warning border-warning/30'
    : 'bg-danger/15 text-danger border-danger/30'
  return (
    <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${tone}`} title={verdict || ''}>
      QA {score}/10
    </span>
  )
}

export function TaskListPanel() {
  const agentTasks = useAppStore(s => s.agentTasks)
  const taskRunId = useAppStore(s => s.taskRunId)
  const taskPreamble = useAppStore(s => s.taskPreamble)
  const activeSessions = useAppStore(s => s.activeSessions)
  const updateAgentTask = useAppStore(s => s.updateAgentTask)
  const setAgentTasks = useAppStore(s => s.setAgentTasks)
  const [busy, setBusy] = useState(false)

  const { done, total, anyActive } = useMemo(() => {
    const total = agentTasks.length
    const done = agentTasks.filter(t => t.status === 'done').length
    const anyActive = agentTasks.some(t => ACTIVE.includes(t.status))
    return { done, total, anyActive }
  }, [agentTasks])

  if (!agentTasks.length) return null

  const cancelOne = async (t: AgentTask) => {
    updateAgentTask(t.id, { status: 'cancelled' })
    try { await api.tasks.cancel(t.id) } catch {}
  }

  const cancelAll = async () => {
    if (!activeSessions) return
    setBusy(true)
    setAgentTasks(agentTasks.map(t => ACTIVE.includes(t.status) ? { ...t, status: 'cancelled' as AgentTaskStatus } : t))
    try { await api.tasks.cancelAll(activeSessions, taskRunId || undefined) } catch {}
    setBusy(false)
  }

  return (
    <div className="mx-3 mb-2 rounded-xl border border-border bg-surface/70 backdrop-blur-sm shadow-soft overflow-hidden animate-slide-up">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border/60">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-[11px] font-bold uppercase tracking-wide text-accent">Tasks</span>
          <span className="text-[11px] text-muted tabular-nums">{done}/{total} done</span>
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

      {taskPreamble && (
        <div className="px-3 pt-2 text-[12px] text-muted leading-snug">{taskPreamble}</div>
      )}

      {/* Progress bar */}
      <div className="h-1 bg-overlay/60">
        <div
          className="h-full bg-accent transition-all duration-500 ease-out"
          style={{ width: total ? `${Math.round((done / total) * 100)}%` : '0%' }}
        />
      </div>

      {/* Task rows */}
      <ul className="divide-y divide-border/40">
        {agentTasks.map((t, i) => (
          <li key={t.id} className="flex items-start gap-2.5 px-3 py-2">
            <span className="mt-0.5 shrink-0"><StatusDot status={t.status} /></span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className={`text-[13px] font-medium truncate ${
                  t.status === 'cancelled' ? 'text-muted line-through'
                  : t.status === 'running' ? 'text-ink'
                  : 'text-ink'
                }`}>
                  <span className="text-muted mr-1 tabular-nums">{i + 1}.</span>{t.title}
                </span>
                <ScoreBadge score={t.qa_score} verdict={t.verdict} kind={t.kind} />
              </div>
              {t.status === 'running' && t.progress && (
                <div className="text-[11px] text-muted/80 truncate mt-0.5">{t.progress}</div>
              )}
              {t.thinking && (
                <TaskThinking text={t.thinking} isLive={t.status === 'running'} />
              )}
              {t.status === 'blocked' && (
                <div className="text-[11px] text-danger/90 mt-0.5">{t.verdict === 'error' ? 'Paused — this task hit an error. Review before continuing.' : 'Paused — QA flagged blocking issues. Review before continuing.'}</div>
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
    </div>
  )
}
