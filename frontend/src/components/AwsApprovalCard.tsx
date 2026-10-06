import { useState } from 'react'

export function AwsApprovalCard({ command, onDecide }: {
  command: string
  onDecide: (action: 'approve' | 'reject' | 'edit', command: string) => void
}) {
  const [draft, setDraft] = useState(command)
  const changed = draft.trim() !== command.trim()
  return (
    <div className="mx-3 mb-2 rounded-xl border border-[#ff9900]/50 bg-surface/80 p-3">
      <div className="text-[11px] font-bold uppercase tracking-wide text-[#ff9900]">AWS command needs approval</div>
      <p className="mt-1 text-[12px] text-muted">The agent wants to run this. Approve it, change it, or reject it.</p>
      <input
        className="input mt-2 font-mono text-[12px]"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          className="text-[12px] font-semibold px-3 py-1.5 rounded-lg bg-[#ff9900]/15 text-[#ff9900] border border-[#ff9900]/40"
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
