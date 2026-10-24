export type DiffRow = {
  type: 'same' | 'added' | 'removed' | 'pending'
  text: string
  origLine?: number
}

const LOOKAHEAD = 8
const LCS_CAP = 800

/** Split on newlines. A trailing newline does not produce an extra blank row. */
export function splitLines(text: string): string[] {
  if (!text) return []
  const parts = text.split('\n')
  if (text.endsWith('\n')) parts.pop()
  return parts
}

/**
 * Diff `partialNew` against `original`.
 * While streaming, unmatched original lines still ahead of the cursor stay
 * `pending`. `partialTail` forces the last new line to `added` because the
 * model has not finished it. When `done`, a capped LCS pass removes `pending`.
 * `original === null` means there is nothing to delete (new file, unresolved).
 */
export function liveDiff(
  original: string[] | null,
  partialNew: string[],
  done: boolean,
  partialTail = false,
): DiffRow[] {
  if (original === null) {
    return partialNew.map((text) => ({ type: 'added', text }))
  }
  if (!done) return forwardWalk(original, partialNew, partialTail)
  if (original.length > LCS_CAP || partialNew.length > LCS_CAP) {
    return forwardWalk(original, partialNew, false).map((row) =>
      row.type === 'pending' ? { ...row, type: 'removed' as const } : row,
    )
  }
  return lcsDiff(original, partialNew)
}

function forwardWalk(original: string[], partialNew: string[], partialTail: boolean): DiffRow[] {
  const rows: DiffRow[] = []
  let pointer = 0
  const last = partialNew.length - 1
  for (let i = 0; i < partialNew.length; i++) {
    const line = partialNew[i]
    if (partialTail && i === last) {
      rows.push({ type: 'added', text: line })
      continue
    }
    const trimmed = line.trimEnd()
    const limit = Math.min(original.length, pointer + LOOKAHEAD)
    let found = -1
    for (let k = pointer; k < limit; k++) {
      if (original[k].trimEnd() === trimmed) {
        found = k
        break
      }
    }
    if (found >= 0) {
      for (let k = pointer; k < found; k++) {
        rows.push({ type: 'removed', text: original[k], origLine: k + 1 })
      }
      rows.push({ type: 'same', text: original[found], origLine: found + 1 })
      pointer = found + 1
    } else {
      rows.push({ type: 'added', text: line })
    }
  }
  for (let k = pointer; k < original.length; k++) {
    rows.push({ type: 'pending', text: original[k], origLine: k + 1 })
  }
  return rows
}

function lcsDiff(original: string[], modified: string[]): DiffRow[] {
  const n = original.length
  const m = modified.length
  const width = m + 1
  const dp = new Uint16Array((n + 1) * width)
  const at = (i: number, j: number) => i * width + j
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (original[i] === modified[j]) dp[at(i, j)] = dp[at(i + 1, j + 1)] + 1
      else dp[at(i, j)] = Math.max(dp[at(i + 1, j)], dp[at(i, j + 1)])
    }
  }
  const rows: DiffRow[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (original[i] === modified[j]) {
      rows.push({ type: 'same', text: original[i], origLine: i + 1 })
      i++
      j++
    } else if (dp[at(i + 1, j)] >= dp[at(i, j + 1)]) {
      rows.push({ type: 'removed', text: original[i], origLine: i + 1 })
      i++
    } else {
      rows.push({ type: 'added', text: modified[j] })
      j++
    }
  }
  while (i < n) {
    rows.push({ type: 'removed', text: original[i], origLine: i + 1 })
    i++
  }
  while (j < m) {
    rows.push({ type: 'added', text: modified[j] })
    j++
  }
  return rows
}
