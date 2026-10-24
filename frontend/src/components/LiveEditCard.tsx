import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { LiveEditCardState } from '../stores/appStore'
import { liveDiff, splitLines, type DiffRow } from '../lib/liveDiff'

const ROW_WINDOW = 400

function fileLabel(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  if (['ts', 'tsx', 'js', 'jsx'].includes(ext)) return 'JS'
  if (ext === 'py') return 'PY'
  if (['css', 'scss'].includes(ext)) return 'CSS'
  if (ext === 'json') return '{}'
  if (['md', 'html'].includes(ext)) return ext.toUpperCase()
  return 'FILE'
}

function rowsFor(card: LiveEditCardState): DiffRow[] {
  const writing = card.status === 'writing'
  if (card.mode === 'snippet' && writing) {
    return [
      ...splitLines(card.oldCode).map((text) => ({ type: 'removed' as const, text })),
      ...splitLines(card.newCode).map((text) => ({ type: 'added' as const, text })),
    ]
  }
  const original = card.mode === 'snippet'
    ? splitLines(card.oldCode)
    : card.original == null
      ? null
      : splitLines(card.original)
  const partialTail = writing && card.newCode.length > 0 && !card.newCode.endsWith('\n')
  return liveDiff(original, splitLines(card.newCode), !writing, partialTail)
}

function rowStyle(type: DiffRow['type']): { bg: string; prefix: string; color: string; prefixColor: string } {
  if (type === 'removed') return { bg: '#3d1a1a', prefix: '-', color: '#ffa0a0', prefixColor: '#f85149' }
  if (type === 'added') return { bg: '#1a3d1a', prefix: '+', color: '#a0ffa0', prefixColor: '#3fb950' }
  if (type === 'pending') return { bg: 'transparent', prefix: ' ', color: 'rgb(var(--c-muted))', prefixColor: '#8b949e' }
  return { bg: 'transparent', prefix: ' ', color: '#e6edf3', prefixColor: '#8b949e' }
}

function LiveEditCardView({ card }: { card: LiveEditCardState }) {
  const [expanded, setExpanded] = useState(false)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const writing = card.status === 'writing'
  const dropped = card.status === 'dropped'

  const rows = useMemo(
    () => rowsFor(card),
    [card],
  )
  const added = rows.reduce((n, row) => n + (row.type === 'added' ? 1 : 0), 0)
  const removed = rows.reduce((n, row) => n + (row.type === 'removed' ? 1 : 0), 0)
  const hidden = writing && rows.length > ROW_WINDOW ? rows.length - ROW_WINDOW : 0
  const visible = hidden ? rows.slice(rows.length - ROW_WINDOW) : rows
  let lastAdded = -1
  for (let i = visible.length - 1; i >= 0; i--) {
    if (visible[i].type === 'added') {
      lastAdded = i
      break
    }
  }
  const range = card.startLine != null && card.endLine != null ? `L${card.startLine}–L${card.endLine}` : ''

  useEffect(() => {
    const el = scroller.current
    if (!el || !stick.current || !writing) return
    el.scrollTop = el.scrollHeight
  }, [card.newCode, card.oldCode, writing])

  return (
    <div
      className="my-2 rounded-lg overflow-hidden border border-border"
      style={{ opacity: dropped ? 0.55 : 1, background: 'rgb(var(--c-base))' }}
    >
      <div
        className="flex items-center gap-2 px-3 py-1.5 text-[12px]"
        style={{ background: 'rgb(var(--c-surface))', borderBottom: '1px solid rgb(var(--c-border))' }}
      >
        <span className="text-[9px] font-semibold tracking-wide text-muted px-1 py-0.5 rounded border border-border">
          {fileLabel(card.filename)}
        </span>
        <span
          className="font-medium text-ink/90 truncate min-w-0"
          style={{ textDecoration: dropped ? 'line-through' : undefined }}
          title={card.filename}
        >
          {card.filename}
        </span>
        {card.symbol ? <span className="text-muted truncate">{card.symbol}</span> : null}
        {range ? <span className="text-muted tabular-nums">{range}</span> : null}
        <span className="ml-auto flex items-center gap-2 flex-shrink-0 tabular-nums">
          {removed > 0 ? <span style={{ color: '#f85149' }}>-{removed}</span> : null}
          {added > 0 ? <span style={{ color: '#3fb950' }}>+{added}</span> : null}
          {dropped ? (
            <span className="text-warning/90">{card.reason ? `Dropped: ${card.reason}` : 'Dropped'}</span>
          ) : writing ? (
            <span className="flex items-center gap-1.5 text-warning/90">
              <span className="w-1.5 h-1.5 rounded-full bg-warning animate-pulse" />
              {card.capped ? '…still writing' : card.mode === 'snippet' ? 'reading original' : 'writing'}
            </span>
          ) : (
            <span className="text-muted">ready</span>
          )}
        </span>
      </div>
      {card.originalTruncated ? (
        <div className="px-3 py-1 text-[11px] text-muted" style={{ borderBottom: '1px solid rgb(var(--c-border))' }}>
          Original preview truncated
        </div>
      ) : null}
      <div
        ref={scroller}
        onScroll={() => {
          const el = scroller.current
          if (!el) return
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
        }}
        style={{ overflow: 'auto', maxHeight: expanded ? undefined : 320 }}
      >
        {hidden > 0 ? (
          <div className="px-3 text-[11px] text-muted" style={{ lineHeight: '20px' }}>
            {hidden} earlier lines
          </div>
        ) : null}
        {visible.map((row, idx) => {
          const style = rowStyle(row.type)
          const isCaret = writing && idx === lastAdded
          return (
            <div key={idx} style={{ background: style.bg, display: 'flex', minHeight: '20px' }}>
              <span style={{ color: style.prefixColor, width: '20px', flexShrink: 0, paddingLeft: '8px', fontFamily: 'monospace', fontSize: '12px', lineHeight: '20px', userSelect: 'none' }}>
                {style.prefix}
              </span>
              <span style={{ color: 'rgb(var(--c-muted))', width: '36px', flexShrink: 0, textAlign: 'right', paddingRight: '8px', fontFamily: 'monospace', fontSize: '11px', lineHeight: '20px', userSelect: 'none' }}>
                {row.origLine ?? ''}
              </span>
              <span style={{ color: style.color, fontFamily: 'monospace', fontSize: '12px', lineHeight: '20px', whiteSpace: 'pre', flex: 1 }}>
                {row.text}
                {isCaret ? (
                  <span className="inline-block w-1.5 h-3 ml-0.5 align-text-bottom bg-accent/80 animate-pulse" />
                ) : null}
              </span>
            </div>
          )
        })}
        {visible.length === 0 && writing ? (
          <div className="px-3 text-[12px] text-muted" style={{ lineHeight: '20px' }}>
            <span className="inline-block w-1.5 h-3 align-text-bottom bg-accent/80 animate-pulse" />
          </div>
        ) : null}
      </div>
      {rows.length > 12 ? (
        <button
          type="button"
          className="w-full text-left px-3 py-1 text-[11px] text-muted hover:text-ink"
          style={{ borderTop: '1px solid rgb(var(--c-border))', background: 'rgb(var(--c-surface))' }}
          onClick={() => setExpanded((open) => !open)}
        >
          {expanded ? 'Collapse' : 'Expand'}
        </button>
      ) : null}
    </div>
  )
}

export const LiveEditCard = React.memo(LiveEditCardView)
