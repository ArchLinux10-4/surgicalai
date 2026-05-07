import React from 'react'

interface DiffViewProps {
  original: string
  modified: string
  title?: string
}

export function DiffView({ original, modified, title }: DiffViewProps) {
  const origLines = original.split('\n')
  const modLines = modified.split('\n')

  // Compute simple line diff
  const renderLines = () => {
    const maxLen = Math.max(origLines.length, modLines.length)
    const rows: JSX.Element[] = []

    // Simple LCS-based diff
    const removed = new Set<number>()
    const added = new Set<number>()
    const unchanged = new Set<number>()

    // Build left (original) and right (modified) columns
    let oi = 0, mi = 0

    // Use longest common subsequence approach - simplified
    const lcs: [number, number][] = []
    const memo: Record<string, number> = {}

    function lcsLen(i: number, j: number): number {
      if (i >= origLines.length || j >= modLines.length) return 0
      const key = `${i},${j}`
      if (key in memo) return memo[key]
      if (origLines[i] === modLines[j]) {
        memo[key] = 1 + lcsLen(i + 1, j + 1)
      } else {
        memo[key] = Math.max(lcsLen(i + 1, j), lcsLen(i, j + 1))
      }
      return memo[key]
    }

    // For large files, just show raw diff line by line
    const diffRows: { type: 'same' | 'removed' | 'added'; content: string; lineNum?: number }[] = []

    // Simple diff: find matching lines
    let i = 0, j = 0
    const oLen = Math.min(origLines.length, 500)
    const mLen = Math.min(modLines.length, 500)

    while (i < oLen || j < mLen) {
      if (i < oLen && j < mLen && origLines[i] === modLines[j]) {
        diffRows.push({ type: 'same', content: origLines[i], lineNum: i + 1 })
        i++; j++
      } else if (j < mLen && (i >= oLen || origLines[i] !== modLines[j])) {
        // Check if this line appears later in original (likely a deletion above)
        if (i < oLen && modLines[j] !== origLines[i]) {
          // Try to find match
          let foundOrig = -1
          for (let k = i; k < Math.min(i + 5, oLen); k++) {
            if (origLines[k] === modLines[j]) { foundOrig = k; break }
          }
          if (foundOrig > i) {
            // Emit removals
            for (let k = i; k < foundOrig; k++) {
              diffRows.push({ type: 'removed', content: origLines[k], lineNum: k + 1 })
            }
            i = foundOrig
          } else {
            diffRows.push({ type: 'added', content: modLines[j], lineNum: j + 1 })
            j++
          }
        } else {
          diffRows.push({ type: 'added', content: modLines[j], lineNum: j + 1 })
          j++
        }
      } else if (i < oLen) {
        diffRows.push({ type: 'removed', content: origLines[i], lineNum: i + 1 })
        i++
      }
    }

    return diffRows.map((row, idx) => {
      const bg = row.type === 'removed' ? '#3d1a1a' : row.type === 'added' ? '#1a3d1a' : 'transparent'
      const prefix = row.type === 'removed' ? '-' : row.type === 'added' ? '+' : ' '
      const color = row.type === 'removed' ? '#ffa0a0' : row.type === 'added' ? '#a0ffa0' : '#e6edf3'
      const prefixColor = row.type === 'removed' ? '#f85149' : row.type === 'added' ? '#3fb950' : '#8b949e'

      return (
        <div key={idx} style={{ background: bg, display: 'flex', minHeight: '20px' }}>
          <span style={{ color: prefixColor, width: '20px', flexShrink: 0, paddingLeft: '8px', fontFamily: 'monospace', fontSize: '12px', lineHeight: '20px', userSelect: 'none' }}>
            {prefix}
          </span>
          <span style={{ color: 'rgb(var(--c-muted))', width: '36px', flexShrink: 0, textAlign: 'right', paddingRight: '8px', fontFamily: 'monospace', fontSize: '11px', lineHeight: '20px', userSelect: 'none' }}>
            {row.lineNum}
          </span>
          <span style={{ color, fontFamily: 'monospace', fontSize: '12px', lineHeight: '20px', whiteSpace: 'pre', flex: 1 }}>
            {row.content}
          </span>
        </div>
      )
    })
  }

  return (
    <div style={{ background: 'rgb(var(--c-base))', border: '1px solid rgb(var(--c-border))', borderRadius: '6px', overflow: 'hidden' }}>
      {title && (
        <div style={{ padding: '6px 12px', background: 'rgb(var(--c-surface))', borderBottom: '1px solid rgb(var(--c-border))', fontSize: '12px', color: 'rgb(var(--c-muted))' }}>
          {title}
        </div>
      )}
      <div style={{ overflow: 'auto', maxHeight: '400px' }}>
        {renderLines()}
      </div>
    </div>
  )
}
