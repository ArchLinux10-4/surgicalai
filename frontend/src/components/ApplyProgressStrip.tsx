import React, { useEffect, useState } from 'react'
import type { ApplyProgress } from '../lib/applyProgress'
import { formatElapsed } from '../lib/applyProgress'

/** Inline progress strip shown while a large-file apply is in flight. */
export function ApplyProgressStrip({
  progress,
  startedAt,
  className = '',
}: {
  progress: ApplyProgress
  startedAt: number
  className?: string
}) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 250)
    return () => window.clearInterval(id)
  }, [])

  const elapsed = formatElapsed(now - startedAt)
  const pct = typeof progress.fraction === 'number'
    ? Math.max(0, Math.min(100, Math.round(progress.fraction * 100)))
    : null

  return (
    <div
      className={`rounded-lg border border-accent/25 bg-accent/5 px-3 py-2 ${className}`}
      role="status"
      aria-live="polite"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex items-center gap-2">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse flex-shrink-0" />
          <span className="text-[11px] font-medium text-ink truncate">{progress.detail}</span>
        </div>
        <span className="text-[10px] tabular-nums text-muted/80 flex-shrink-0">{elapsed}</span>
      </div>
      <div className="mt-1.5 h-1 rounded-full bg-overlay/70 overflow-hidden">
        {pct !== null ? (
          <div
            className="h-full bg-accent/70 transition-[width] duration-300 ease-out"
            style={{ width: `${pct}%` }}
          />
        ) : (
          <div className="h-full w-1/3 bg-accent/70 rounded-full animate-[sai-apply-indeterminate_1.2s_ease-in-out_infinite]" />
        )}
      </div>
      {pct !== null && (
        <div className="mt-1 text-[10px] text-muted/70 tabular-nums">{pct}%</div>
      )}
      <style>{`
        @keyframes sai-apply-indeterminate {
          0% { transform: translateX(-120%); }
          100% { transform: translateX(340%); }
        }
      `}</style>
    </div>
  )
}
