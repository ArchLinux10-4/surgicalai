/**
 * Shared change-classification helpers for InlineDiffCard and Apply All.
 *
 * Cards only render "real" diffs (actual +/- lines). Apply All used to count
 * every change id from surgical_data, which left the sticky bar visible when
 * every visible card already said "All applied".
 */

export function isRealDiffChange(c: { diff?: string } | null | undefined): boolean {
  if (!c?.diff) return false
  const lines = c.diff.split('\n')
  const hasAdds = lines.some(l => l.startsWith('+') && !l.startsWith('+++'))
  const hasRemoves = lines.some(l => l.startsWith('-') && !l.startsWith('---'))
  return hasAdds || hasRemoves
}

export function appliedStorageKey(sessionId: string, changeId: string) {
  return `sai-applied:${sessionId}:${changeId}`
}

export function skippedStorageKey(sessionId: string, changeId: string) {
  return `sai-skipped:${sessionId}:${changeId}`
}

function readLocalFlag(key: string): boolean {
  try {
    if (typeof localStorage === 'undefined') return false
    return localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

export function isLocallyApplied(sessionId: string, changeId: string): boolean {
  return readLocalFlag(appliedStorageKey(sessionId, changeId))
}

export function isLocallySkipped(sessionId: string, changeId: string): boolean {
  return readLocalFlag(skippedStorageKey(sessionId, changeId))
}

/**
 * Split file changes into clean (bulk-applyable) vs QA-flagged pending rows.
 * Same filter must drive both the Apply All label and the click handler.
 */
export function splitPendingChanges(
  changes: any[] | null | undefined,
  appliedIds: Set<string>,
  sessionId: string,
): { clean: any[]; flagged: any[] } {
  const real = (changes || []).filter((c: any) => c?.id && isRealDiffChange(c))
  const pending = real.filter((c: any) =>
    !appliedIds.has(c.id)
    && !isLocallyApplied(sessionId, c.id)
    && !isLocallySkipped(sessionId, c.id),
  )
  const clean = pending.filter((c: any) => c?.qa_result?.verdict !== 'blocked')
  const flagged = pending.filter((c: any) => c?.qa_result?.verdict === 'blocked')
  return { clean, flagged }
}
