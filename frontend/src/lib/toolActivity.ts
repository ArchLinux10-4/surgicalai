/**
 * Pure tool-activity reducer. No React. Imported by the store and by
 * node:test (explicit .ts suffix, type-only imports).
 */
import type { ToolActivityEvent, ToolKind, ToolRow, ToolStatus } from '../types/index.ts'

const KINDS = new Set<ToolKind>([
  'file', 'search', 'repo', 'github', 'history', 'preview', 'web',
  'git', 'aws', 'mcp', 'edit', 'qa', 'step',
])
const PHASE_KINDS = new Set<ToolKind>(['step', 'edit', 'qa'])
const MAX_ROWS = 40
const OMITTED_ID = '__omitted__'

function clip(value: unknown, max: number): string {
  const text = typeof value === 'string' ? value : value == null ? '' : String(value)
  return text.length > max ? text.slice(0, max) : text
}

function asKind(value: unknown): ToolKind {
  return typeof value === 'string' && KINDS.has(value as ToolKind) ? value as ToolKind : 'step'
}

function asStatus(value: unknown, fallback: ToolStatus): ToolStatus {
  return value === 'running' || value === 'waiting' || value === 'done' || value === 'failed' || value === 'stopped'
    ? value
    : fallback
}

export function capToolRows(rows: ToolRow[]): ToolRow[] {
  const real = rows.filter(r => r.id !== OMITTED_ID)
  if (real.length <= MAX_ROWS) return rows
  const head = real.slice(0, 5)
  const tail = real.slice(real.length - 34)
  const dropped = real.length - head.length - tail.length
  const more: ToolRow = {
    id: OMITTED_ID,
    kind: 'step',
    label: `${dropped} more steps`,
    doneLabel: `${dropped} more steps`,
    status: 'done',
    startedAt: 0,
  }
  return [...head, more, ...tail]
}

export function applyToolEvent(rows: ToolRow[], ev: ToolActivityEvent, now: number): ToolRow[] {
  if (!ev || typeof ev !== 'object') return rows
  const id = clip(ev.id, 80)
  if (!id) return rows
  const phase = ev.phase
  if (phase !== 'start' && phase !== 'update' && phase !== 'end') return rows

  if (phase === 'start') {
    if (rows.some(r => r.id === id)) return rows
    const label = clip(ev.label, 160) || 'Working'
    const row: ToolRow = {
      id,
      kind: asKind(ev.kind),
      label,
      doneLabel: clip(ev.done_label, 160) || label,
      target: clip(ev.target, 160) || undefined,
      status: 'running',
      startedAt: now,
      taskId: ev.task_id ? clip(ev.task_id, 80) : undefined,
      agent: ev.agent ? clip(ev.agent, 80) : undefined,
    }
    return capToolRows([...rows, row])
  }

  const index = rows.findIndex(r => r.id === id)
  if (index < 0) return rows
  const current = rows[index]

  if (phase === 'update') {
    const status = ev.status === 'waiting' || ev.status === 'running' ? ev.status : current.status
    const target = ev.target != null && String(ev.target) !== '' ? clip(ev.target, 160) : current.target
    const taskId = current.taskId || (ev.task_id ? clip(ev.task_id, 80) : undefined)
    if (status === current.status && target === current.target && taskId === current.taskId) return rows
    const next = rows.slice()
    next[index] = { ...current, status, target, taskId }
    return next
  }

  let status: ToolStatus
  if (ev.status === 'stopped' || ev.summary === 'Stopped') status = 'stopped'
  else if (ev.ok === false) status = 'failed'
  else status = 'done'
  const ms = typeof ev.ms === 'number' && Number.isFinite(ev.ms) ? Math.max(0, ev.ms) : current.ms
  const next = rows.slice()
  next[index] = {
    ...current,
    status,
    endedAt: now,
    ms,
    summary: ev.summary != null ? clip(ev.summary, 160) : current.summary,
    preview: ev.preview != null ? clip(ev.preview, 400) : current.preview,
    taskId: current.taskId || (ev.task_id ? clip(ev.task_id, 80) : undefined),
  }
  return next
}

export function stopRunning(rows: ToolRow[], now: number): ToolRow[] {
  let changed = false
  const next = rows.map(row => {
    if (row.status !== 'running' && row.status !== 'waiting') return row
    changed = true
    const elapsed = row.startedAt > 0 ? Math.max(0, now - row.startedAt) : row.ms
    return { ...row, status: 'stopped' as const, endedAt: now, ms: elapsed }
  })
  return changed ? next : rows
}

