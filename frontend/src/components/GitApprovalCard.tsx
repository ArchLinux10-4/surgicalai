import { useEffect, useState } from 'react'

const PILLS: Record<string, { label: string; className: string; hint: string }> = {
  read: {
    label: 'Read',
    className: 'bg-success/15 text-success',
    hint: 'This only reads git state. It ran automatically.',
  },
  local: {
    label: 'Local',
    className: 'bg-accent/15 text-accent',
    hint: 'This changes the local branch or index. It does not push.',
  },
  remote: {
    label: 'Remote',
    className: 'bg-[#ff9900]/15 text-[#ff9900]',
    hint: 'This pushes a branch or opens a pull request. It runs after you approve it.',
  },
  merge: {
    label: 'Merge',
    className: 'bg-danger/15 text-danger',
    hint: 'This merges into the default branch. It runs only after you approve it.',
  },
}

export function GitLevelPill({ level }: { level?: string }) {
  const pill = level ? PILLS[level] : undefined
  if (!pill) return null
  return (
    <span
      title={pill.hint}
      aria-label={`${pill.label}: ${pill.hint}`}
      className={`inline-flex shrink-0 cursor-help items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${pill.className}`}
    >
      {pill.label}
    </span>
  )
}

export function GitApprovalCard({ command, level, title, summary, onDecide }: {
  command: string
  level?: string
  title?: string
  summary?: string
  onDecide: (action: 'approve' | 'reject' | 'edit', command: string) => void
}) {
  const [draft, setDraft] = useState(command)
  useEffect(() => { setDraft(command) }, [command])
  const changed = draft.trim() !== command.trim()
  const merge = level === 'merge'
  const approveClass = merge
    ? 'text-danger border-danger/40 hover:bg-danger/15'
    : 'text-[#ff9900] border-[#ff9900]/40 hover:bg-[#ff9900]/15'
  return (
    <div className="flex flex-wrap items-center gap-2 px-3 py-1.5 border-t border-white/10 bg-[#10140c]">
      <GitLevelPill level={level} />
      {title && (
        <span
          title={title}
          className={`shrink-0 max-w-[18rem] truncate text-[10px] font-semibold uppercase tracking-wide ${merge ? 'text-danger' : 'text-[#ff9900]'}`}
        >
          {title}
        </span>
      )}
      {merge ? (
        <span className="min-w-[12rem] flex-1 truncate font-mono text-[12px] text-[#d6d6d6]" title={command}>
          {command}
        </span>
      ) : (
        <input
          className="min-w-[12rem] flex-1 rounded border border-white/10 bg-black/40 px-2 py-1 font-mono text-[12px] text-[#d6d6d6] outline-none focus:border-white/30"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Git command to approve"
        />
      )}
      {summary && (
        <span className="w-full truncate text-[11px] text-muted" title={summary}>{summary}</span>
      )}
      <button
        type="button"
        className={`shrink-0 rounded border px-2 py-1 text-[11px] font-semibold disabled:opacity-40 ${approveClass}`}
        disabled={!draft.trim()}
        onClick={() => onDecide(merge || !changed ? 'approve' : 'edit', merge ? command : draft.trim())}
      >
        {merge || !changed ? 'Approve' : 'Run edit'}
      </button>
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
