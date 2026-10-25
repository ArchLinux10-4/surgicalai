import { useEffect, useState } from 'react'
import { useAppStore } from '../stores/appStore'
import { LocalApprovalCard, LocalLevelPill } from './LocalApprovalCard'

export function LocalTerminalDock({ compact = false }: { compact?: boolean }) {
  const activeSessions = useAppStore(s => s.activeSessions)
  const lines = useAppStore(s => s.localTerminalLines)
  const localApproval = useAppStore(s => s.localApproval)
  const setLocalApproval = useAppStore(s => s.setLocalApproval)
  const visible = lines.filter(l => !activeSessions || l.sessionId === activeSessions)
  const approval = localApproval && (!activeSessions || localApproval.sessionId === activeSessions)
    ? localApproval
    : null
  const [open, setOpen] = useState(true)
  const latest = visible[visible.length - 1]

  useEffect(() => {
    if (approval || visible.length) setOpen(true)
  }, [approval, visible.length])

  if (!approval && !latest) return null

  return (
    <div className={`border-t border-white/10 bg-[#0c1014] ${compact ? '' : ''}`}>
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-1 text-left text-[11px] text-muted"
        onClick={() => setOpen(v => !v)}
      >
        <span>{open ? '▾' : '▸'}</span>
        <span className="font-semibold text-ink">This Mac</span>
        {latest && <LocalLevelPill level={latest.level} />}
        {latest && (
          <span className="truncate font-mono text-[11px] text-[#d6d6d6]">
            {latest.command} {latest.exit_code === 0 ? '' : `(exit ${latest.exit_code})`}
          </span>
        )}
      </button>
      {open && latest && (
        <pre className="max-h-28 overflow-auto px-3 pb-2 font-mono text-[11px] text-[#c8c8c8] whitespace-pre-wrap">
          {(latest.stdout || latest.stderr || '').slice(0, 2000)}
        </pre>
      )}
      {approval && (
        <LocalApprovalCard
          command={approval.command}
          level={approval.level}
          title={approval.title}
          summary={approval.summary}
          cwd={approval.cwd}
          onDecide={(action, command, always) => {
            approval.reply({ action, command, always })
            setLocalApproval(null)
          }}
        />
      )}
    </div>
  )
}
