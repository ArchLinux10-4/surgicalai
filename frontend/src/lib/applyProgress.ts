/** Shared apply-progress helpers for large-file apply UX. */

export type ApplyProgressStage =
  | 'reading'
  | 'applying'
  | 'saving'
  | 'syncing'
  | 'marking'
  | 'finishing'

export interface ApplyProgress {
  stage: ApplyProgressStage
  /** Short button label, e.g. "Applying…" */
  label: string
  /** Longer strip detail, e.g. "Applying 3 changes to index.html" */
  detail: string
  /** Optional determinate fraction 0–1 when we know file i/N. */
  fraction?: number
}

export function formatElapsed(ms: number): string {
  const sec = Math.max(0, Math.floor(ms / 1000))
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}m ${s.toString().padStart(2, '0')}s`
}

export function applyStageLabel(stage: ApplyProgressStage): string {
  switch (stage) {
    case 'reading': return 'Reading…'
    case 'applying': return 'Applying…'
    case 'saving': return 'Saving…'
    case 'syncing': return 'Syncing…'
    case 'marking': return 'Recording…'
    case 'finishing': return 'Finishing…'
  }
}
