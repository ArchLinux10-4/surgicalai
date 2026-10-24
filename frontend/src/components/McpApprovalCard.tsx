import { useState } from 'react'
import { useAppStore } from '../stores/appStore'

export function McpApprovalCard() {
  const activeSessions = useAppStore(s => s.activeSessions)
  const approval = useAppStore(s => s.mcpApproval)
  const setApproval = useAppStore(s => s.setMcpApproval)
  const calls = useAppStore(s => s.mcpCalls)
  const pending = approval && (!activeSessions || approval.sessionId === activeSessions) ? approval : null
  const visibleCalls = calls.filter(c => !activeSessions || c.sessionId === activeSessions).slice(-4)
  const [argsOpen, setArgsOpen] = useState(false)
  const [openCall, setOpenCall] = useState<number | null>(null)

  if (!pending && visibleCalls.length === 0) return null

  const decide = (action: 'approve' | 'reject') => {
    if (!pending) return
    if (!pending.reply({ action })) return
    setApproval(null)
    setArgsOpen(false)
  }

  return (
    <div className="border-t border-border bg-surface px-4 py-3 text-sm">
      {pending && (
        <div className="rounded-lg border border-border bg-base p-3">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="font-medium text-ink">MCP tool needs approval</div>
              <div className="mt-1 text-xs text-muted">
                {pending.serverName || pending.server}.{pending.tool}
              </div>
            </div>
            <button
              type="button"
              className="text-xs text-muted hover:text-ink"
              onClick={() => setArgsOpen(v => !v)}
            >
              {argsOpen ? 'Hide arguments' : 'Show arguments'}
            </button>
          </div>
          {argsOpen && (
            <pre className="mt-2 max-h-40 overflow-auto rounded bg-surface p-2 text-xs text-ink">
              {JSON.stringify(pending.arguments || {}, null, 2)}
            </pre>
          )}
          <div className="mt-3 flex gap-2">
            <button type="button" className="btn-primary px-3 py-1 text-xs" onClick={() => decide('approve')}>
              Approve
            </button>
            <button type="button" className="btn-ghost border border-border px-3 py-1 text-xs" onClick={() => decide('reject')}>
              Reject
            </button>
          </div>
        </div>
      )}
      {visibleCalls.length > 0 && (
        <div className={pending ? 'mt-2 space-y-1' : 'space-y-1'}>
          {visibleCalls.map((call, i) => (
            <button
              key={`${call.server}-${call.tool}-${i}`}
              type="button"
              className="block w-full rounded border border-border px-2 py-1 text-left text-xs"
              onClick={() => setOpenCall(openCall === i ? null : i)}
            >
              <span className={call.ok ? 'text-ink' : 'text-red-400'}>
                {call.ok ? 'MCP' : 'MCP failed'} {call.server}.{call.tool}
              </span>
              {openCall === i && (
                <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap text-muted">
                  {call.ok ? (call.text || '(empty)') : (call.error || 'The call failed.')}
                </pre>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
