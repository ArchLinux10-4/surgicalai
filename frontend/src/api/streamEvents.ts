/** Event names shared with backend/services/stream_events.py. */

export const STREAM_EVENT_TYPES = [
  'aws_approval_needed',
  'aws_cli',
  'aws_done',
  'chat',
  'checkpoint',
  'compacting',
  'compacting_done',
  'credit_paused',
  'done',
  'edit_end',
  'edit_live',
  'edit_start',
  'error',
  'file_needed',
  'file_needed_cleared',
  'git_approval_needed',
  'git_cli',
  'git_done',
  'git_result',
  'git_step',
  'local_approval_needed',
  'local_cli',
  'local_done',
  'mcp_approval_needed',
  'mcp_call',
  'mcp_done',
  'phase',
  'plan_coverage',
  'plan_failed',
  'plan_locked',
  'plan_missing',
  'plan_questions',
  'plan_ready',
  'plan_step',
  'plan_step_result',
  'plan_unchanged',
  'plan_updated',
  'planning_started',
  'preview_check_needed',
  'progress',
  'qa_note',
  'qa_done',
  'result',
  'smart_result',
  'steer_applied',
  'steer_closed',
  'steer_ready',
  'steer_unapplied',
  'task_blocked',
  'task_cancelled',
  'task_done',
  'task_plan',
  'task_progress',
  'task_skipped',
  'task_start',
  'task_thinking',
  'tasks_complete',
  'thinking',
  'thinking_end',
  'thinking_start',
  'token',
  'tool_activity',
  'web_search_query',
  'web_search_results',
  'web_search_start',
] as const

export type StreamEventType = (typeof STREAM_EVENT_TYPES)[number]

export type StreamEvent = { type: StreamEventType; [key: string]: unknown }

const ANALYZE = /reading \d+ file|parsing file|found \d+ symbol|looking up|loaded:|claude is analyzing|searching codebase|^loading:|🐙 github:|checking history:/i
const CODE = /preparing code|narrowing:|writing code changes|✍️/i
const QA = /running qa|QA\s*[✅⚠️🚫⏭]/i

export type PhaseName = 'analyze' | 'code' | 'qa'

export function phaseForProgress(content: string): PhaseName | null {
  if (QA.test(content)) return 'qa'
  if (CODE.test(content)) return 'code'
  if (ANALYZE.test(content)) return 'analyze'
  return null
}

export function phaseRank(phase: string | null | undefined): number {
  if (phase === 'analyze') return 1
  if (phase === 'code') return 2
  if (phase === 'qa') return 3
  if (phase === 'done') return 4
  return 0
}

const PHASE_LABELS: Record<string, string> = {
  analyze: 'Reading the code',
  code: 'Writing the change',
  qa: 'Checking the change',
  done: 'Done',
}

export function phaseLabel(phase: string | number | null | undefined): string {
  const name = typeof phase === 'number'
    ? (['', 'analyze', 'code', 'qa', 'done'][phase] || '')
    : (phase || '')
  return PHASE_LABELS[name] || ''
}
