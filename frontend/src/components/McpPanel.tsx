import { useEffect, useState } from 'react'
import { api } from '../api/client'

type Tool = { name: string; description: string; always_allow?: boolean }
type Connection = {
  id: string
  name: string
  enabled: boolean
  transport: string
  url?: string
  command?: string
  args?: string[]
  header_names?: string[]
  headers_set?: boolean
  env_names?: string[]
  env_set?: boolean
  tools?: Tool[]
  last_error?: string
}
type Status = {
  available: boolean
  installed: boolean
  install_command: string
  connections: Connection[]
}
type Row = { name: string; value: string }

const empty = { name: '', transport: 'url' as 'url' | 'stdio', url: '', command: '', args: [''], headers: [{ name: '', value: '' }] as Row[], env: [{ name: '', value: '' }] as Row[] }

export function McpPanel() {
  const [status, setStatus] = useState<Status | null>(null)
  const [error, setError] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [step, setStep] = useState<1 | 2>(1)
  const [form, setForm] = useState(empty)
  const [tested, setTested] = useState<Tool[]>([])
  const [busy, setBusy] = useState(false)
  const [removeId, setRemoveId] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const load = () => {
    api.mcp.status()
      .then(setStatus)
      .catch((e: any) => setError(e?.message || 'Could not load MCP connections.'))
  }
  useEffect(() => { load() }, [])

  const copyInstall = async () => {
    try {
      await navigator.clipboard.writeText(status?.install_command || '')
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  const spec = () => ({
    name: form.name.trim(),
    transport: form.transport,
    url: form.url.trim(),
    headers: form.headers.filter(h => h.name.trim() && h.value),
    command: form.command.trim(),
    args: form.args.map(a => a.trim()).filter(Boolean),
    env: form.env.filter(e => e.name.trim() && e.value),
  })

  const test = async () => {
    setBusy(true)
    setError('')
    try {
      const out = await api.mcp.test(spec())
      if (!out.ok) {
        setError(out.error || 'The connection test failed.')
        return
      }
      setTested(out.tools || [])
      setStep(2)
    } catch (e: any) {
      setError(e?.message || 'The connection test failed.')
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    setBusy(true)
    setError('')
    try {
      await api.mcp.save({ ...spec(), confirmed: true })
      setAdding(false)
      setStep(1)
      setForm(empty)
      setTested([])
      load()
    } catch (e: any) {
      setError(e?.message || 'Could not save this connection.')
    } finally {
      setBusy(false)
    }
  }

  if (!status) {
    return <div className="text-sm text-muted">{error || 'Loading MCP connections…'}</div>
  }

  if (!status.available) {
    return (
      <p className="text-sm text-ink">
        MCP runs on the machine where SurgicalAI is installed. This hosted instance does not connect to MCP servers.
      </p>
    )
  }

  return (
    <div className="space-y-4">
      {!status.installed && (
        <div className="rounded-lg border border-border bg-surface p-3 text-sm">
          <div className="text-ink">The MCP client is not installed in this local app yet.</div>
          <code className="mt-2 block overflow-auto text-xs text-muted">{status.install_command}</code>
          <button type="button" className="btn-ghost mt-2 border border-border px-3 py-1 text-xs" onClick={copyInstall}>
            {copied ? 'Copied' : 'Copy command'}
          </button>
        </div>
      )}
      {error && <div className="rounded bg-red-500/10 px-3 py-2 text-xs text-red-400">{error}</div>}
      {status.connections.length === 0 && !adding && (
        <button type="button" className="btn-primary px-4 py-2 text-sm" onClick={() => { setAdding(true); setStep(1); setError('') }}>
          Add server
        </button>
      )}
      {status.connections.map(conn => (
        <div key={conn.id} className="rounded-lg border border-border p-3">
          <div className="flex items-center justify-between gap-3">
            <button type="button" className="text-left" onClick={() => setOpenId(openId === conn.id ? null : conn.id)}>
              <div className="font-medium text-ink">{conn.name}</div>
              <div className="text-xs text-muted">
                {conn.transport === 'url' ? conn.url : `${conn.command} ${(conn.args || []).join(' ')}`}
                {' · '}{(conn.tools || []).length} tools
                {conn.headers_set ? ' · token set' : ''}
                {conn.env_set ? ' · env set' : ''}
              </div>
            </button>
            <label className="flex items-center gap-2 text-xs text-muted">
              Enabled
              <input
                type="checkbox"
                checked={conn.enabled}
                onChange={async (e) => {
                  await api.mcp.patch(conn.id, { enabled: e.target.checked })
                  load()
                }}
              />
            </label>
          </div>
          {conn.last_error && <div className="mt-2 text-xs text-red-400">{conn.last_error}</div>}
          {openId === conn.id && (
            <div className="mt-3 space-y-2">
              {(conn.tools || []).map(tool => (
                <div key={tool.name} className="flex items-start justify-between gap-3 text-xs">
                  <div>
                    <div className="text-ink">{tool.name}</div>
                    <div className="text-muted">{tool.description}</div>
                  </div>
                  <label className="shrink-0 text-muted">
                    Always allow
                    <input
                      className="ml-2"
                      type="checkbox"
                      checked={!!tool.always_allow}
                      onChange={async (e) => {
                        await api.mcp.patch(conn.id, { tool: tool.name, always_allow: e.target.checked })
                        load()
                      }}
                    />
                  </label>
                </div>
              ))}
              {removeId === conn.id ? (
                <div className="flex gap-2 pt-2">
                  <span className="text-xs text-muted">Remove this connection?</span>
                  <button
                    type="button"
                    className="text-xs text-red-400"
                    onClick={async () => { await api.mcp.remove(conn.id); setRemoveId(null); load() }}
                  >
                    Remove
                  </button>
                  <button type="button" className="text-xs text-muted" onClick={() => setRemoveId(null)}>Cancel</button>
                </div>
              ) : (
                <button type="button" className="pt-2 text-xs text-red-400" onClick={() => setRemoveId(conn.id)}>
                  Disconnect
                </button>
              )}
            </div>
          )}
        </div>
      ))}
      {status.connections.length > 0 && !adding && (
        <button type="button" className="btn-ghost border border-border px-4 py-2 text-sm" onClick={() => { setAdding(true); setStep(1); setError('') }}>
          Add server
        </button>
      )}
      {adding && step === 1 && (
        <div className="space-y-3 rounded-lg border border-border p-3">
          <div className="text-sm font-medium text-ink">Add MCP server</div>
          <input className="w-full rounded border border-border bg-base px-2 py-1 text-sm" placeholder="Display name" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
          <select className="rounded border border-border bg-base px-2 py-1 text-sm" value={form.transport} onChange={e => setForm({ ...form, transport: e.target.value as 'url' | 'stdio' })}>
            <option value="url">URL</option>
            <option value="stdio">Command</option>
          </select>
          {form.transport === 'url' ? (
            <>
              <input className="w-full rounded border border-border bg-base px-2 py-1 text-sm" placeholder="https://host/mcp" value={form.url} onChange={e => setForm({ ...form, url: e.target.value })} />
              {form.headers.map((row, i) => (
                <div key={i} className="flex gap-2">
                  <input className="w-1/3 rounded border border-border bg-base px-2 py-1 text-sm" placeholder="Header" value={row.name} onChange={e => {
                    const headers = [...form.headers]; headers[i] = { ...row, name: e.target.value }; setForm({ ...form, headers })
                  }} />
                  <input className="flex-1 rounded border border-border bg-base px-2 py-1 text-sm" placeholder="Value" type="password" value={row.value} onChange={e => {
                    const headers = [...form.headers]; headers[i] = { ...row, value: e.target.value }; setForm({ ...form, headers })
                  }} />
                </div>
              ))}
              <button type="button" className="text-xs text-muted" onClick={() => setForm({ ...form, headers: [...form.headers, { name: '', value: '' }] })}>Add header</button>
            </>
          ) : (
            <>
              <input className="w-full rounded border border-border bg-base px-2 py-1 text-sm" placeholder="Program, for example npx" value={form.command} onChange={e => setForm({ ...form, command: e.target.value })} />
              {form.args.map((arg, i) => (
                <input key={i} className="w-full rounded border border-border bg-base px-2 py-1 text-sm" placeholder={`Argument ${i + 1}`} value={arg} onChange={e => {
                  const args = [...form.args]; args[i] = e.target.value; setForm({ ...form, args })
                }} />
              ))}
              <button type="button" className="text-xs text-muted" onClick={() => setForm({ ...form, args: [...form.args, ''] })}>Add argument</button>
              {form.env.map((row, i) => (
                <div key={i} className="flex gap-2">
                  <input className="w-1/3 rounded border border-border bg-base px-2 py-1 text-sm" placeholder="ENV" value={row.name} onChange={e => {
                    const env = [...form.env]; env[i] = { ...row, name: e.target.value }; setForm({ ...form, env })
                  }} />
                  <input className="flex-1 rounded border border-border bg-base px-2 py-1 text-sm" placeholder="Value" type="password" value={row.value} onChange={e => {
                    const env = [...form.env]; env[i] = { ...row, value: e.target.value }; setForm({ ...form, env })
                  }} />
                </div>
              ))}
              <button type="button" className="text-xs text-muted" onClick={() => setForm({ ...form, env: [...form.env, { name: '', value: '' }] })}>Add env var</button>
            </>
          )}
          <div className="flex gap-2">
            <button type="button" className="btn-primary px-3 py-1 text-sm" disabled={busy || !status.installed} onClick={test}>
              {busy ? 'Testing…' : 'Test connection'}
            </button>
            <button type="button" className="btn-ghost border border-border px-3 py-1 text-sm" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </div>
      )}
      {adding && step === 2 && (
        <div className="space-y-3 rounded-lg border border-border p-3">
          <div className="text-sm font-medium text-ink">Confirm {form.name}</div>
          <div className="text-xs text-muted">
            {form.transport === 'url' ? form.url : `${form.command} ${form.args.filter(Boolean).join(' ')}`}
          </div>
          <ul className="space-y-1 text-xs">
            {tested.map(tool => (
              <li key={tool.name}><span className="text-ink">{tool.name}</span> <span className="text-muted">{tool.description}</span></li>
            ))}
            {tested.length === 0 && <li className="text-muted">This server listed no tools.</li>}
          </ul>
          <div className="flex gap-2">
            <button type="button" className="btn-primary px-3 py-1 text-sm" disabled={busy} onClick={save}>
              {busy ? 'Saving…' : 'Confirm'}
            </button>
            <button type="button" className="btn-ghost border border-border px-3 py-1 text-sm" onClick={() => setStep(1)}>Back</button>
          </div>
        </div>
      )}
    </div>
  )
}
