/**
 * One switch for a smart-stream SSE object.
 *
 * client.ts parses the line and calls this. Unknown types warn in dev
 * instead of vanishing in an empty catch.
 */
import type { StreamEvent } from './streamEvents.ts'

type Send = (msg: unknown) => void

export type StreamHandlers = {
  onProgress: (msg: string) => void
  pushToken: (token: string) => void
  onResult: (result: any) => void
  noteModel: (model: string) => void
  fireDone: (sources?: any[]) => void
  onError: (err: string) => void
  onCreditPaused?: (info: any) => void
  onWebSearch?: (event: any) => void
  onThinking?: (text: string, phase: 'start' | 'delta' | 'end') => void
  onCompacting?: (phase: 'start' | 'done', info?: { summary?: string; compacted_count?: number }) => void
  onEditStart?: () => void
  onEditEnd?: () => void
  onEditLive?: (event: any) => void
  onFileNeeded?: (info: { filename: string; message: string; retry?: boolean }, respond: (resp: any) => boolean) => void
  onFileCleared?: (filename: string) => void
  onAwsCli?: (event: any) => void
  onAwsApproval?: (command: string, respond: (resp: any) => boolean, meta: any) => void
  onGitCli?: (event: any) => void
  onGitApproval?: (command: string, respond: (resp: any) => boolean, meta: any) => void
  onGitStep?: (event: any) => void
  onGitResult?: (event: any) => void
  onLocalCli?: (event: any) => void
  onLocalApproval?: (command: string, respond: (resp: any) => boolean, meta: any) => void
  onToolActivity?: (event: any) => void
  onPreviewCheck?: (event: any, respond: (payload: any) => boolean) => void
  onMcpApproval?: (info: any, respond: (resp: any) => boolean) => void
  onMcpCall?: (event: any) => void
  onSteer?: (event: any) => void
  onTask?: (event: any) => void
  onPlan?: (event: any) => void
  onPhase?: (phase: string) => void
  onQaNote?: (note: any) => void
  onQaDone?: (event: any) => void
  setSteerLive: (live: boolean) => void
  getSendToServer: () => Send | null
}

const TASK_TYPES = new Set([
  'planning_started', 'task_plan', 'task_start', 'task_progress', 'task_done',
  'task_thinking', 'task_blocked', 'task_cancelled', 'task_skipped', 'tasks_complete',
])

const PLAN_TYPES = new Set([
  'plan_ready', 'plan_updated', 'plan_unchanged', 'plan_locked', 'plan_coverage',
  'plan_missing', 'plan_failed', 'plan_questions', 'plan_step',
])

function warnUnknown(type: string) {
  const env = (import.meta as { env?: { DEV?: boolean } }).env
  if (env?.DEV) console.warn('unknown stream event', type)
}

function creditInfo(chunk: StreamEvent) {
  const content = chunk.content
  if (content && typeof content === 'object') return content
  if (typeof content === 'string') {
    try { return JSON.parse(content) } catch { return { message: content } }
  }
  return chunk
}

