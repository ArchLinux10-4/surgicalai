import { useEffect, useRef, useState } from 'react'
import { Check, ContentCopy } from '@mui/icons-material'
import { api } from '../api/client'

export function shortAwsCli(version?: string): string {
  if (!version) return ''
  const match = version.match(/aws-cli\/[0-9.]+/)
  return match ? match[0] : version.split(/\s+/)[0]
}

const REGIONS = [
  'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
  'eu-west-1', 'eu-west-2', 'eu-central-1',
  'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1',
]

const KEY_OK = /^(AKIA|ASIA)[A-Z0-9]{16}$/
const REGION_OK = /^[a-z]{2}(-[a-z]+)+-\d$/

type Phase = 'loading' | 'form' | 'success' | 'account' | 'confirm' | 'disconnected'

type AwsStatus = {
  connected?: boolean
  account?: string
  arn?: string
  user_id?: string
  region?: string
  access_key_id?: string
  cli_version?: string
  error?: string
}

function Detail({ label, value, title, copyable }: { label: string; value?: string; title?: string; copyable?: boolean }) {
  const [copied, setCopied] = useState(false)
  if (!value) return null
  const copy = () => {
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }).catch(() => {})
  }
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5 border-b border-border/60 last:border-0">
      <span className="shrink-0 text-[11px] text-muted">{label}</span>
      <span className="flex min-w-0 items-baseline justify-end gap-1.5">
        <span title={title || value} className="min-w-0 text-right font-mono text-[12px] text-ink break-all">{value}</span>
        {copyable && (
          <button
            type="button"
            onClick={copy}
            className="shrink-0 rounded p-0.5 text-muted hover:text-ink hover:bg-overlay/60 transition-colors"
            title={copied ? 'Copied' : `Copy ${label.toLowerCase()}`}
            aria-label={copied ? `Copied ${label.toLowerCase()}` : `Copy ${label.toLowerCase()}`}
          >
            {copied ? <Check sx={{ fontSize: 13 }} className="text-success" /> : <ContentCopy sx={{ fontSize: 13 }} />}
          </button>
        )}
      </span>
    </div>
  )
}

function AccountDetails({ status }: { status: AwsStatus }) {
  return (
    <div className="mt-3 rounded-lg border border-border bg-base/40 px-3">
      <Detail label="Account" value={status.account} />
      <Detail label="User" value={status.user_id} />
      <Detail label="ARN" value={status.arn} />
      <Detail label="Region" value={status.region} />
      <Detail label="Access key" value={status.access_key_id} copyable />
      <Detail
        label="CLI"
        value={shortAwsCli(status.cli_version) || 'Not installed'}
        title={status.cli_version || 'The AWS CLI is not installed on the server'}
      />
    </div>
  )
}

