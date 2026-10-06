import { useState } from 'react'
import { api } from '../api/client'

const REGIONS = [
  'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
  'eu-west-1', 'eu-west-2', 'eu-central-1',
  'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1',
]

const KEY_OK = /^(AKIA|ASIA)[A-Z0-9]{16}$/

export function AwsConnectModal({ open, onClose, onConnected }: {
  open: boolean
  onClose: () => void
  onConnected: () => void
}) {
  const [accessKeyId, setAccessKeyId] = useState('')
  const [secret, setSecret] = useState('')
  const [region, setRegion] = useState('us-east-1')
  const [sessionToken, setSessionToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!open) return null
  const temporary = accessKeyId.trim().startsWith('ASIA')
  const canSubmit = KEY_OK.test(accessKeyId.trim())
    && secret.trim().length >= 16
    && (!temporary || sessionToken.trim().length > 0)
    && !busy

  const submit = async () => {
    setBusy(true)
    setError('')
    try {
      await api.aws.connect({
        access_key_id: accessKeyId.trim(),
        secret_access_key: secret.trim(),
        region,
        session_token: temporary ? sessionToken.trim() : '',
      })
      setSecret('')
      setSessionToken('')
      onConnected()
      onClose()
    } catch (e: any) {
      setError(e?.message || 'Connection failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-xl border border-border bg-surface p-4 shadow-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="text-sm font-semibold text-ink">Connect AWS</div>
        <p className="mt-1 text-[12px] text-muted leading-snug">
          The access key is checked with AWS STS before it is saved. The secret is stored encrypted for this account only.
        </p>
        <label className="mt-3 block text-[11px] text-muted">Access key ID</label>
        <input className="input mt-1 font-mono" value={accessKeyId} onChange={(e) => setAccessKeyId(e.target.value)} placeholder="AKIA..." autoComplete="off" />
        <label className="mt-3 block text-[11px] text-muted">Secret access key</label>
        <input className="input mt-1 font-mono" type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="off" />
        <label className="mt-3 block text-[11px] text-muted">Region</label>
        <select className="input mt-1" value={region} onChange={(e) => setRegion(e.target.value)}>
          {REGIONS.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        {temporary && (
          <>
            <label className="mt-3 block text-[11px] text-muted">Session token</label>
            <input className="input mt-1 font-mono" type="password" value={sessionToken} onChange={(e) => setSessionToken(e.target.value)} autoComplete="off" />
          </>
        )}
        {error && <p className="mt-3 text-[12px] text-danger">{error}</p>}
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
          <button type="button" className="btn-primary" disabled={!canSubmit} onClick={() => { void submit() }}>
            {busy ? 'Checking…' : 'Connect'}
          </button>
        </div>
      </div>
    </div>
  )
}
