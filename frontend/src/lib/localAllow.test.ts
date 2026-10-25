/**
 * Run from frontend/: node --experimental-strip-types --test src/lib/localAllow.test.ts
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { canAlwaysAllowLocal } from './localAllow.ts'

test('change and network can be always-allowed', () => {
  assert.equal(canAlwaysAllowLocal('change', 'npm test'), true)
  assert.equal(canAlwaysAllowLocal('network', 'curl https://example.com'), true)
})

test('deletes, protected paths, and shell scripts cannot', () => {
  assert.equal(canAlwaysAllowLocal('delete', 'rm note.txt'), false)
  assert.equal(canAlwaysAllowLocal('protected', 'cat .env'), false)
  assert.equal(canAlwaysAllowLocal('change', 'bash -lc "npm test"'), false)
  assert.equal(canAlwaysAllowLocal('read', 'ls'), false)
})
