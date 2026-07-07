import { useEffect, useRef } from 'react'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import type { AgentTask, AgentTaskStatus } from '../types'

const TERMINAL: AgentTaskStatus[] = ['done', 'blocked', 'cancelled', 'error']
const POLL_MS = 2500

/**
 * Keeps the agentic task list in sync with Claude's actual progress.
 *
 * The live SSE stream is the *instant* channel (task_start / task_progress /
 * task_done). This hook is the *reconciliation* channel: while a run is active
 * it polls the DB-backed task state every few seconds and merges it into the
 * store. That way the UI stays correct even if the stream drops (mobile sleep,
 * proxy timeout) — the backend writes every transition to the DB as Claude
 * completes each task, so polling guarantees eventual consistency.
 *
 * Polling stops automatically once every task reaches a terminal state or the
 * run id clears, so there is no idle network chatter outside an active run.
 */
export function useTaskPolling(sessionId: string | null | undefined) {
  const taskRunId = useAppStore((s) => s.taskRunId)
  const agentTasks = useAppStore((s) => s.agentTasks)
  const setAgentTasks = useAppStore((s) => s.setAgentTasks)

  // Avoid clobbering the live client-only `progress` / streamed `thinking`
  // fields on each poll.
  const progressRef = useRef<Record<string, string | undefined>>({})
  const thinkingRef = useRef<Record<string, string | undefined>>({})
  useEffect(() => {
    const map: Record<string, string | undefined> = {}
    const tmap: Record<string, string | undefined> = {}
    for (const t of agentTasks) { map[t.id] = t.progress; tmap[t.id] = t.thinking }
    progressRef.current = map
    thinkingRef.current = tmap
  }, [agentTasks])

  // Whether anything is still in flight (drives whether we keep polling).
  const hasActive = agentTasks.length > 0 && agentTasks.some((t) => !TERMINAL.includes(t.status))

  useEffect(() => {
    if (!sessionId || !taskRunId) return
    // Nothing left to watch — don't start a poll loop.
    if (agentTasks.length > 0 && !hasActive) return

    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const reconcile = (rows: any[]) => {
      if (!Array.isArray(rows) || rows.length === 0) return
      const merged: AgentTask[] = rows
        .slice()
        .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
        .map((r) => ({
          id: r.id,
          seq: r.seq,
          title: r.title,
          detail: r.detail,
          status: r.status as AgentTaskStatus,
          qa_score: r.qa_score ?? null,
          verdict: r.verdict ?? null,
          run_id: r.run_id,
          // Preserve the live progress line the stream gave us; fall back to
          // the persisted result summary so a reconnect still shows context.
          progress: progressRef.current[r.id] ?? r.result_summary ?? undefined,
          // Prefer whichever thinking trail is longer: the live streamed copy
          // (client path) or the persisted DB copy (server-runner path).
          thinking: (() => {
            const live = thinkingRef.current[r.id] || ''
            const stored = (r.thinking as string) || ''
            return (live.length >= stored.length ? live : stored) || undefined
          })(),
        }))
      setAgentTasks(merged)
    }

    // Read the store at decision time so we stop promptly once everything is
    // terminal, rather than relying on the snapshot from the effect closure.
    const allTerminal = () => {
      const cur = useAppStore.getState().agentTasks
      return cur.length > 0 && cur.every((t) => TERMINAL.includes(t.status))
    }

    const tick = async () => {
      try {
        const rows = await api.tasks.list(sessionId, taskRunId)
        if (!cancelled) reconcile(rows as any[])
      } catch {
        // Transient failure — keep polling; the next tick may succeed.
      }
      if (!cancelled && !allTerminal()) timer = setTimeout(tick, POLL_MS)
    }

    timer = setTimeout(tick, POLL_MS)
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [sessionId, taskRunId, hasActive, agentTasks.length, setAgentTasks])
}