export function AwsConnectModal({ open, onClose, onConnected }: {
  open: boolean
  onClose: () => void
  onConnected: () => void
}) {
  const [phase, setPhase] = useState<Phase>('loading')
  const [status, setStatus] = useState<AwsStatus | null>(null)
  const [accessKeyId, setAccessKeyId] = useState('')
  const [secret, setSecret] = useState('')
  const [region, setRegion] = useState('us-east-1')
  const [sessionToken, setSessionToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [touched, setTouched] = useState(false)
  const keyRef = useRef<HTMLInputElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    setError('')
    setTouched(false)
    setPhase('loading')
    api.aws.status()
      .then((s) => {
        setStatus(s)
        setPhase(s?.connected ? 'account' : 'form')
        if (s?.region) setRegion(s.region)
      })
      .catch((e: any) => {
        setStatus(null)
        setPhase('form')
        setError(e?.message || 'Could not check the AWS connection.')
      })
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, busy, onClose])

  useEffect(() => {
    if (!open) return
    if (phase === 'form') keyRef.current?.focus()
    else dialogRef.current?.focus()
  }, [open, phase])

  if (!open) return null

  const temporary = accessKeyId.trim().startsWith('ASIA')
  const keyInvalid = accessKeyId.trim().length > 0 && !KEY_OK.test(accessKeyId.trim())
  const secretInvalid = secret.length > 0 && secret.trim().length < 16
  const regionInvalid = region.trim().length > 0 && !REGION_OK.test(region.trim())
  const tokenMissing = temporary && sessionToken.trim().length === 0
  const canSubmit = KEY_OK.test(accessKeyId.trim())
    && secret.trim().length >= 16
    && REGION_OK.test(region.trim())
    && !tokenMissing
    && !busy

  const close = () => {
    if (busy) return
    onClose()
  }

  const submit = async () => {
    setTouched(true)
    if (!canSubmit) return
    setBusy(true)
    setError('')
    try {
      const saved = await api.aws.connect({
        access_key_id: accessKeyId.trim(),
        secret_access_key: secret.trim(),
        region: region.trim(),
        session_token: temporary ? sessionToken.trim() : '',
      })
      setSecret('')
      setSessionToken('')
      setAccessKeyId('')
      setStatus(saved)
      setPhase('success')
      onConnected()
    } catch (e: any) {
      setError(e?.message || 'AWS did not accept these details.')
    } finally {
      setBusy(false)
    }
  }

  const disconnect = async () => {
    setBusy(true)
    setError('')
    try {
      await api.aws.disconnect()
      setStatus(null)
      setPhase('disconnected')
      onConnected()
    } catch (e: any) {
      setError(e?.message || 'Could not disconnect.')
      setPhase('account')
    } finally {
      setBusy(false)
    }
  }

  const title = phase === 'confirm'
    ? 'Disconnect AWS'
    : phase === 'disconnected'
      ? 'Disconnected'
      : phase === 'success'
        ? 'Connected'
        : phase === 'account'
          ? 'AWS account'
          : 'Connect AWS'

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4" onClick={close}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="w-full max-w-md rounded-xl border border-border bg-surface p-4 shadow-modal outline-none"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="aws-account-title"
      >
        <div id="aws-account-title" className="text-sm font-semibold text-ink">{title}</div>

        {phase === 'loading' && (
          <p className="mt-3 text-[12px] text-muted">Checking the connection…</p>
        )}

        {phase === 'form' && (
          <>
            <p className="mt-1 text-[12px] text-muted leading-snug">
              The key is checked with AWS before it is saved. The secret stays encrypted on this account.
            </p>
            {status?.cli_version ? (
              <p className="mt-2 text-[11px] font-mono text-muted truncate" title={status.cli_version}>
                CLI {shortAwsCli(status.cli_version)}
              </p>
            ) : status && !status.cli_version ? (
              <p className="mt-2 text-[11px] text-danger">
                The AWS CLI is not installed on the server. A key can be saved, but commands cannot run.
              </p>
            ) : null}
            <form onSubmit={(e) => { e.preventDefault(); void submit() }}>
            <label htmlFor="aws-access-key" className="mt-3 block text-[11px] text-muted">Access key ID</label>
            <input
              id="aws-access-key"
              ref={keyRef}
              className="input mt-1 font-mono"
              value={accessKeyId}
              onChange={(e) => setAccessKeyId(e.target.value.toUpperCase())}
              placeholder="AKIA..."
              autoComplete="off"
              spellCheck={false}
            />
            {(touched || keyInvalid) && keyInvalid && (
              <p className="mt-1 text-[11px] text-danger">Use a 20-character key starting with AKIA or ASIA.</p>
            )}
            <label htmlFor="aws-secret" className="mt-3 block text-[11px] text-muted">Secret access key</label>
            <input
              id="aws-secret"
              className="input mt-1 font-mono"
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              autoComplete="off"
            />
            {(touched || secretInvalid) && secretInvalid && (
              <p className="mt-1 text-[11px] text-danger">Secret access key is too short.</p>
            )}
            <label htmlFor="aws-region" className="mt-3 block text-[11px] text-muted">Region</label>
            <input
              id="aws-region"
              className="input mt-1 font-mono"
              value={region}
              onChange={(e) => setRegion(e.target.value.trim().toLowerCase())}
              placeholder="us-east-1"
              list="aws-regions"
              spellCheck={false}
              autoComplete="off"
            />
            <datalist id="aws-regions">
              {REGIONS.map((r) => <option key={r} value={r} />)}
            </datalist>
            {(touched || regionInvalid) && regionInvalid && (
              <p className="mt-1 text-[11px] text-danger">Region should look like us-east-1.</p>
            )}
            {temporary && (
              <>
                <label htmlFor="aws-session-token" className="mt-3 block text-[11px] text-muted">Session token</label>
                <input
                  id="aws-session-token"
                  className="input mt-1 font-mono"
                  type="password"
                  value={sessionToken}
                  onChange={(e) => setSessionToken(e.target.value)}
                  autoComplete="off"
                />
                {touched && tokenMissing && (
                  <p className="mt-1 text-[11px] text-danger">Temporary keys need a session token.</p>
                )}
              </>
            )}
            {status?.error && !error && (
              <p className="mt-3 text-[12px] text-danger">{status.error}</p>
            )}
            {error && <p className="mt-3 text-[12px] text-danger">{error}</p>}
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" className="btn-ghost" onClick={close} disabled={busy}>Cancel</button>
              <button type="submit" className="btn-primary" disabled={!canSubmit}>
                {busy ? 'Checking…' : 'Connect'}
              </button>
            </div>
            </form>
          </>
        )}

        {phase === 'success' && status && (
          <>
            <p className="mt-1 text-[12px] text-success">AWS accepted this key. The account is connected.</p>
            <AccountDetails status={status} />
            <div className="mt-4 flex justify-end">
              <button type="button" className="btn-primary" onClick={close}>Done</button>
            </div>
          </>
        )}

        {phase === 'account' && status && (
          <>
            <p className="mt-1 text-[12px] text-muted">This account is used by the terminal and by agents.</p>
            <AccountDetails status={status} />
            {error && <p className="mt-3 text-[12px] text-danger">{error}</p>}
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" className="btn-ghost" onClick={close}>Close</button>
              <button type="button" className="btn-danger" onClick={() => { setError(''); setPhase('confirm') }}>
                Disconnect
              </button>
            </div>
          </>
        )}

        {phase === 'confirm' && (
          <>
            <p className="mt-2 text-[12px] text-muted leading-snug">
              Disconnect {status?.account ? `account ${status.account}` : 'this account'}?
              Agents will no longer be able to run AWS commands with it.
            </p>
            {error && <p className="mt-3 text-[12px] text-danger">{error}</p>}
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" className="btn-ghost" disabled={busy} onClick={() => setPhase('account')}>
                Cancel
              </button>
              <button type="button" className="btn-danger" disabled={busy} onClick={() => { void disconnect() }}>
                {busy ? 'Disconnecting…' : 'Disconnect'}
              </button>
            </div>
          </>
        )}

        {phase === 'disconnected' && (
          <>
            <p className="mt-2 text-[12px] text-ink leading-snug">
              Disconnected. The saved key is gone, and agents can no longer use this account.
            </p>
            <div className="mt-4 flex justify-end">
              <button type="button" className="btn-primary" onClick={close}>Done</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
