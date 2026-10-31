import assert from 'node:assert/strict'
import test from 'node:test'
import { createFrameGate, nextBinaryFrame, takeBinaryFrame } from './framePump.ts'

test('frames that arrive mid-decode collapse to the newest', () => {
  const gate = createFrameGate()
  const first = new ArrayBuffer(1)
  const second = new ArrayBuffer(2)
  const third = new ArrayBuffer(3)
  assert.equal(takeBinaryFrame(gate, first), first)
  assert.equal(takeBinaryFrame(gate, second), null)
  assert.equal(takeBinaryFrame(gate, third), null)
  assert.equal(nextBinaryFrame(gate), third)
  assert.equal(nextBinaryFrame(gate), null)
  assert.equal(gate.decoding, false)
})
