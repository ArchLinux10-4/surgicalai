/**
 * Run from frontend/: node --experimental-strip-types --test src/lib/toolActivity.test.ts
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { applyToolEvent, deriveStage, stopRunning, summarizeTrail } from './toolActivity.ts'
import type { ToolActivityEvent, ToolRow } from '../types/index.ts'

function row(partial: Partial<ToolRow> & Pick<ToolRow, 'id'>): ToolRow {
  return {
    kind: 'search',
    label: 'Searching the code',
    doneLabel: 'Searched the code',
    status: 'running',
    startedAt: 1_000,
    ...partial,
  }
}

function event(partial: Partial<ToolActivityEvent> & Pick<ToolActivityEvent, 'id' | 'phase'>): ToolActivityEvent {
  return { type: 'tool_activity', ...partial }
}

describe('applyToolEvent', () => {
  it('ignores a duplicate start and an end or update for an unknown id', () => {
    const start = event({ id: 't1', phase: 'start', kind: 'search', label: 'Searching the code' })
    const once = applyToolEvent([], start, 5_000)
    const twice = applyToolEvent(once, start, 6_000)
    assert.equal(twice, once)
    assert.equal(twice.length, 1)

    const end = applyToolEvent(once, event({ id: 'missing', phase: 'end', ok: false }), 7_000)
    const update = applyToolEvent(once, event({ id: 'missing', phase: 'update', status: 'waiting' }), 7_000)
    assert.equal(end, once)
    assert.equal(update, once)
    assert.doesNotThrow(() => applyToolEvent(once, event({ id: '', phase: 'end' }), 1))
    assert.doesNotThrow(() => applyToolEvent(once, { type: 'tool_activity', id: 't1', phase: 'nope' } as unknown as ToolActivityEvent, 1))
  })
})

describe('stopRunning', () => {
  it('stops running and waiting rows and leaves finished rows untouched', () => {
    const done = row({ id: 'done', status: 'done', ms: 10 })
    const running = row({ id: 'run', status: 'running' })
    const waiting = row({ id: 'wait', status: 'waiting' })
    const next = stopRunning([done, running, waiting], 4_000)
    assert.equal(next[0], done)
    assert.equal(next[1].status, 'stopped')
    assert.equal(next[2].status, 'stopped')
    assert.equal(next[1].ms, 3_000)
  })
})

describe('deriveStage', () => {
  it('prefers approval, then the running tool, and never falls back to Thinking while work is visible', () => {
    const running = row({ id: 'run', status: 'running', label: 'Reading files', target: 'a.py' })
    const waiting = row({ id: 'wait', status: 'waiting' })
    assert.equal(
      deriveStage({ rows: [running], isThinking: true, content: 'hello' }).label,
      'Reading files · a.py',
    )
    assert.equal(
      deriveStage({ rows: [running, waiting], content: 'hello' }).label,
      'Waiting for your approval',
    )
    assert.notEqual(deriveStage({ rows: [running] }).label, 'Thinking')
    assert.equal(deriveStage({ rows: [], content: 'Hello' }).label, 'Writing the reply')
    assert.notEqual(deriveStage({ rows: [], content: 'Hello' }).label, 'Thinking')
  })
})

describe('summarizeTrail', () => {
  it('excludes phase rows, handles one tool versus many, and never yields NaN', () => {
    const search = row({ id: 's', status: 'done', ms: 20_000 })
    const file = row({ id: 'f', kind: 'file', label: 'Reading files', doneLabel: 'Read files', status: 'done', ms: 4_000 })
    const qa = row({ id: 'q', kind: 'qa', label: 'Running QA', doneLabel: 'Ran QA', status: 'done', ms: 1_000 })
    assert.equal(summarizeTrail([search, qa]), 'Used 1 tool · 21s')
    assert.equal(summarizeTrail([search, file]), 'Used 2 tools · 24s')
    assert.equal(summarizeTrail([qa]), 'Finished in 1.0s')
    const missing = row({ id: 'm', status: 'done', startedAt: 0, ms: undefined })
    const text = summarizeTrail([missing])
    assert.equal(text, 'Used 1 tool')
    assert.equal(text.includes('NaN'), false)
  })
})
