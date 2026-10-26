/**
 * Run from frontend/: node --experimental-strip-types --test src/api/streamDispatch.test.ts
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { dispatchStreamChunk } from './streamDispatch.ts'
import type { StreamHandlers } from './streamDispatch.ts'

function handlers(calls: string[]): StreamHandlers {
  const note = (name: string) => () => { calls.push(name) }
  return {
    onProgress: (msg) => calls.push(`progress:${msg}`),
    pushToken: (token) => calls.push(`token:${token}`),
    onResult: (result) => calls.push(`result:${result.ok}`),
    noteModel: (model) => calls.push(`model:${model}`),
    fireDone: () => calls.push('done'),
    onError: (err) => calls.push(`error:${err}`),
    onThinking: (_t, phase) => calls.push(`thinking:${phase}`),
    onPhase: (phase) => calls.push(`phase:${phase}`),
    onQaNote: (note) => calls.push(`qa:${note.level}`),
    onTask: (event) => calls.push(`task:${event.type}`),
    setSteerLive: (live) => calls.push(`steer:${live}`),
    getSendToServer: () => null,
    onEditLive: note('edit_live'),
    onPreviewCheck: note('preview'),
  }
}

describe('dispatchStreamChunk', () => {
  it('keeps the order of a recorded turn', () => {
    const calls: string[] = []
    const h = handlers(calls)
    const lines = [
      { type: 'thinking_start' },
      { type: 'thinking', content: 'Looking.' },
      { type: 'thinking_end' },
      { type: 'token', content: "I'll read the file." },
      { type: 'phase', phase: 'analyze' },
      { type: 'tool_activity', id: '1' },
      { type: 'smart_result', content: JSON.stringify({ ok: true }) },
      { type: 'qa_note', level: 'info', change_id: 'c1' },
      { type: 'done', model: 'grok-4' },
    ]
    for (const chunk of lines) dispatchStreamChunk(chunk as any, h)
    assert.deepEqual(calls, [
      'thinking:start',
      'thinking:delta',
      'thinking:end',
      "token:I'll read the file.",
      'phase:analyze',
      'result:true',
      'qa:info',
      'model:grok-4',
      'done',
    ])
  })

  it('does not call a handler for an unknown type', () => {
    const calls: string[] = []
    dispatchStreamChunk({ type: 'aws_done' } as any, handlers(calls))
    assert.deepEqual(calls, [])
  })
})
