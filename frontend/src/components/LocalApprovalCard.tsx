import { useEffect, useState } from 'react'
import { canAlwaysAllowLocal } from '../lib/localAllow'

const PILLS: Record<string, { label: string; className: string }> = {
  read: { label: 'Read', className: 'bg-success/15 text-success' },
  change: { label: 'Change', className: 'bg-accent/15 text-accent' },
  network: { label: 'Network', className: 'bg-[#ff9900]/15 text-[#ff9900]' },
  delete: { label: 'Delete', className: 'bg-danger/15 text-danger' },
  protected: { label: 'Protected', className: 'bg-danger/15 text-danger' },
}

export function LocalLevelPill({ level }: { level?: string }) {
  const pill = level ? PILLS[level] : undefined
  if (!pill) return null
  return (
    <span className={`inline-flex shrink-0 items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${pill.className}`}>
      {pill.label}
    </span>
  )
}

export function LocalApprovalCard({
  command, level, title, summary, cwd, onDecide,
}: {
  command: string
  level?: string
  title?: string
  summary?: string
  cwd?: string
  onDecide: (action: 'approve' | 'reject' | 'edit', command: string, always?: boolean) => void
}) {
  const [draft, setDraft] = useState(command)
  useEffect(() => { setDraft(command) }, [command])
  const changed = draft.trim() !== command.trim()
  const allowAlways = canAlwaysAllowLocal(level, command) && !changed
  return (
    <div className="flex flex-wrap items-center gap-2 px-3 py-1.5 border-t border-white/10 bg-[#10140c]">
      <LocalLevelPill level={level} />
      {title && (
        <span className="shrink-0 max-w-[18rem] truncate text-[10px] font-semibold uppercase tracking-wide text-[#ff9900]">
          {title}
        </span>
      )}
      {cwd && <span className="w-full truncate font-mono text-[10px] text-muted">{cwd}</span>}
      <input
        className="min-w-[12rem] flex-1 rounded border border-white/10 bg-black/40 px-2 py-1 font-mono text-[12px] text-[#d6d6d6] outline-none focus:border-white/30"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        aria-label="Local command to approve"
      />
      {summary && (
        <pre className="w-full max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-muted">{summary}</pre>
      )}
      <button
        type="button"
        className="shrink-0 rounded border border-[#ff9900]/40 px-2 py-1 text-[11px] font-semibold text-[#ff9900] hover:bg-[#ff9900]/15 disabled:opacity-40"
        disabled={!draft.trim()}
        onClick={() => onDecide(changed ? 'edit' : 'approve', changed ? draft.trim() : command)}
      >
        {changed ? 'Run edit' : 'Allow once'}
      </button>
      {allowAlways && (
        <button
          type="button"
          className="shrink-0 rounded border border-white/15 px-2 py-1 text-[11px] font-semibold text-ink hover:bg-white/5"
          onClick={() => onDecide('approve', command, true)}
        >
          Always allow
        </button>
      )}
      <button
        type="button"
        className="shrink-0 rounded border border-white/15 px-2 py-1 text-[11px] font-semibold text-[#d6d6d6] hover:bg-white/5"
        onClick={() => onDecide('reject', command)}
      >
        Reject
      </button>
    </div>
  )
}
