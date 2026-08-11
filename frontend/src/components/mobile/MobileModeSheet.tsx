/**
 * MobileModeSheet — bottom sheet to pick Edit / Ask / Plan / Agent.
 * Pattern mirrors MenuDrawer / MobileGitHubSheet (backdrop, drag handle, large rows).
 * Mobile-only — never imported by desktop ChatPanel.
 */
import React, { useEffect, useRef } from 'react'
import {
  CHAT_MODES,
  MODE_COLOR,
  MODE_META,
  type ChatMode,
} from './chatMode'

interface Props {
  open: boolean
  current: ChatMode
  available: ChatMode[]
  onSelect: (m: ChatMode) => void
  onClose: () => void
}

export function MobileModeSheet({ open, current, available, onSelect, onClose }: Props) {
  const overlayRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const modes = CHAT_MODES.filter(m => available.includes(m))

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-[60] flex flex-col justify-end"
      onClick={e => { if (e.target === overlayRef.current) onClose() }}
    >
      <div className={`absolute inset-0 bg-black/50 transition-opacity ${open ? 'opacity-100' : 'opacity-0'}`} />
      <div
        className="relative bg-surface rounded-t-2xl border-t border-border shadow-modal
          animate-slide-up max-h-[70vh] flex flex-col"
      >
        <div className="flex justify-center pt-2 pb-1">
          <div className="w-10 h-1 rounded-full bg-border" />
        </div>
        <div className="px-5 pb-2 flex items-center justify-between">
          <h2 className="text-[15px] font-semibold text-ink">Chat mode</h2>
          <button
            type="button"
            onClick={onClose}
            className="w-9 h-9 flex items-center justify-center rounded-xl text-muted hover:bg-overlay/60"
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        <div className="overflow-y-auto pb-[max(12px,env(safe-area-inset-bottom))]">
          {modes.map(m => {
            const meta = MODE_META[m]
            const color = MODE_COLOR[m]
            const selected = current === m
            return (
              <button
                key={m}
                type="button"
                onClick={() => { onSelect(m); onClose() }}
                className={`w-full flex items-start gap-3 px-5 py-4 text-left border-t border-border/40
                  active:bg-overlay/50 transition-colors ${selected ? 'bg-overlay/30' : ''}`}
              >
                <span className={`mt-1.5 w-2 h-2 rounded-full flex-shrink-0 ${color.dot}`} />
                <span className="min-w-0 flex-1">
                  <span className={`block text-[14px] font-semibold ${selected ? color.text : 'text-ink'}`}>
                    {meta.label}
                  </span>
                  <span className="block text-[12px] text-muted mt-0.5 leading-snug">{meta.desc}</span>
                </span>
                {selected && <span className="text-accent text-[13px] font-semibold mt-0.5">✓</span>}
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}