export function dispatchStreamChunk(chunk: StreamEvent, h: StreamHandlers) {
  const send = () => h.getSendToServer()
  switch (chunk.type) {
    case 'progress':
      h.onProgress(String(chunk.content ?? ''))
      return
    case 'token':
    case 'chat':
      h.pushToken(String(chunk.content ?? ''))
      return
    case 'smart_result': {
      const result = JSON.parse(String(chunk.content ?? '{}'))
      if (chunk.model) {
        h.noteModel(String(chunk.model))
        result._model = chunk.model
      }
      h.onResult(result)
      return
    }
    case 'done':
      if (chunk.model) h.noteModel(String(chunk.model))
      h.fireDone(chunk.web_search_sources as any[] | undefined)
      return
    case 'error':
      h.onError(String(chunk.content ?? ''))
      return
    case 'credit_paused':
      h.onCreditPaused?.(creditInfo(chunk))
      return
    case 'web_search_start':
      h.onWebSearch?.({ phase: 'start' })
      return
    case 'web_search_query':
      h.onWebSearch?.({ phase: 'query', query: chunk.content })
      return
    case 'web_search_results':
      h.onWebSearch?.({ phase: 'results', results: chunk.results || [], error: chunk.error })
      return
    case 'thinking_start':
      h.onThinking?.('', 'start')
      return
    case 'thinking':
      h.onThinking?.(String(chunk.content ?? ''), 'delta')
      return
    case 'thinking_end':
      h.onThinking?.('', 'end')
      return
    case 'compacting':
      h.onCompacting?.('start')
      return
    case 'compacting_done':
      h.onCompacting?.('done', { summary: chunk.summary as string | undefined, compacted_count: chunk.compacted_count as number | undefined })
      return
    case 'edit_start':
      h.onEditStart?.()
      return
    case 'edit_end':
      h.onEditEnd?.()
      return
    case 'edit_live':
      h.onEditLive?.(chunk)
      return
    case 'file_needed':
      h.onFileNeeded?.(
        { filename: String(chunk.filename ?? ''), message: String(chunk.content ?? ''), retry: chunk.retry as boolean | undefined },
        (resp) => {
          const sender = send()
          if (!sender) return false
          sender({ type: 'file_response', ...resp })
          return true
        },
      )
      return
    case 'aws_cli':
      h.onAwsCli?.(chunk)
      return
    case 'git_cli':
      h.onGitCli?.(chunk)
      return
    case 'git_approval_needed':
      h.onGitApproval?.(String(chunk.command ?? ''), (resp) => {
        const sender = send()
        if (!sender) return false
        sender({ type: 'git_cli_response', ...resp })
        return true
      }, { level: chunk.level, title: chunk.title, target: chunk.target, summary: chunk.summary })
      return
    case 'local_cli':
      h.onLocalCli?.(chunk)
      return
    case 'local_approval_needed':
      h.onLocalApproval?.(String(chunk.command ?? ''), (resp) => {
        const sender = send()
        if (!sender) return false
        sender({ type: 'local_cli_response', ...resp })
        return true
      }, {
        level: chunk.level, title: chunk.title, target: chunk.target,
        summary: chunk.summary, action: chunk.action, path: chunk.path, cwd: chunk.cwd,
      })
      return
    case 'git_step':
      h.onGitStep?.(chunk)
      return
    case 'git_result':
      h.onGitResult?.(chunk)
      return
    case 'tool_activity':
      h.onToolActivity?.(chunk)
      return
    case 'aws_approval_needed':
      h.onAwsApproval?.(String(chunk.command ?? ''), (resp) => {
        const sender = send()
        if (!sender) return false
        sender({ type: 'aws_cli_response', ...resp })
        return true
      }, { level: chunk.level, title: chunk.title, target: chunk.target })
      return
    case 'preview_check_needed':
      h.onPreviewCheck?.(chunk, (payload) => {
        const sender = send()
        if (!sender) return false
        const body = JSON.stringify({ type: 'preview_check_response', check_id: chunk.check_id, ...payload })
        if (body.length > 6000) return false
        sender({ type: 'preview_check_response', check_id: chunk.check_id, ...payload })
        return true
      })
      return
    case 'mcp_approval_needed':
      h.onMcpApproval?.({
        server: chunk.server || '',
        server_name: chunk.server_name,
        tool: chunk.tool || '',
        arguments: chunk.arguments || {},
      }, (resp) => {
        const sender = send()
        if (!sender) return false
        sender({ type: 'mcp_call_response', action: resp.action })
        return true
      })
      return
    case 'mcp_call':
      h.onMcpCall?.(chunk)
      return
    case 'steer_ready':
      h.setSteerLive(true)
      h.onSteer?.(chunk)
      return
    case 'steer_closed':
      h.setSteerLive(false)
      h.onSteer?.(chunk)
      return
    case 'steer_applied':
    case 'steer_unapplied':
      h.onSteer?.(chunk)
      return
    case 'file_needed_cleared':
      h.onFileCleared?.(String(chunk.filename ?? ''))
      return
    case 'phase':
      h.onPhase?.(String(chunk.phase ?? ''))
      return
    case 'qa_note':
      h.onQaNote?.(chunk)
      return
    case 'qa_done':
      h.onQaDone?.(chunk)
      return
    default:
      if (TASK_TYPES.has(chunk.type)) {
        h.onTask?.(chunk)
        return
      }
      if (PLAN_TYPES.has(chunk.type)) {
        h.onPlan?.(chunk)
        return
      }
      warnUnknown(chunk.type)
  }
}
