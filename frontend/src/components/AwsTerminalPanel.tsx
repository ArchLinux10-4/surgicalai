import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { AwsConnectModal, shortAwsCli } from './AwsConnectModal'

export function AwsTerminalPanel() {
  const [status, setStatus] = useState<any>(null)
  const [open, setOpen] = useState(false)

  const refresh = () => {
    api.aws.status().then(setStatus).catch(() => setStatus({ connected: false }))
  }
  useEffect(() => { refresh() }, [])

  if (!status?.connected) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 px-5 text-center">
        <div className="text-sm font-semibold text-ink">AWS CLI</div>
        <p className="text-[12px] text-muted leading-snug">Connect an access key to run AWS CLI commands and let agents use the same account.</p>
        {status?.error && <p className="text-[12px] text-danger">{status.error}</p>}
        <button type="button" className="btn-primary" onClick={() => setOpen(true)}>Connect AWS</button>
        <AwsConnectModal open={open} onClose={() => setOpen(false)} onConnected={refresh} />
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-3 py-2 border-b border-border text-[11px] text-muted">
        <div className="flex items-center justify-between gap-2">
          <div className="text-ink font-semibold">AWS CLI</div>
          <button
            type="button"
            className="text-[11px] font-semibold text-accent hover:underline"
            onClick={() => setOpen(true)}
          >
            Account
          </button>
        </div>
        <div className="truncate">Account {status.account} · {status.region}</div>
        <div className="truncate">{status.arn}</div>
        {status.cli_version && (
          <div className="truncate" title={status.cli_version}>{shortAwsCli(status.cli_version)}</div>
        )}
        <AwsConnectModal open={open} onClose={() => setOpen(false)} onConnected={refresh} />
      </div>
      <p className="px-3 py-2 text-[12px] text-muted leading-snug">
        The terminal is the wide bar under the chat. Commands and output stay there.
      </p>
    </div>
  )
}
