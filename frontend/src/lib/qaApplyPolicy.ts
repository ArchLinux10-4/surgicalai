/**
 * Apply-gate policy from QA provenance (session 3a6150e9 / Option A).
 *
 * Machine-verified sources (tsc / structural / plan_*) hard-stop Apply.
 * LLM-only blocked → Apply only after explicit inline acknowledgment.
 * Never promotes free-text LLM type_errors into a machine hard-stop — that
 * was the false-positive bottleneck mode; inference only recognizes prefixes
 * stamped by our own pipeline force-block / structural merge paths.
 */
import type { QAResult } from '../types'

const MACHINE = new Set(['tsc', 'structural', 'plan_incomplete', 'plan_noop'])

export type ApplyGateKind = 'allowed' | 'hard_stop' | 'ack_required'

export interface ApplyGate {
  kind: ApplyGateKind
  sources: string[]
  machineVerified: boolean
  /** Short UI labels for the provenance strip. */
  reasons: string[]
}

function isMachineSource(s: string): boolean {
  const t = (s || '').trim()
  return MACHINE.has(t) || t.startsWith('plan_')
}

/** Infer sources for older sessions that lack block_sources on the wire. */
export function inferBlockSources(qa: QAResult | null | undefined): string[] {
  if (!qa || qa.verdict !== 'blocked') return []
  const stamped = (qa.block_sources || []).map(s => String(s || '').trim()).filter(Boolean)
  if (stamped.length) return Array.from(new Set(stamped))

  const sources: string[] = []
  const summary = qa.summary || ''
  if (summary.startsWith('tsc:')) sources.push('tsc')

  for (const te of qa.type_errors || []) {
    const s = String(te || '')
    // Exact force_block shape: "TS1005 (line 42): ..."
    if (/^TS\d+\s+\(line\s+/.test(s) && s.includes('):')) {
      if (!sources.includes('tsc')) sources.push('tsc')
      break
    }
  }

  const struct = (qa.import_issues || []).filter(i => String(i || '').startsWith('[STRUCTURAL]'))
  if (struct.length) {
    sources.push('structural')
    const joined = struct.join('\n')
    if (joined.includes('identical to ORIGINAL') || joined.includes('change plan was not')) {
      sources.push('plan_noop')
    } else if (joined.includes('Plan requires') || joined.includes('half-implemented')) {
      sources.push('plan_incomplete')
    }
  }

  if (!sources.length) sources.push('llm')
  return Array.from(new Set(sources))
}

function reasonLabels(sources: string[]): string[] {
  const labels: string[] = []
  for (const s of sources) {
    if (s === 'tsc') labels.push('TypeScript compile (tsc)')
    else if (s === 'structural') labels.push('Structural QA')
    else if (s === 'plan_incomplete') labels.push('Plan incomplete')
    else if (s === 'plan_noop') labels.push('Plan not applied (identical code)')
    else if (s === 'llm') labels.push('LLM QA judgment only')
    else if (s.startsWith('plan_')) labels.push(`Plan check (${s})`)
    else labels.push(s)
  }
  return labels
}

export function getApplyGate(qa: QAResult | null | undefined): ApplyGate {
  if (!qa || qa.verdict !== 'blocked') {
    return { kind: 'allowed', sources: [], machineVerified: false, reasons: [] }
  }
  const sources = inferBlockSources(qa)
  const machineVerified = sources.some(isMachineSource)

  if (machineVerified) {
    return {
      kind: 'hard_stop',
      sources,
      machineVerified: true,
      reasons: reasonLabels(sources.filter(isMachineSource)),
    }
  }
  return {
    kind: 'ack_required',
    sources: sources.length ? sources : ['llm'],
    machineVerified: false,
    reasons: reasonLabels(sources.length ? sources : ['llm']),
  }
}

/** Evidence lines to show under provenance (machine first). */
export function provenanceEvidence(qa: QAResult | null | undefined, limit = 6): string[] {
  if (!qa) return []
  const lines: string[] = []
  for (const te of qa.type_errors || []) {
    const s = String(te || '').trim()
    if (s) lines.push(s)
    if (lines.length >= limit) return lines
  }
  for (const ii of qa.import_issues || []) {
    const s = String(ii || '').trim()
    if (s) lines.push(s)
    if (lines.length >= limit) return lines
  }
  for (const le of qa.logic_errors || []) {
    const s = String(le || '').trim()
    if (s) lines.push(s)
    if (lines.length >= limit) return lines
  }
  if (qa.summary && lines.length < limit) lines.push(qa.summary)
  return lines
}
