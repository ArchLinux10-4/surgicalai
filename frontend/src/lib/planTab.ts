export function planTabKey(sessionId: string) {
  return `surgicalai:planTabHidden:${sessionId}`
}

export function planStatusBadge(phase: string, tasks: { status: string; result_summary?: string }[]) {
  const total = tasks.length
  const done = tasks.filter(t => t.status === 'done').length
  const blocked = tasks.filter(t => t.status === 'blocked').length
  const open = tasks.filter(t =>
    t.status === 'blocked' || (t.status === 'pending' && (t.result_summary || '').trim()),
  ).length
  const crashed = tasks.some(t => /build crashed|implement failed|build failed/i.test(t.result_summary || ''))
  if (phase === 'implementing') return `Building ${done}/${total || 0}`
  if (phase === 'complete') return 'Done'
  if (crashed && blocked === 0) return 'Build failed — retry'
  if (open > 0) return `Needs work ${open}`
  if (phase === 'ready' || phase === 'blocked') return 'Ready'
  return ''
}

/** A run with nothing pending must not be sent to /execute-task. */
export function shouldQueueAfterStart(resp: { ok?: boolean; mode?: string } | null | undefined) {
  if (resp?.ok) return false
  if (resp?.mode === 'no_tasks') return false
  return true
}
