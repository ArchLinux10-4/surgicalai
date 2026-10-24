import { useEffect, useState } from 'react'
import { CheckCircle, ErrorOutline } from '@mui/icons-material'
import { api } from '../api/client'
import { AwsConnectModal } from './AwsConnectModal'

export function AwsTerminalPanel() {
  const [status, setStatus] = useState<any>(null)
  const [open, setOpen] = useState(false)

  const refresh = () => {
    api.aws.status().then(setStatus).catch(() => setStatus({ connected: false }))
  }
  useEffect(() => { refresh() }, [])

  const connected = !!status?.connected

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-3 py-2 border-b border-border text-[11px] text-muted">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 min-w-0">
            {status == null ? (
              <span className="h-4 w-4 rounded-full bg-overlay/70 animate-pulse shrink-0" />
            ) : (
              <span
                className={`inline-flex h-4 w-4 items-center justify-center rounded-full shrink-0 ${
                  connected ? 'bg-success/15 text-success' : 'bg-danger/15 text-danger'
                }`}
                title={connected ? 'Connected' : 'Not connected'}
              >
                {connected
                  ? <CheckCircle sx={{ fontSize: 12 }} />
                  : <ErrorOutline sx={{ fontSize: 12 }} />}
              </span>
            )}
            <span className="text-ink font-semibold">AWS CLI</span>
          </div>
          <button
            type="button"
            className="text-[11px] font-semibold text-accent hover:underline disabled:opacity-40"
            onClick={() => setOpen(true)}
            disabled={status == null}
          >
            Account
          </button>
        </div>
        <AwsConnectModal open={open} onClose={() => setOpen(false)} onConnected={refresh} />
      </div>
      {status?.error && !connected && (
        <p className="px-3 pt-2 text-[12px] text-danger leading-snug">{status.error}</p>
      )}
      <p className="px-3 py-2 text-[12px] text-muted leading-snug">
        {connected
          ? 'The terminal is the wide bar under the chat. Commands and output stay there.'
          : 'Open Account to connect an access key. Agents and the terminal use that account.'}
      </p>
    </div>
  )
}
