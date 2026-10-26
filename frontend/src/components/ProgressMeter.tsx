import { useRef } from 'react'

/**
 * Shared checklist meter. Hidden when there is nothing to measure.
 * A non-zero fill has a minimum width so a 1-of-11 run is visible.
 * While a step is running a sheen travels across the fill. Steps that
 * need work are a separate segment. `hold` keeps the done width from
 * shrinking mid-run, which is what made a crashed plan build look like
 * it undid finished work.
 */
export function ProgressMeter({
  done,
  failed = 0,
  total,
  running = false,
  hold = false,
  holdKey = '',
  tone = 'accent',
}: {
  done: number
  failed?: number
  total: number
  running?: boolean
  hold?: boolean
  holdKey?: string
  tone?: 'accent' | 'success'
}) {
  const water = useRef({ key: holdKey, done: 0 })
  if (!hold) {
    water.current = { key: holdKey, done }
  } else if (water.current.key !== holdKey) {
    water.current = { key: holdKey, done }
  } else if (done > water.current.done) {
    water.current = { key: holdKey, done }
  }
  const shownDone = hold ? water.current.done : done
  if (!total) return null
  const donePct = Math.round((shownDone / total) * 100)
  const failPct = Math.round((Math.max(0, failed) / total) * 100)
  const doneWidth = shownDone > 0 ? Math.max(donePct, 4) : 0
  const complete = shownDone >= total && failed <= 0 && !running
  const fill = complete || tone === 'success'
    ? 'bg-gradient-to-r from-success/75 to-success'
    : 'bg-gradient-to-r from-accent/80 to-accent'
  const label = failed > 0
    ? `${shownDone} of ${total} done, ${failed} need work`
    : `${shownDone} of ${total} done`
  return (
    <div className="flex items-center gap-2.5 px-3 py-2">
      <div
        role="progressbar"
        aria-valuenow={Math.min(total, shownDone + Math.max(0, failed))}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuetext={label}
        aria-label={label}
        className="relative h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-overlay ring-1 ring-inset ring-border/80 shadow-[inset_0_1px_2px_rgba(0,0,0,0.35)]"
      >
        <div
          className={`absolute inset-y-0 left-0 overflow-hidden rounded-full transition-[width] duration-500 ease-out ${fill}`}
          style={{ width: `${doneWidth}%` }}
        >
          <span className="pointer-events-none absolute inset-x-0 top-0 h-1/2 bg-gradient-to-b from-white/30 to-transparent" />
          {running && <span className="meter-sweep" />}
        </div>
        {failPct > 0 && (
          <div
            className="absolute inset-y-0 overflow-hidden rounded-full bg-gradient-to-r from-danger/70 to-danger transition-[width,left] duration-500 ease-out"
            style={{ left: `${doneWidth}%`, width: `${Math.max(failPct, 4)}%` }}
          >
            <span className="pointer-events-none absolute inset-x-0 top-0 h-1/2 bg-gradient-to-b from-white/25 to-transparent" />
          </div>
        )}
      </div>
      <span className={`w-9 shrink-0 text-right text-[11px] font-medium tabular-nums ${
        complete ? 'text-success' : failPct > 0 ? 'text-danger' : 'text-muted'
      }`}>
        {shownDone}/{total}
      </span>
    </div>
  )
}
