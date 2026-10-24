import { useEffect, useState } from 'react'

const PILLS: Record<string, { label: string; className: string; hint: string }> = {
  on_task: {
    label: 'On task',
    className: 'bg-success/15 text-success',
    hint: 'Read-only lookup for what you asked. It ran automatically and changed nothing.',
  },
  side: {
    label: 'Side lookup',
    className: 'bg-accent/15 text-accent',
    hint: 'Read-only lookup the agent made for extra context, such as checking which account is connected. It ran automatically and changed nothing.',
  },
  change: {
    label: 'Change',
    className: 'bg-[#ff9900]/15 text-[#ff9900]',
    hint: 'This command creates or modifies something in your AWS account. It only runs after you approve it.',
  },
  deletes: {
    label: 'Deletes',
    className: 'bg-danger/15 text-danger',
    hint: 'This command removes data or resources and may not be reversible. It only runs after you approve it.',
  },
  sensitive: {
    label: 'Secret',
    className: 'bg-danger/15 text-danger',
    hint: 'This command can read a secret, key, or credential. It only runs after you approve it.',
  },
}

export function AwsLevelPill({ level }: { level?: string }) {
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

export function AwsApprovalCard({ command, level, title, onDecide }: {
  command: string
  level?: string
  title?: string
  target?: string
  onDecide: (action: 'approve' | 'reject' | 'edit', command: string) => void
}) {
  const [draft, setDraft] = useState(command)
  useEffect(() => { setDraft(command) }, [command])
  const changed = draft.trim() !== command.trim()
  const danger = level === 'deletes' || level === 'sensitive'
  const approveClass = danger
    ? 'text-danger border-danger/40 hover:bg-danger/15'
    : 'text-[#ff9900] border-[#ff9900]/40 hover:bg-[#ff9900]/15'
  const multiline = draft.includes('\n')
  return (
    <div className="flex flex-wrap items-center gap-2 px-3 py-1.5 border-t border-white/10 bg-[#160c0c]">
      <AwsLevelPill level={level} />
      {title && (
        <span
          title={title}
          className={`shrink-0 max-w-[16rem] truncate text-[10px] font-semibold uppercase tracking-wide ${danger ? 'text-danger' : 'text-[#ff9900]'}`}
        >
          {title}
        </span>
      )}
      {multiline ? (
        <textarea
          rows={Math.min(3, draft.split('\n').length)}
          className="min-w-[12rem] flex-1 resize-none rounded border border-white/10 bg-black/40 px-2 py-1 font-mono text-[12px] text-[#d6d6d6] outline-none focus:border-white/30"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Command to approve"
        />
      ) : (
        <input
          className="min-w-[12rem] flex-1 rounded border border-white/10 bg-black/40 px-2 py-1 font-mono text-[12px] text-[#d6d6d6] outline-none focus:border-white/30"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Command to approve"
        />
      )}
      <button
        type="button"
        className={`shrink-0 rounded border px-2 py-1 text-[11px] font-semibold disabled:opacity-40 ${approveClass}`}
        disabled={!draft.trim()}
        onClick={() => onDecide(changed ? 'edit' : 'approve', draft.trim())}
      >
        {changed ? 'Run edit' : 'Approve'}
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
