import test from 'node:test'
import assert from 'node:assert/strict'
import { modifierMask, normalizeUrl } from './input.ts'

test('normalizeUrl matches the address bar rules', () => {
  assert.equal(normalizeUrl('example.com/a'), 'https://example.com/a')
  assert.equal(normalizeUrl('localhost:5173/app'), 'http://localhost:5173/app')
  assert.equal(normalizeUrl('127.0.0.1:3000'), 'http://127.0.0.1:3000')
  assert.equal(normalizeUrl('https://already.dev'), 'https://already.dev')
  assert.equal(normalizeUrl('  '), '')
})

test('modifierMask uses the CDP bit order', () => {
  assert.equal(modifierMask({ altKey: true, ctrlKey: false, metaKey: false, shiftKey: false }), 1)
  assert.equal(modifierMask({ altKey: false, ctrlKey: true, metaKey: false, shiftKey: false }), 2)
  assert.equal(modifierMask({ altKey: false, ctrlKey: false, metaKey: true, shiftKey: false }), 4)
  assert.equal(modifierMask({ altKey: false, ctrlKey: false, metaKey: false, shiftKey: true }), 8)
  assert.equal(modifierMask({ altKey: true, ctrlKey: false, metaKey: true, shiftKey: true }), 13)
})
