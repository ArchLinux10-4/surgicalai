import { useEffect, useState } from 'react'

const PILLS: Record<string, { label: string; className: string }> = {
  on_task: { label: 'On task', className: 'bg-success/15 text-success' },
  side: { label: 'Side lookup', className: 'bg-accent/15 text-accent' },
  change: { label: 'Change', className: 'bg-[#ff9900]/15 text-[#ff9900]' },
  deletes: { label: 'Deletes', className: 'bg-danger/15 text-danger' },
  sensitive: { label: 'Secret', className: 'bg-danger/15 text-danger' },
}

export function AwsLevelPill({ level }: { level?: string }) {
  const pill = level ? PILLS[level] : undefined
  if (!pill) return null
  return (
    <span className={`inline-flex shrink-0 items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${pill.className}`}>
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
  const border = danger ? 'border-danger/50' : 'border-[#ff9900]/50'
  const headingColor = danger ? 'text-danger' : 'text-[#ff9900]'
  const approveClass = danger
    ? 'bg-danger/15 text-danger border border-danger/40'
    : 'bg-[#ff9900]/15 text-[#ff9900] border border-[#ff9900]/40'
  return (
    <div className={`mx-3 mb-2 rounded-xl border ${border} bg-surface/80 p-3`}>
      <div className="flex items-center gap-2">
        <AwsLevelPill level={level} />
        <div className={`text-[11px] font-bold uppercase tracking-wide ${headingColor}`}>
          {title || 'AWS command needs approval'}
        </div>
      </div>
      <p className="mt-1 text-[12px] text-muted">The agent wants to run this. Approve it, change it, or reject it.</p>
      <textarea
        className="input mt-2 w-full min-h-[4.5rem] resize-y font-mono text-[12px] whitespace-pre-wrap"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          className={`text-[12px] font-semibold px-3 py-1.5 rounded-lg ${approveClass}`}
          onClick={() => onDecide(changed ? 'edit' : 'approve', draft.trim())}
        >
          {changed ? 'Run changed command' : 'Approve'}
        </button>
        <button
          type="button"
          className="text-[12px] font-semibold px-3 py-1.5 rounded-lg text-danger border border-danger/30"
          onClick={() => onDecide('reject', command)}
        >
          Reject
        </button>
      </div>
    </div>
  )
}
