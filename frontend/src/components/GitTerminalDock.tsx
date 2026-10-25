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
  const gitSteps = useAppStore(s => s.gitSteps)
  const gitResult = useAppStore(s => s.gitResult)
  const setGitFollowup = useAppStore(s => s.setGitFollowup)
  const gitApproval = useAppStore(s => s.gitApproval)
  const setGitApproval = useAppStore(s => s.setGitApproval)
  const visible = lines.filter(l => !activeSessions || l.sessionId === activeSessions)
  const steps = gitSteps.filter(step => !activeSessions || step.sessionId === activeSessions)
  const result = gitResult && (!activeSessions || gitResult.sessionId === activeSessions) ? gitResult : null
  const approval = gitApproval && (!activeSessions || gitApproval.sessionId === activeSessions)
    ? gitApproval
    : null
  const [open, setOpen] = useState(true)
  const latest = visible[visible.length - 1]

  useEffect(() => {
    if (approval || visible.length || steps.length || result) setOpen(true)
  }, [approval, visible.length, steps.length, result])

  if (!approval && !latest && !steps.length && !result) return null

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
      {open && steps.length > 0 && (
        <ul className="space-y-0.5 px-3 pb-2 text-[11px]">
          {steps.map((step, i) => (
            <li key={`${step.name}-${i}`} className={step.ok ? 'text-success' : 'text-danger'}>
              {step.ok ? '✓' : '✗'} {step.name}. {step.detail.split('\n')[0]}
            </li>
          ))}
        </ul>
      )}
      {open && result && (
        <div className="flex flex-wrap items-center gap-2 px-3 pb-2 text-[11px]">
          <span className={result.ok ? 'text-success' : 'text-danger'}>
            {result.ok
              ? (result.pr_url || result.verified.length ? 'Verified on GitHub' : 'Git finished')
              : 'Git did not finish'}
            {result.branch ? ` · ${result.branch}` : ''}
            {result.commit ? ` · ${result.commit.slice(0, 7)}` : ''}
          </span>
          {result.pr_url && (
            <a className="text-accent underline" href={result.pr_url} target="_blank" rel="noreferrer">
              Open pull request
            </a>
          )}
          {result.fix_url && !result.ok && (
            <a className="text-[#ff9900] underline" href={result.fix_url} target="_blank" rel="noreferrer">
              Fix GitHub App access
            </a>
          )}
          {!result.ok && (
            <button
              type="button"
              className="rounded border border-white/15 px-2 py-0.5 text-ink"
              onClick={() => setGitFollowup('continue publishing')}
            >
              Retry
            </button>
          )}
          {result.next && <span className="w-full text-muted">{result.next.split('\n')[0]}</span>}
        </div>
      )}
      {open && latest && !steps.length && (
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
