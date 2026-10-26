import test from 'node:test'
import assert from 'node:assert/strict'
import { planStatusBadge, shouldQueueAfterStart } from './planTab.ts'

test('plan badge names the build state', () => {
  assert.equal(planStatusBadge('ready', [{ status: 'pending' }]), 'Ready')
  assert.equal(planStatusBadge('implementing', [{ status: 'done' }, { status: 'pending' }]), 'Building 1/2')
  assert.equal(planStatusBadge('complete', [{ status: 'done' }]), 'Done')
  assert.equal(
    planStatusBadge('blocked', [{ status: 'blocked', result_summary: 'symbol not found' }, { status: 'done' }]),
    'Needs work 1',
  )
  assert.equal(
    planStatusBadge('ready', [{ status: 'pending', result_summary: 'Build crashed: boom' }]),
    'Build failed — retry',
  )
  assert.equal(
    planStatusBadge('ready', [{ status: 'pending', result_summary: 'Could not find `MODEL` in `a.py`.' }]),
    'Needs work 1',
  )
  assert.equal(
    planStatusBadge('blocked', [
      { status: 'blocked', result_summary: 'symbol not found' },
      { status: 'pending', result_summary: 'Could not find `login credit` in `login.html`.' },
      { status: 'done' },
    ]),
    'Needs work 2',
  )
})

test('a run with no pending tasks is not sent to execute-task', () => {
  assert.equal(shouldQueueAfterStart({ ok: false, mode: 'no_tasks' }), false)
  assert.equal(shouldQueueAfterStart({ ok: true, mode: 'server' }), false)
  assert.equal(shouldQueueAfterStart({ ok: false, mode: 'disabled' }), true)
})
