import test from 'node:test'
import assert from 'node:assert/strict'
import { parseFrame } from './frame.ts'

test('parseFrame splits metadata from jpeg bytes', () => {
  const meta = JSON.stringify({ deviceWidth: 800, deviceHeight: 600 })
  const metaBytes = new TextEncoder().encode(meta)
  const jpeg = new Uint8Array([1, 2, 3, 4])
  const buf = new ArrayBuffer(4 + metaBytes.length + jpeg.length)
  new DataView(buf).setUint32(0, metaBytes.length, false)
  new Uint8Array(buf, 4, metaBytes.length).set(metaBytes)
  new Uint8Array(buf, 4 + metaBytes.length).set(jpeg)
  const parsed = parseFrame(buf)
  assert.equal(parsed.metadata.deviceWidth, 800)
  assert.equal(parsed.metadata.deviceHeight, 600)
  assert.deepEqual(Array.from(parsed.jpeg), [1, 2, 3, 4])
})
