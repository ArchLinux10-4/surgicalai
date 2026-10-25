import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import { GitApprovalCard, GitLevelPill } from './GitApprovalCard'

export function refreshRepoBranch(sessionId: string) {
  const state = useAppStore.getState()
  const repoId = state.sessions.find(s => s.id === sessionId)?.repository_id
  if (!repoId) return
  api.repositories.status(repoId).then((st) => {
    const cur = useAppStore.getState()
    cur.setRepositories(cur.repositories.map(r => (
      r.id === repoId ? { ...r, branch: st.branch, dirty: st.dirty } : r
    )))
  }).catch(() => {})
}

export function GitTerminalDock({ compact = false }: { compact?: boolean }) {
  const activeSessions = useAppStore(s => s.activeSessions)
  const lines = useAppStore(s => s.gitTerminalLines)
  const gitApproval = useAppStore(s => s.gitApproval)
  const setGitApproval = useAppStore(s => s.setGitApproval)
  const visible = lines.filter(l => !activeSessions || l.sessionId === activeSessions)
  const approval = gitApproval && (!activeSessions || gitApproval.sessionId === activeSessions)
    ? gitApproval
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
        <span className="font-semibold text-ink">Git</span>
        {latest && <GitLevelPill level={latest.level} />}
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
        <GitApprovalCard
          command={approval.command}
          level={approval.level}
          title={approval.title}
          summary={approval.summary}
          onDecide={(action, command) => {
            approval.reply({ action, command })
            setGitApproval(null)
          }}
        />
      )}
    </div>
  )
}
