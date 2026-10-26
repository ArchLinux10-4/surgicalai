/**
 * Run from frontend/: node --experimental-strip-types --test src/lib/stagedFiles.test.ts
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { applyStagedToFiles, latestStagedEdits } from './stagedFiles.ts'
import type { SessionFile } from '../types/index.ts'

function file(partial: Partial<SessionFile> & Pick<SessionFile, 'id' | 'filename'>): SessionFile {
  return {
    session_id: 's',
    language: 'python',
    lines: 10,
    symbol_count: 1,
    created_at: '',
    edited: 0,
    disk_state: 'applied',
    ...partial,
  }
}

test('a file present only in the latest result is edited and not saved', () => {
  const messages = [{
    surgical_data: JSON.stringify({
      changes_by_file: {
        'gpt_matching.py': {
          filename: 'gpt_matching.py',
          changes: [{ diff: '--- a\n+++ b\n@@\n-old\n+new\n' }],
        },
      },
      new_files: [{ filename: 'test_openai_models.py', content: 'def test_ok():\n    pass\n' }],
    }),
  }]
  const staged = latestStagedEdits(messages)
  const listed = applyStagedToFiles([
    file({ id: '1', filename: 'gpt_matching.py' }),
    file({ id: '2', filename: 'login.html', lines: 40 }),
  ], staged, 's')
  const edited = listed.find(row => row.filename === 'gpt_matching.py')
  const untouched = listed.find(row => row.filename === 'login.html')
  const created = listed.find(row => row.filename === 'test_openai_models.py')
  assert.equal(edited!.edited, 1)
  assert.equal(edited!.disk_state, 'pending')
  assert.equal(staged.get('gpt_matching.py')?.added, 1)
  assert.equal(staged.get('gpt_matching.py')?.removed, 1)
  assert.equal(untouched!.disk_state, 'applied')
  assert.equal(untouched!.edited, 0)
  assert.equal(created!.disk_state, 'pending')
  assert.equal(created!.edited, 1)
  assert.equal(listed.filter(row => row.edited).length, 2)
})
