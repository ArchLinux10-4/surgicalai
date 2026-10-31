import { useState } from 'react'
import { browserApi } from './browserClient'
import { useBrowserStore } from './browserStore'
import { browserStrings } from './strings'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'

const GROUPS: { title: string; fields: string[] }[] = [
  { title: 'Layout', fields: ['display', 'width', 'height', 'gap', 'flex-direction', 'justify-content', 'align-items', 'grid-template-columns'] },
  { title: 'Spacing', fields: ['margin', 'padding'] },
  { title: 'Typography', fields: ['font-family', 'font-size', 'font-weight', 'line-height', 'letter-spacing', 'color', 'text-align'] },
  { title: 'Appearance', fields: ['background-color', 'border', 'border-radius', 'box-shadow', 'opacity'] },
]

function boxValues(value: string): [string, string, string, string] {
  const parts = (value || '0').trim().split(/\s+/)
  if (parts.length === 1) return [parts[0], parts[0], parts[0], parts[0]]
  if (parts.length === 2) return [parts[0], parts[1], parts[0], parts[1]]
  if (parts.length === 3) return [parts[0], parts[1], parts[2], parts[1]]
  return [parts[0] || '0', parts[1] || '0', parts[2] || '0', parts[3] || '0']
}

export function StyleInspector() {
  const open = useBrowserStore((s) => s.inspectorOpen)
  const inspector = useBrowserStore((s) => s.inspector)
  const [busy, setBusy] = useState(false)
  if (!open) return null
  const node = inspector?.node
  const edits = inspector?.edits || {}
  const computed = node?.computed || {}

  const setProp = async (prop: string, value: string) => {
    if (!node?.token || !inspector) {
      toast.info(browserStrings.noToken)
      return
    }
    const next = { ...edits, [prop]: value }
    useBrowserStore.setState({ inspector: { ...inspector, edits: next } })
    const chip = useAppStore.getState().pickedElements.find((el) => el.token === node.token)
    if (chip) useAppStore.getState().updatePickedElement(chip.id, { styleEdits: next })
    try {
      await browserApi.style(node.token, { [prop]: value })
    } catch (err: any) {
      toast.error('Style was not applied', err?.message || '')
    }
  }

  const reset = async () => {
    if (!node?.token || !inspector) return
    setBusy(true)
    try {
      await browserApi.clearStyle(node.token)
      useBrowserStore.setState({ inspector: { ...inspector, edits: {} } })
      const chip = useAppStore.getState().pickedElements.find((el) => el.token === node.token)
      if (chip) useAppStore.getState().updatePickedElement(chip.id, { styleEdits: {} })
    } catch (err: any) {
      toast.error('Reset failed', err?.message || '')
    } finally {
      setBusy(false)
    }
  }

  const applyToCode = () => {
    if (!node) return
    const lines = Object.entries(edits).map(([key, value]) => `${key}: ${computed[key] || ''} → ${value}`)
    const src = inspector?.source?.[0]
    const where = src ? `${src.file}:${src.line}` : (node.css_path || node.tag)
    const id = node.el_id ? `#${node.el_id}` : ''
    const text = [
      `Update the styles of <${node.tag}>${id} at ${where}:`,
      lines.join('\n') || '(no style edits yet)',
      'Keep the markup; change the stylesheet or Tailwind classes that produce these values.',
    ].join('\n')
    useAppStore.getState().appendToComposer(text)
    useAppStore.getState().setBrowserOpen(false)
  }

  const openHit = async (fileId: string, filename: string, line: number) => {
    const sessionId = useAppStore.getState().activeSessions
    if (!sessionId || !fileId) {
      toast.info(filename, `Line ${line}`)
      return
    }
    try {
      const file = await api.sessionFiles.get(sessionId, fileId)
      useAppStore.getState().setActiveFile({
        path: file.filename || filename,
        content: file.content || '',
        language: file.language || 'text',
        size: (file.content || '').length,
        lines: file.lines || 0,
        revealLine: line,
      })
      useAppStore.getState().setRightTab('editor')
      useAppStore.getState().setBrowserOpen(false)
    } catch (err: any) {
      toast.error('Could not open file', err?.message || filename)
    }
  }

  return (
    <aside className="flex w-[300px] shrink-0 flex-col border-l border-border bg-surface">
      <div className="border-b border-border px-3 py-2">
        <div className="text-[12px] font-semibold text-ink">
          {node ? `<${node.tag}>${node.el_id ? '#' + node.el_id : ''}` : 'Nothing picked'}
        </div>
        {node?.css_path && <div className="truncate font-mono text-[10px] text-muted">{node.css_path}</div>}
        <div className="mt-1 flex flex-col gap-0.5">
          {(inspector?.source || []).slice(0, 3).map((hit) => (
            <button
              key={`${hit.file}:${hit.line}`}
              type="button"
              className="truncate text-left text-[11px] text-accent hover:underline"
              onClick={() => { void openHit(hit.file_id, hit.file, hit.line) }}
            >
              {hit.file}:{hit.line}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {!node && <p className="text-[12px] text-muted">Pick an element to edit its live styles.</p>}
        {node && GROUPS.map((group) => (
          <div key={group.title} className="mb-3">
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted">{group.title}</div>
            {group.fields.map((field) => (
              field === 'margin' || field === 'padding' ? (
                <BoxField key={`${node.token}-${field}`} label={field} value={edits[field] || computed[field] || ''} onCommit={(value) => { void setProp(field, value) }} />
              ) : (
                <label key={field} className="mb-1 flex items-center gap-2 text-[11px] text-muted">
                  <span className="w-24 shrink-0 truncate">{field}</span>
                  {(field === 'color' || field === 'background-color') && /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(edits[field] || '') && (
                    <input type="color" value={edits[field]} onChange={(ev) => { void setProp(field, ev.target.value) }} className="h-5 w-5 border-0 bg-transparent" />
                  )}
                  <input
                    value={edits[field] ?? computed[field] ?? ''}
                    onChange={(ev) => {
                      if (!inspector) return
                      useBrowserStore.setState({ inspector: { ...inspector, edits: { ...edits, [field]: ev.target.value } } })
                    }}
                    onBlur={(ev) => { if (ev.target.value !== (computed[field] || '')) void setProp(field, ev.target.value) }}
                    className="min-w-0 flex-1 rounded border border-border bg-base px-1.5 py-0.5 text-ink"
                  />
                </label>
              )
            ))}
          </div>
        ))}
      </div>
      <div className="flex gap-2 border-t border-border p-2">
        <button type="button" disabled={busy || !node?.token} onClick={() => { void reset() }} className="rounded-md border border-border px-2 py-1 text-[11px] text-muted hover:text-ink disabled:opacity-40">
          {browserStrings.resetStyles}
        </button>
        <button type="button" disabled={!node} onClick={applyToCode} className="flex-1 rounded-md bg-accent px-2 py-1 text-[11px] font-semibold text-white disabled:opacity-40">
          {browserStrings.applyToCode}
        </button>
      </div>
    </aside>
  )
}

function BoxField({ label, value, onCommit }: { label: string; value: string; onCommit: (value: string) => void }) {
  const [top, right, bottom, left] = boxValues(value)
  const [parts, setParts] = useState([top, right, bottom, left])
  const names = ['top', 'right', 'bottom', 'left']
  return (
    <div className="mb-1">
      <div className="text-[11px] text-muted">{label}</div>
      <div className="mt-0.5 grid grid-cols-4 gap-1">
        {parts.map((part, i) => (
          <input
            key={names[i]}
            aria-label={`${label} ${names[i]}`}
            value={part}
            onChange={(ev) => setParts((prev) => prev.map((item, idx) => idx === i ? ev.target.value : item))}
            onBlur={() => onCommit(parts.join(' '))}
            className="w-full rounded border border-border bg-base px-1 py-0.5 text-[10px] text-ink"
          />
        ))}
      </div>
    </div>
  )
}
