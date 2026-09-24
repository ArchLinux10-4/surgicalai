/**
 * Run from frontend/: node --experimental-strip-types --test src/lib/diffChange.test.ts
 */
import { describe, it, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  appliedStorageKey,
  isRealDiffChange,
  skippedStorageKey,
  splitPendingChanges,
} from './diffChange.ts'

/** Minimal in-memory localStorage for Node tests. */
function installLocalStorage() {
  const store = new Map<string, string>()
  const ls = {
    getItem(key: string) {
      return store.has(key) ? store.get(key)! : null
    },
    setItem(key: string, value: string) {
      store.set(key, String(value))
    },
    removeItem(key: string) {
      store.delete(key)
    },
    clear() {
      store.clear()
    },
  }
  ;(globalThis as any).localStorage = ls
  return ls
}

describe('isRealDiffChange', () => {
  it('returns false for missing / null / empty diff', () => {
    assert.equal(isRealDiffChange(null), false)
    assert.equal(isRealDiffChange(undefined), false)
    assert.equal(isRealDiffChange({}), false)
    assert.equal(isRealDiffChange({ diff: '' }), false)
  })

  it('returns false for context-only and header-only diffs', () => {
    assert.equal(
      isRealDiffChange({
        diff: '@@ -1,3 +1,3 @@\n context line\n another context',
      }),
      false,
    )
    assert.equal(
      isRealDiffChange({
        diff: '--- a/file.ts\n+++ b/file.ts\n@@ -1 +1 @@',
      }),
      false,
    )
  })

  it('returns true when there is a real add or remove line', () => {
    assert.equal(
      isRealDiffChange({
        diff: '@@ -1,2 +1,3 @@\n context\n+added line\n more',
      }),
      true,
    )
    assert.equal(
      isRealDiffChange({
        diff: '@@ -1,2 +1,1 @@\n-removed line\n context',
      }),
      true,
    )
  })
})

describe('splitPendingChanges', () => {
  const sessionId = 'sess-test'

  beforeEach(() => {
    installLocalStorage().clear()
  })

  const realDiff = '@@ -1 +1 @@\n-old\n+new'
  const ghostDiff = '@@ -1 +1 @@\n context only'

  it('ignores ghost diffs', () => {
    const { clean, flagged } = splitPendingChanges(
      [
        { id: 'ghost', diff: ghostDiff },
        { id: 'real', diff: realDiff },
      ],
      new Set(),
      sessionId,
    )
    assert.equal(clean.length, 1)
    assert.equal(clean[0].id, 'real')
    assert.equal(flagged.length, 0)
  })

  it('ignores ids already in the DB applied set', () => {
    const { clean, flagged } = splitPendingChanges(
      [{ id: 'a', diff: realDiff }],
      new Set(['a']),
      sessionId,
    )
    assert.equal(clean.length, 0)
    assert.equal(flagged.length, 0)
  })

  it('ignores locally applied and locally skipped', () => {
    localStorage.setItem(appliedStorageKey(sessionId, 'applied-local'), '1')
    localStorage.setItem(skippedStorageKey(sessionId, 'skipped-local'), '1')
    const { clean, flagged } = splitPendingChanges(
      [
        { id: 'applied-local', diff: realDiff },
        { id: 'skipped-local', diff: realDiff },
        { id: 'pending', diff: realDiff },
      ],
      new Set(),
      sessionId,
    )
    assert.deepEqual(clean.map((c: any) => c.id), ['pending'])
    assert.equal(flagged.length, 0)
  })

  it('puts blocked verdict in flagged, not clean', () => {
    const { clean, flagged } = splitPendingChanges(
      [
        { id: 'ok', diff: realDiff, qa_result: { verdict: 'pass' } },
        { id: 'bad', diff: realDiff, qa_result: { verdict: 'blocked' } },
      ],
      new Set(),
      sessionId,
    )
    assert.deepEqual(clean.map((c: any) => c.id), ['ok'])
    assert.deepEqual(flagged.map((c: any) => c.id), ['bad'])
  })
})
