export function planTabKey(sessionId: string) {
  return `surgicalai:planTabHidden:${sessionId}`
}

export function planStatusBadge(phase: string, tasks: { status: string; result_summary?: string }[]) {
  const total = tasks.length
  const done = tasks.filter(t => t.status === 'done').length
  const needs = tasks.filter(t => t.status === 'blocked' || (t.status === 'pending' && t.result_summary)).length
  if (phase === 'implementing') return `Building ${done}/${total || 0}`
  if (phase === 'complete') return 'Done'
  if (needs > 0) return `Needs work ${needs}`
  if (phase === 'ready' || phase === 'blocked') return 'Ready'
  return ''
}

/** A run with nothing pending must not be sent to /execute-task. */
export function shouldQueueAfterStart(resp: { ok?: boolean; mode?: string } | null | undefined) {
  if (resp?.ok) return false
  if (resp?.mode === 'no_tasks') return false
  return true
}
