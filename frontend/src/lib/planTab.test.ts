import test from 'node:test'
import assert from 'node:assert/strict'
import { planStatusBadge, shouldQueueAfterStart } from './planTab.ts'

test('plan badge names the build state', () => {
  assert.equal(planStatusBadge('ready', [{ status: 'pending' }]), 'Ready')
  assert.equal(planStatusBadge('implementing', [{ status: 'done' }, { status: 'pending' }]), 'Building 1/2')
  assert.equal(planStatusBadge('complete', [{ status: 'done' }]), 'Done')
  assert.equal(
    planStatusBadge('ready', [{ status: 'pending', result_summary: 'missing' }, { status: 'done' }]),
    'Needs work 1',
  )
})

test('a run with no pending tasks is not sent to execute-task', () => {
  assert.equal(shouldQueueAfterStart({ ok: false, mode: 'no_tasks' }), false)
  assert.equal(shouldQueueAfterStart({ ok: true, mode: 'server' }), false)
  assert.equal(shouldQueueAfterStart({ ok: false, mode: 'disabled' }), true)
})
