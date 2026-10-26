import type { SessionFile } from '../types'

export interface StagedEdit {
  added: number
  removed: number
  created: boolean
  at: number
}

function lineCounts(diff: string): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const line of String(diff || '').split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) added++
    else if (line.startsWith('-') && !line.startsWith('---')) removed++
  }
  return { added, removed }
}

function addCounts(into: { added: number; removed: number }, change: any) {
  if (typeof change?.diff === 'string' && change.diff) {
    const n = lineCounts(change.diff)
    into.added += n.added
    into.removed += n.removed
    return
  }
  const added = Number(change?.added || 0)
  const removed = Number(change?.removed || 0)
  if (added || removed) {
    into.added += added
    into.removed += removed
  }
}

/** Edits in the latest assistant result. Apply has not written these yet. */
export function latestStagedEdits(messages: { surgical_data?: string; created_at?: string }[]): Map<string, StagedEdit> {
  const out = new Map<string, StagedEdit>()
  for (let i = messages.length - 1; i >= 0; i--) {
    const raw = messages[i]?.surgical_data
    if (!raw) continue
    let parsed: any
    try { parsed = JSON.parse(raw) } catch { continue }
    const at = Date.parse(messages[i]?.created_at || '')
    const changes = parsed?.changes_by_file || {}
    const created = Array.isArray(parsed?.new_files) ? parsed.new_files : []
    if (!Object.keys(changes).length && !created.length) continue
    for (const [key, data] of Object.entries(changes) as [string, any][]) {
      const name = String(data?.filename || key || '')
      if (!name) continue
      const counts = { added: 0, removed: 0 }
      const list = Array.isArray(data?.changes) ? data.changes : []
      for (const change of list) addCounts(counts, change)
      const prev = out.get(name) || { added: 0, removed: 0, created: false, at: 0 }
      out.set(name, {
        added: prev.added + counts.added,
        removed: prev.removed + counts.removed,
        created: prev.created,
        at: Number.isFinite(at) ? at : 0,
      })
    }
    for (const file of created) {
      const name = String(file?.filename || '')
      if (!name) continue
      const lines = String(file?.content || '').split('\n').filter(line => line.length > 0).length
      const prev = out.get(name)
      out.set(name, {
        added: prev?.added || lines,
        removed: prev?.removed || 0,
        created: true,
        at: Number.isFinite(at) ? at : 0,
      })
    }
    break
  }
  return out
}

function sameFile(a: string, b: string) {
  if (a === b) return true
  const base = (name: string) => name.split('/').pop() || name
  return base(a) === base(b)
}

/**
 * Session files plus any new file that exists only in the latest result.
 * A staged file counts as edited and Not saved. Files the result did not
 * touch keep their on-disk state.
 */
export function applyStagedToFiles(
  files: SessionFile[],
  staged: Map<string, StagedEdit>,
  sessionId: string,
): SessionFile[] {
  const seen = new Set<string>()
  const next = files.map(file => {
    const hit = [...staged.entries()].find(([name]) => sameFile(name, file.filename))
    if (!hit) return file
    seen.add(hit[0])
    const savedAt = Date.parse(file.updated_at || '')
    if (file.edited && Number.isFinite(savedAt) && hit[1].at > 0 && savedAt > hit[1].at) {
      return file
    }
    return {
      ...file,
      edited: 1,
      disk_state: 'pending' as const,
      origin: hit[1].created ? 'created' as const : file.origin,
    }
  })
  for (const [name, edit] of staged) {
    if ([...seen].some(got => sameFile(got, name))) continue
    next.push({
      id: `staged:${name}`,
      session_id: sessionId,
      filename: name,
      language: '',
      lines: edit.added,
      symbol_count: 0,
      created_at: '',
      edited: 1,
      origin: edit.created ? 'created' : 'uploaded',
      disk_state: 'pending',
    })
  }
  return next
}

export function stagedCountsFor(filename: string, staged: Map<string, StagedEdit>) {
  for (const [name, edit] of staged) {
    if (sameFile(name, filename)) return edit
  }
  return null
}
