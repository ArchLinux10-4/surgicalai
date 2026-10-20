import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import { AwsConnectModal } from './AwsConnectModal'

/** Join pasted shell continuations. Mirrors backend normalize_aws_command. */
function normalizeAwsCommand(command: string): string {
  const text = command.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  let out = ''
  let quote = ''
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (quote) {
      out += ch
      if (ch === quote) quote = ''
      continue
    }
    if (ch === "'" || ch === '"') {
      quote = ch
      out += ch
      continue
    }
    if (ch === '\\') {
      const nxt = i + 1 < text.length ? text[i + 1] : ''
      if (nxt === '\n' || nxt === '' || nxt === ' ' || nxt === '\t') {
        i += 1
        while (i < text.length && (text[i] === ' ' || text[i] === '\t' || text[i] === '\n')) i += 1
        i -= 1
        out += ' '
        continue
      }
    }
    out += ch
  }
  return out.trim()
}

export function AwsTerminalPanel() {
  const activeSessions = useAppStore(s => s.activeSessions)
  const lines = useAppStore(s => s.awsTerminalLines)
  const push = useAppStore(s => s.pushAwsTerminalLine)
  const [status, setStatus] = useState<any>(null)
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = () => {
    api.aws.status().then(setStatus).catch(() => setStatus({ connected: false }))
  }
  useEffect(() => { refresh() }, [])

  const visible = lines.filter(l => !activeSessions || l.sessionId === activeSessions)

  const run = async () => {
    const typed = normalizeAwsCommand(draft)
    if (!typed || busy) return
    const command = typed.startsWith('aws ') ? typed : `aws ${typed}`
    setDraft('')
    setBusy(true)
    try {
      const out = await api.aws.exec(command)
      push({
        sessionId: activeSessions || '',
        command,
        stdout: out.stdout || '',
        stderr: out.stderr || '',
        exit_code: out.exit_code ?? 1,
      })
    } catch (e: any) {
      push({
        sessionId: activeSessions || '',
        command,
        stdout: '',
        stderr: e?.message || 'Command failed',
        exit_code: 1,
      })
    } finally {
      setBusy(false)
    }
  }

  if (!status?.connected) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 px-5 text-center">
        <div className="text-sm font-semibold text-ink">AWS CLI</div>
        <p className="text-[12px] text-muted leading-snug">Connect an access key to run AWS CLI commands and let agents use the same account.</p>
        <button type="button" className="btn-primary" onClick={() => setOpen(true)}>Connect AWS</button>
        <AwsConnectModal open={open} onClose={() => setOpen(false)} onConnected={refresh} />
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-3 py-2 border-b border-border text-[11px] text-muted">
        <div className="text-ink font-semibold">AWS CLI</div>
        <div className="truncate">Account {status.account} · {status.region}</div>
        <div className="truncate">{status.arn}</div>
        <button
          type="button"
          className="mt-1 text-danger"
          onClick={() => { api.aws.disconnect().then(refresh).catch(() => {}) }}
        >
          Disconnect
        </button>
      </div>
      <div className="flex-1 overflow-y-auto bg-[#100808] px-3 py-2 font-mono text-[11px] text-[#d6d6d6]">
        {visible.length === 0 && <div className="text-faint">aws&gt;</div>}
        {visible.map((line, i) => (
          <div key={i} className="mb-2">
            <div className="text-accent">$ {line.command}</div>
            {line.stdout && <pre className="whitespace-pre-wrap">{line.stdout}</pre>}
            {line.stderr && <pre className="whitespace-pre-wrap text-danger">{line.stderr}</pre>}
          </div>
        ))}
      </div>
      <form className="border-t border-border p-2" onSubmit={(e) => { e.preventDefault(); void run() }}>
        <input
          className="input font-mono text-[12px]"
          placeholder="s3 ls"
          value={draft}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          onPaste={(e) => {
            const text = e.clipboardData.getData('text')
            if (!/[\n\r\\]/.test(text)) return
            e.preventDefault()
            const el = e.currentTarget
            const start = el.selectionStart ?? draft.length
            const end = el.selectionEnd ?? draft.length
            setDraft(normalizeAwsCommand(draft.slice(0, start) + text + draft.slice(end)))
          }}
        />
      </form>
    </div>
  )
}
