import { useEffect, useState } from 'react'
import { Check, Close, ContentCopy, EditOutlined, TextSnippet, UnfoldLess, UnfoldMore } from '@mui/icons-material'
import {
  PASTE_VIEW_CAP,
  formatPasteMeta,
  parsePastedBlocks,
  type PastedBlock,
} from '../lib/pastedBlocks'

function useCopy(text: string) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }).catch(() => {})
  }
  return { copied, copy }
}

export function PastedBlockViewer({
  block,
  readOnly,
  presentation,
  onClose,
  onSave,
  onDelete,
  onConvert,
}: {
  block: PastedBlock
  readOnly?: boolean
  presentation: 'popover' | 'sheet'
  onClose: () => void
  onSave?: (text: string) => void
  onDelete?: () => void
  onConvert?: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(block.text)
  const [showAll, setShowAll] = useState(false)
  const { copied, copy } = useCopy(editing ? draft : block.text)
  const huge = block.text.length > PASTE_VIEW_CAP && !showAll && !editing
  const shown = huge ? block.text.slice(0, PASTE_VIEW_CAP) : block.text

  useEffect(() => {
    setDraft(block.text)
    setEditing(false)
    setShowAll(false)
  }, [block.id, block.text])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        onClose()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onClose])

  const frame = presentation === 'sheet'
    ? 'fixed inset-x-0 bottom-0 z-[70] max-h-[85vh] rounded-t-2xl border-t border-border bg-surface shadow-modal flex flex-col'
    : 'absolute bottom-full left-0 right-0 z-30 mb-2 max-h-[70vh] rounded-xl border border-border bg-surface shadow-modal flex flex-col'

  return (
    <div className={presentation === 'sheet' ? 'fixed inset-0 z-[70]' : 'contents'} role={presentation === 'sheet' ? 'presentation' : undefined}>
      {presentation === 'sheet' && (
        <button type="button" className="absolute inset-0 bg-black/50" aria-label="Close pasted text" onClick={onClose} />
      )}
      <div className={frame} role="dialog" aria-modal="true" aria-label="Pasted text">
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border/70">
          <TextSnippet sx={{ fontSize: 16 }} className="text-accent shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="text-[12px] font-semibold text-ink">Pasted text</div>
            <div className="text-[10px] text-muted truncate">{formatPasteMeta(editing ? draft : block.text)}</div>
          </div>
          <button type="button" onClick={copy} className="h-7 px-2 rounded-md text-[11px] text-muted hover:text-ink hover:bg-overlay/60 inline-flex items-center gap-1" aria-label={copied ? 'Copied' : 'Copy pasted text'}>
            {copied ? <Check sx={{ fontSize: 13 }} className="text-success" /> : <ContentCopy sx={{ fontSize: 13 }} />}
            {copied ? 'Copied' : 'Copy'}
          </button>
          <button type="button" onClick={onClose} className="h-7 w-7 rounded-md text-muted hover:text-ink hover:bg-overlay/60 inline-flex items-center justify-center" aria-label="Close">
            <Close sx={{ fontSize: 14 }} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-3 py-2">
          {editing ? (
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              className="w-full min-h-[220px] bg-base/40 border border-border rounded-lg p-2 text-[12px] leading-relaxed font-mono text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
              aria-label="Edit pasted text"
            />
          ) : (
            <pre className="text-[12px] leading-relaxed font-mono text-ink/90 whitespace-pre-wrap break-words">
              {shown}
              {huge ? '\n…' : ''}
            </pre>
          )}
          {huge && (
            <button type="button" onClick={() => setShowAll(true)} className="mt-2 text-[12px] font-medium text-accent hover:underline">
              Show all
            </button>
          )}
        </div>
        {!readOnly && (
          <div className="flex flex-wrap items-center gap-1.5 px-3 py-2 border-t border-border/70">
            {editing ? (
              <>
                <button type="button" onClick={() => { onSave?.(draft); setEditing(false) }} className="h-7 px-2.5 rounded-md bg-accent text-white text-[11px] font-semibold">Save</button>
                <button type="button" onClick={() => { setDraft(block.text); setEditing(false) }} className="h-7 px-2.5 rounded-md text-[11px] text-muted hover:bg-overlay/60">Cancel</button>
              </>
            ) : (
              <>
                <button type="button" onClick={() => setEditing(true)} className="h-7 px-2 rounded-md text-[11px] text-ink hover:bg-overlay/60 inline-flex items-center gap-1">
                  <EditOutlined sx={{ fontSize: 13 }} /> Edit
                </button>
                {onConvert && (
                  <button type="button" onClick={onConvert} className="h-7 px-2 rounded-md text-[11px] text-ink hover:bg-overlay/60 inline-flex items-center gap-1">
                    <UnfoldMore sx={{ fontSize: 13 }} /> Convert to text
                  </button>
                )}
                {onDelete && (
                  <button type="button" onClick={onDelete} className="h-7 px-2 rounded-md text-[11px] text-danger hover:bg-danger/10">Delete</button>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export function PastedPill({
  block,
  index,
  warn,
  onOpen,
  onRemove,
}: {
  block: PastedBlock
  index: number
  warn?: boolean
  onOpen: () => void
  onRemove?: () => void
}) {
  const meta = formatPasteMeta(block.text)
  const preview = block.text.split(/\r?\n/).map((line) => line.trim()).find(Boolean) || 'Pasted text'
  return (
    <div className={`motion-safe:animate-slide-up motion-reduce:animate-none flex items-center gap-1.5 max-w-full pl-2 pr-1 py-1 rounded-lg border text-[11px] font-medium ${
      warn ? 'bg-danger/10 border-danger/30 text-danger' : 'bg-accent/10 border-accent/25 text-accent'
    }`}>
      <button
        type="button"
        onClick={onOpen}
        onKeyDown={(e) => {
          if ((e.key === 'Backspace' || e.key === 'Delete') && onRemove) {
            e.preventDefault()
            onRemove()
          }
        }}
        title={preview}
        aria-label={`Pasted text ${index}, ${meta}${warn ? ', may be too large for the model' : ''}`}
        className="flex items-center gap-1.5 min-w-0 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
      >
        <TextSnippet sx={{ fontSize: 13 }} className="shrink-0" />
        <span className="truncate">Pasted text #{index}</span>
        <span className={`truncate font-normal ${warn ? 'text-danger/80' : 'text-muted'}`}>{meta}</span>
      </button>
      {onRemove && (
        <button type="button" onClick={onRemove} className="shrink-0 p-0.5 rounded hover:bg-accent/20" aria-label={`Remove pasted text ${index}`} title="Remove">
          <Close sx={{ fontSize: 12 }} />
        </button>
      )}
    </div>
  )
}

export function UserMessageBody({
  content,
  steered,
  sending,
}: {
  content: string
  steered?: boolean
  sending?: boolean
}) {
  const [openId, setOpenId] = useState<string | null>(null)
  const { visibleText, blocks } = parsePastedBlocks(content || '')
  const openBlock = blocks.find((block) => block.id === openId) || null
  return (
    <div>
      {(sending || steered) && (
        <div className="flex justify-end mb-1">
          {sending ? (
            <span className="text-[10px] text-muted inline-flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-accent/70 animate-pulse motion-reduce:animate-none" />
              Sending…
            </span>
          ) : (
            <span className="text-[10px] font-medium text-accent bg-accent/10 px-1.5 py-0.5 rounded">Steered</span>
          )}
        </div>
      )}
      {visibleText.trim() && (
        <div className="bg-overlay/60 border border-border/40 rounded-2xl rounded-tr-sm px-4 py-2.5 text-sm text-ink leading-relaxed whitespace-pre-wrap shadow-sm break-words">
          {visibleText}
        </div>
      )}
      {blocks.length > 0 && (
        <div className="mt-1.5 flex flex-wrap justify-end gap-1.5">
          {blocks.map((block, i) => (
            <PastedPill key={block.id} block={block} index={i + 1} onOpen={() => setOpenId(block.id)} />
          ))}
        </div>
      )}
      {openBlock && (
        <div className="relative mt-2">
          <PastedBlockViewer block={openBlock} readOnly presentation="popover" onClose={() => setOpenId(null)} />
        </div>
      )}
    </div>
  )
}

export function ComposerPastedTray({
  blocks,
  warn,
  presentation,
  undo,
  onUndo,
  onRemove,
  onSave,
  onConvert,
  onDismissUndo,
}: {
  blocks: PastedBlock[]
  warn?: boolean
  presentation: 'popover' | 'sheet'
  undo: PastedBlock | null
  onUndo: () => void
  onRemove: (id: string) => void
  onSave: (id: string, text: string) => void
  onConvert: (block: PastedBlock) => void
  onDismissUndo: () => void
}) {
  const [openId, setOpenId] = useState<string | null>(null)
  const open = blocks.find((block) => block.id === openId) || null
  if (blocks.length === 0 && !undo) return null
  return (
    <div className="relative mb-2">
      {undo && (
        <div className="mb-1.5 flex items-center gap-2 text-[11px] text-muted" role="status">
          <span>Moved into the message.</span>
          <button type="button" onClick={onUndo} className="font-semibold text-accent hover:underline">Back to pill</button>
          <button type="button" onClick={onDismissUndo} className="text-muted/70 hover:text-ink" aria-label="Dismiss">
            <Close sx={{ fontSize: 12 }} />
          </button>
        </div>
      )}
      {blocks.length > 0 && (
        <div className="flex flex-wrap gap-1.5" aria-live="polite">
          {blocks.map((block, i) => (
            <PastedPill
              key={block.id}
              block={block}
              index={i + 1}
              warn={warn}
              onOpen={() => setOpenId(block.id)}
              onRemove={() => onRemove(block.id)}
            />
          ))}
        </div>
      )}
      {open && (
        <PastedBlockViewer
          block={open}
          presentation={presentation}
          onClose={() => setOpenId(null)}
          onSave={(text) => onSave(open.id, text)}
          onDelete={() => { onRemove(open.id); setOpenId(null) }}
          onConvert={() => { onConvert(open); setOpenId(null) }}
        />
      )}
    </div>
  )
}

export function CollapseHint({ onCollapse }: { onCollapse: () => void }) {
  return (
    <button
      type="button"
      onClick={onCollapse}
      className="mb-1.5 inline-flex items-center gap-1 text-[11px] text-muted hover:text-ink"
    >
      <UnfoldLess sx={{ fontSize: 13 }} />
      Long message. Collapse to pill
    </button>
  )
}
