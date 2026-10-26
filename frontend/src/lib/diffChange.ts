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

export function isLocallySkipped(sessionId: string, changeId: string): boolean {
  return readLocalFlag(skippedStorageKey(sessionId, changeId))
}

/** Copy old browser-only applied flags onto the server, then delete them. */
export async function migrateLocalApplied(sessionId: string): Promise<void> {
  if (!sessionId || typeof localStorage === 'undefined') return
  const flag = `sai-applied-migrated:${sessionId}`
  const prefix = `sai-applied:${sessionId}:`
  let keys: string[] = []
  try {
    keys = Object.keys(localStorage).filter(key => key.startsWith(prefix))
  } catch {
    return
  }
  if (localStorage.getItem(flag) === '1' && keys.length === 0) return
  const { api } = await import('../api/client')
  await Promise.all(keys.map(async (key) => {
    const changeId = key.slice(prefix.length)
    if (changeId) {
      try { await api.surgical.markApplied(sessionId, changeId) } catch { /* keep going */ }
    }
    try { localStorage.removeItem(key) } catch { /* ignore */ }
  }))
  try { localStorage.setItem(flag, '1') } catch { /* ignore */ }
}

export function supersededKeysByMessage(messages: { id?: string; surgical_data?: string; message_type?: string }[]): Record<string, string> {
  const latest = new Map<string, string>()
  const rows: { id: string; files: Record<string, any> }[] = []
  for (const msg of messages) {
    if (!msg?.id || !msg.surgical_data) continue
    if (msg.message_type && msg.message_type !== 'natural_result' && msg.message_type !== 'surgical_result') continue
    try {
      const data = JSON.parse(msg.surgical_data)
      const files = data?.changes_by_file || {}
      rows.push({ id: msg.id, files })
      for (const fileData of Object.values(files) as any[]) {
        if (fileData?.filename) latest.set(fileData.filename, msg.id)
      }
    } catch { /* skip malformed cards */ }
  }
  const keys: Record<string, string> = {}
  for (const row of rows) {
    const ids: string[] = []
    for (const fileData of Object.values(row.files) as any[]) {
      if (!fileData?.filename || latest.get(fileData.filename) === row.id) continue
      for (const change of fileData.changes || []) {
        if (change?.id) ids.push(change.id)
      }
    }
    if (ids.length) keys[row.id] = ids.sort().join(',')
  }
  return keys
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
    && !isLocallySkipped(sessionId, c.id),
  )
  const clean = pending.filter((c: any) => c?.qa_result?.verdict !== 'blocked')
  const flagged = pending.filter((c: any) => c?.qa_result?.verdict === 'blocked')
  return { clean, flagged }
}
