/**
 * ElementPickerToolbar — floating bar below the preview.
 *
 * Shows pick-mode toggle, selected element count, element list,
 * clear button, and "Copy to Chat" button.
 */

import React from 'react'
import { useElementPickerStore, type PickedElement } from '../../stores/elementPickerStore'
import { CenterFocusStrong, Close, ContentCopy, DeleteSweep } from '@mui/icons-material'

/* ── Single selected-element chip ──────────────────────────────── */
function ElementChip({ el, index }: { el: PickedElement; index: number }) {
  const removeElement = useElementPickerStore((s) => s.removeElement)

  const label = el.id
    ? `#${el.id}`
    : el.classes.length
      ? `.${el.classes[0]}`
      : el.tag

  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-success/15 text-success border border-success/30 rounded-full text-[11px] font-medium max-w-[160px]">
      <span className="font-bold text-[10px] opacity-70">{index + 1}</span>
      <span className="truncate">&lt;{el.tag}&gt;{label !== el.tag ? ` ${label}` : ''}</span>
      <button
        onClick={() => removeElement(el.index)}
        className="ml-0.5 opacity-60 hover:opacity-100 transition-opacity"
        title="Deselect"
      >
        <Close sx={{ fontSize: 10 }} />
      </button>
    </span>
  )
}

/* ── Toolbar ───────────────────────────────────────────────────── */
export function ElementPickerToolbar() {
  const pickMode = useElementPickerStore((s) => s.pickMode)
  const setPickMode = useElementPickerStore((s) => s.setPickMode)
  const selectedElements = useElementPickerStore((s) => s.selectedElements)
  const clearElements = useElementPickerStore((s) => s.clearElements)
  const getFormattedContext = useElementPickerStore((s) => s.getFormattedContext)

  const handleCopy = () => {
    const ctx = getFormattedContext()
    if (!ctx) return
    navigator.clipboard.writeText(ctx)
  }

  return (
    <div className="flex items-center gap-2 px-3 py-1.5 bg-surface border-t border-border min-h-[36px] flex-wrap">
      {/* Pick mode toggle */}
      <button
        onClick={() => setPickMode(!pickMode)}
        className={`
          inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-semibold
          transition-all duration-150
          ${pickMode
            ? 'bg-blue-500 text-white shadow-sm shadow-blue-500/30'
            : 'bg-surface-alt text-muted hover:text-ink hover:bg-overlay border border-border'
          }
        `}
        title={pickMode ? 'Stop picking (ESC)' : 'Pick UI elements'}
      >
        <CenterFocusStrong sx={{ fontSize: 13 }} />
        {pickMode ? 'Picking…' : 'Pick Elements'}
      </button>

      {/* Selected elements */}
      {selectedElements.length > 0 && (
        <>
          <span className="text-[11px] text-muted font-medium">
            {selectedElements.length} selected
          </span>

          <div className="flex items-center gap-1 flex-wrap">
            {selectedElements.map((el, i) => (
              <ElementChip key={el.index} el={el} index={i} />
            ))}
          </div>

          <div className="flex items-center gap-1 ml-auto">
            <button
              onClick={handleCopy}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-semibold
                bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 transition-colors"
              title="Copy element context to clipboard"
            >
              <ContentCopy sx={{ fontSize: 11 }} />
              Copy
            </button>
            <button
              onClick={clearElements}
              className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-semibold
                bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors"
              title="Clear all selections"
            >
              <DeleteSweep sx={{ fontSize: 11 }} />
              Clear
            </button>
          </div>
        </>
      )}

      {/* Hint when pick mode is on but nothing selected yet */}
      {pickMode && selectedElements.length === 0 && (
        <span className="text-[11px] text-muted italic">
          Click elements in the preview to select them · ESC to exit
        </span>
      )}
    </div>
  )
}
