import { useState } from 'react'
import { Close, DragIndicator, EditOutlined, KeyboardArrowDown, KeyboardArrowUp, Send } from '@mui/icons-material'
import type { SteerQueueItem } from '../stores/appStore'
import { PastedPill } from './PastedBlocks'
import { PastedBlockViewer } from './PastedBlocks'
import type { PastedBlock } from '../lib/pastedBlocks'

export function QueuedMessagesTray({
  items,
  paused,
  layout,
  onSendNow,
  onEdit,
  onDelete,
  onMove,
  onReorder,
  onResume,
  onClear,
}: {
  items: SteerQueueItem[]
  paused: boolean
  layout: 'desktop' | 'mobile'
  onSendNow: (item: SteerQueueItem) => void
  onEdit: (item: SteerQueueItem) => void
  onDelete: (id: string) => void
  onMove: (id: string, dir: -1 | 1) => void
  onReorder: (fromId: string, toId: string) => void
  onResume: () => void
  onClear: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [sheet, setSheet] = useState<SteerQueueItem | null>(null)
  const [openPaste, setOpenPaste] = useState<PastedBlock | null>(null)
  if (items.length === 0 && !paused) return null
  const hidden = !expanded && items.length > 3 ? items.length - 3 : 0
  const shown = hidden > 0 ? items.slice(0, 3) : items
  const always = layout === 'mobile'

  return (
    <div className="mb-2 space-y-1.5" aria-live="polite">
      {paused && items.length > 0 && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-surface/80 px-2.5 py-1.5 text-[11px] text-muted">
          <span className="flex-1">Queue paused</span>
          <button type="button" onClick={onResume} className="font-semibold text-accent hover:underline">Resume queue</button>
          <button type="button" onClick={onClear} className="hover:text-ink">Clear</button>
        </div>
      )}
      {shown.map((item, index) => (
        <div
          key={item.id}
          draggable={layout === 'desktop'}
          onDragStart={(e) => {
            e.dataTransfer.setData('text/plain', item.id)
            e.dataTransfer.effectAllowed = 'move'
          }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault()
            const from = e.dataTransfer.getData('text/plain')
            if (from) onReorder(from, item.id)
          }}
          className="motion-safe:animate-slide-up motion-reduce:animate-none group flex items-start gap-1.5 rounded-xl border border-border/70 bg-surface/80 px-2 py-1.5"
        >
          <button
            type="button"
            className="mt-0.5 text-muted/60 cursor-grab active:cursor-grabbing shrink-0"
            aria-label={`Reorder message ${index + 1}`}
            title="Drag to reorder"
          >
            <DragIndicator sx={{ fontSize: 14 }} />
          </button>
          <span className="mt-0.5 w-4 text-[10px] font-semibold text-muted shrink-0">{index + 1}</span>
          <div className="min-w-0 flex-1">
            <button
              type="button"
              className="w-full text-left"
              onClick={() => { if (layout === 'mobile') setSheet(item) }}
            >
              <p className="text-[12px] text-ink line-clamp-2 whitespace-pre-wrap break-words">
                {item.text.trim() || 'Pasted text'}
              </p>
            </button>
            {item.pasted.length > 0 && (
              <span className="mt-1 flex flex-wrap gap-1">
                {item.pasted.map((block, i) => (
                  <PastedPill key={block.id} block={block} index={i + 1} onOpen={() => setOpenPaste(block)} />
                ))}
              </span>
            )}
          </div>
          <div className={`flex items-center shrink-0 ${always ? '' : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'} transition-opacity`}>
            <button type="button" className="h-6 w-6 rounded text-muted hover:text-ink hover:bg-overlay/60" aria-label="Move up" onClick={() => onMove(item.id, -1)}>
              <KeyboardArrowUp sx={{ fontSize: 14 }} />
            </button>
            <button type="button" className="h-6 w-6 rounded text-muted hover:text-ink hover:bg-overlay/60" aria-label="Move down" onClick={() => onMove(item.id, 1)}>
              <KeyboardArrowDown sx={{ fontSize: 14 }} />
            </button>
            {layout === 'desktop' && (
              <>
                <button type="button" className="h-6 px-1.5 rounded text-[10px] font-semibold text-accent hover:bg-accent/10" onClick={() => onSendNow(item)}>Now</button>
                <button type="button" className="h-6 w-6 rounded text-muted hover:text-ink hover:bg-overlay/60" aria-label="Edit" onClick={() => onEdit(item)}>
                  <EditOutlined sx={{ fontSize: 13 }} />
                </button>
                <button type="button" className="h-6 w-6 rounded text-muted hover:text-danger hover:bg-danger/10" aria-label="Delete" onClick={() => onDelete(item.id)}>
                  <Close sx={{ fontSize: 13 }} />
                </button>
              </>
            )}
          </div>
        </div>
      ))}
      {items.length > 3 && (
        <button type="button" onClick={() => setExpanded((v) => !v)} className="text-[11px] text-muted hover:text-ink">
          {expanded ? 'Show less' : `+${items.length - 3} more`}
        </button>
      )}
      {sheet && layout === 'mobile' && (
        <div className="fixed inset-0 z-[70]" role="presentation">
          <button type="button" className="absolute inset-0 bg-black/50" aria-label="Close" onClick={() => setSheet(null)} />
          <div className="absolute inset-x-0 bottom-0 rounded-t-2xl border-t border-border bg-surface p-3 shadow-modal" role="dialog" aria-label="Queued message">
            <p className="text-[13px] text-ink line-clamp-4 whitespace-pre-wrap">{sheet.text.trim() || 'Pasted text'}</p>
            <div className="mt-3 flex flex-col gap-1.5">
              <button type="button" className="h-9 rounded-lg bg-accent text-white text-[13px] font-semibold inline-flex items-center justify-center gap-1.5" onClick={() => { onSendNow(sheet); setSheet(null) }}>
                <Send sx={{ fontSize: 14 }} /> Send now
              </button>
              <button type="button" className="h-9 rounded-lg text-[13px] text-ink hover:bg-overlay/60" onClick={() => { onEdit(sheet); setSheet(null) }}>Edit</button>
              <button type="button" className="h-9 rounded-lg text-[13px] text-danger hover:bg-danger/10" onClick={() => { onDelete(sheet.id); setSheet(null) }}>Delete</button>
            </div>
          </div>
        </div>
      )}
      {openPaste && (
        <div className="relative">
          <PastedBlockViewer block={openPaste} readOnly presentation={layout === 'mobile' ? 'sheet' : 'popover'} onClose={() => setOpenPaste(null)} />
        </div>
      )}
    </div>
  )
}