export function finalizeTrail(rows: ToolRow[], now = Date.now()): ToolRow[] {
  return stopRunning(rows, now).map(row => ({
    id: row.id,
    kind: row.kind,
    label: row.label,
    doneLabel: row.doneLabel,
    target: row.target,
    status: row.status,
    startedAt: 0,
    ms: typeof row.ms === 'number' && Number.isFinite(row.ms) ? Math.max(0, row.ms) : undefined,
    summary: row.summary,
    preview: row.preview,
    taskId: row.taskId,
  }))
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return ''
  const safe = Math.max(0, ms)
  if (safe < 10000) {
    const seconds = safe / 1000
    return `${seconds.toFixed(1)}s`
  }
  if (safe < 60000) return `${Math.round(safe / 1000)}s`
  const minutes = Math.floor(safe / 60000)
  const seconds = Math.round((safe % 60000) / 1000)
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`
}

function rowMs(row: ToolRow): number | null {
  if (typeof row.ms === 'number' && Number.isFinite(row.ms)) return Math.max(0, row.ms)
  if (typeof row.endedAt === 'number' && row.startedAt > 0) {
    const delta = row.endedAt - row.startedAt
    if (Number.isFinite(delta)) return Math.max(0, delta)
  }
  return null
}

export function summarizeTrail(rows: ToolRow[]): string {
  const counted = rows.filter(row => row.id !== OMITTED_ID && !PHASE_KINDS.has(row.kind))
  let total = 0
  let known = false
  for (const row of rows) {
    if (row.id === OMITTED_ID) continue
    const ms = rowMs(row)
    if (ms == null) continue
    total += ms
    known = true
  }
  const duration = known ? formatDuration(total) : ''
  if (counted.length === 0) return duration ? `Finished in ${duration}` : 'Finished'
  const noun = counted.length === 1 ? 'tool' : 'tools'
  return duration ? `Used ${counted.length} ${noun} · ${duration}` : `Used ${counted.length} ${noun}`
}

const EMOJI = /^(?:\p{Extended_Pictographic}|\p{Emoji_Presentation}|[\u2600-\u27BF])+\s*/u

export function stripLeadingEmoji(text: string): string {
  return text.replace(EMOJI, '').trim()
}

export function deriveStage(input: {
  rows: ToolRow[]
  content?: string
  progress?: string
  isThinking?: boolean
  isBuildingEdit?: boolean
  writingLive?: boolean
}): { label: string; tone: 'work' | 'wait' | 'write' | 'think' } {
  if (input.rows.some(row => row.status === 'waiting')) {
    return { label: 'Waiting for your approval', tone: 'wait' }
  }
  const running = [...input.rows].reverse().find(row => row.status === 'running')
  if (running) {
    const label = running.target ? `${running.label} · ${running.target}` : running.label
    return { label, tone: 'work' }
  }
  if (input.isBuildingEdit || input.writingLive) return { label: 'Writing the change', tone: 'write' }
  if ((input.content || '').trim()) return { label: 'Writing the reply', tone: 'write' }
  if (input.isThinking) return { label: 'Reasoning', tone: 'think' }
  const progress = stripLeadingEmoji((input.progress || '').trim())
  if (progress && progress !== 'Thinking...' && progress !== 'Thinking') {
    return { label: progress, tone: 'think' }
  }
  return { label: 'Thinking', tone: 'think' }
}

export function modelFamily(id?: string): { name: string; family: 'claude' | 'gpt' | 'grok' | 'gemini' | 'local' | 'unknown' } {
  const model = (id || '').toLowerCase()
  if (model.startsWith('claude')) return { name: 'Claude', family: 'claude' }
  if (model.startsWith('gpt') || /^o\d/.test(model)) return { name: 'GPT', family: 'gpt' }
  if (model.startsWith('grok')) return { name: 'Grok', family: 'grok' }
  if (model.startsWith('gemini')) return { name: 'Gemini', family: 'gemini' }
  if (model.startsWith('ollama')) return { name: 'Local', family: 'local' }
  return { name: 'SurgicalAI', family: 'unknown' }
}

export function rowsFromProgress(progress: string, now: number): ToolRow[] {
  const label = stripLeadingEmoji((progress || '').trim())
  if (!label || label === 'Thinking...' || label === 'Thinking') return []
  return [{
    id: 'legacy-progress',
    kind: 'step',
    label,
    doneLabel: label,
    status: 'running',
    startedAt: now,
  }]
}

export function hydrateToolRows(raw: unknown): ToolRow[] {
  if (!Array.isArray(raw)) return []
  const rows: ToolRow[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    const id = clip(record.id, 80)
    if (!id) continue
    const label = clip(record.label, 160) || 'Working'
    const ms = typeof record.ms === 'number' && Number.isFinite(record.ms) ? Math.max(0, record.ms) : undefined
    rows.push({
      id,
      kind: asKind(record.kind),
      label,
      doneLabel: clip(record.doneLabel ?? record.done_label, 160) || label,
      target: record.target ? clip(record.target, 160) : undefined,
      status: asStatus(record.status, 'done'),
      startedAt: typeof record.startedAt === 'number' && Number.isFinite(record.startedAt) ? record.startedAt : 0,
      ms,
      summary: record.summary ? clip(record.summary, 160) : undefined,
      preview: record.preview ? clip(record.preview, 400) : undefined,
      taskId: record.taskId || record.task_id ? clip(record.taskId || record.task_id, 80) : undefined,
      agent: record.agent ? clip(record.agent, 80) : undefined,
    })
  }
  return rows
}
