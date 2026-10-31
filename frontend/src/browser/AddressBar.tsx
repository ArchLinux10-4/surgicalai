import { useEffect, useRef, useState } from 'react'
import { ArrowBack, ArrowForward, Close, AdsClick, PhotoCamera, Refresh, Stop, Terminal, Tune } from '@mui/icons-material'
import { browserApi } from './browserClient'
import { useBrowserStore } from './browserStore'
import { VIEWPORTS, browserStrings } from './strings'
import { useAppStore } from '../stores/appStore'
import { fetchScreenshot } from './browserClient'
import { api } from '../api/client'
import { toast } from '../lib/toast'

interface Props {
  onNavigate: (url: string) => void
}

export function AddressBar({ onNavigate }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const displayUrl = useBrowserStore((s) => s.displayUrl)
  const title = useBrowserStore((s) => s.title)
  const favicon = useBrowserStore((s) => s.favicon)
  const loading = useBrowserStore((s) => s.loading)
  const canGoBack = useBrowserStore((s) => s.canGoBack)
  const canGoForward = useBrowserStore((s) => s.canGoForward)
  const pickMode = useBrowserStore((s) => s.pickMode)
  const drawerOpen = useBrowserStore((s) => s.drawerOpen)
  const unread = useBrowserStore((s) => s.unreadConsoleErrors)
  const inspectorOpen = useBrowserStore((s) => s.inspectorOpen)
  const presetId = useBrowserStore((s) => s.presetId)
  const focusAddress = useBrowserStore((s) => s.focusAddress)
  const reloadAfterApply = useBrowserStore((s) => s.reloadAfterApply)
  const attachShot = useBrowserStore((s) => s.attachShot)
  const [draft, setDraft] = useState(displayUrl)
  const [editing, setEditing] = useState(false)
  const [devicesOpen, setDevicesOpen] = useState(false)

  useEffect(() => {
    if (!editing) setDraft(displayUrl)
  }, [displayUrl, editing])

  useEffect(() => {
    if (!focusAddress) return
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [focusAddress])

  const secure = displayUrl.startsWith('https://')

  return (
    <div className="flex flex-col border-b border-border bg-surface/80">
      <div className="flex items-center gap-1 px-2 py-1.5">
        <button type="button" title="Back" disabled={!canGoBack} onClick={() => browserApi.back().catch(() => {})} className="rounded-md p-1 text-muted hover:bg-overlay hover:text-ink disabled:opacity-30">
          <ArrowBack sx={{ fontSize: 16 }} />
        </button>
        <button type="button" title="Forward" disabled={!canGoForward} onClick={() => browserApi.forward().catch(() => {})} className="rounded-md p-1 text-muted hover:bg-overlay hover:text-ink disabled:opacity-30">
          <ArrowForward sx={{ fontSize: 16 }} />
        </button>
        <button
          type="button"
          title={loading ? 'Stop' : 'Reload'}
          onClick={() => (loading ? browserApi.stopLoading() : browserApi.reload()).catch(() => {})}
          className="rounded-md p-1 text-muted hover:bg-overlay hover:text-ink"
        >
          {loading ? <Stop sx={{ fontSize: 16 }} /> : <Refresh sx={{ fontSize: 16 }} />}
        </button>
        <form
          className="flex min-w-0 flex-1 items-center gap-1.5 rounded-lg border border-border bg-base px-2 py-1"
          onSubmit={(ev) => {
            ev.preventDefault()
            setEditing(false)
            onNavigate(draft)
          }}
        >
          {favicon ? <img src={favicon} alt="" className="h-3.5 w-3.5 shrink-0" /> : <span className={`h-2 w-2 shrink-0 rounded-full ${secure ? 'bg-success' : 'bg-muted/50'}`} />}
          <input
            ref={inputRef}
            value={editing ? draft : (displayUrl || draft)}
            title={title || displayUrl}
            onChange={(ev) => setDraft(ev.target.value)}
            onFocus={() => { setEditing(true); setDraft(displayUrl || draft) }}
            onBlur={() => setEditing(false)}
            onKeyDown={(ev) => { if (ev.key === 'Escape') { setDraft(displayUrl); setEditing(false); inputRef.current?.blur() } }}
            placeholder="localhost:5173 or a URL"
            spellCheck={false}
            className="min-w-0 flex-1 bg-transparent text-[12px] text-ink outline-none placeholder:text-muted/60"
          />
        </form>
        <button
          type="button"
          title={browserStrings.pickHint}
          onClick={() => browserApi.pickMode(!pickMode).then((status) => useBrowserStore.setState({ pickMode: status.pick_mode })).catch(() => {})}
          className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold ${pickMode ? 'bg-accent text-white' : 'text-muted hover:bg-overlay hover:text-ink'}`}
        >
          <AdsClick sx={{ fontSize: 14 }} />
          {browserStrings.pick}
        </button>
        <button
          type="button"
          title={browserStrings.console}
          onClick={() => useBrowserStore.getState().setDrawerOpen(!drawerOpen)}
          className={`relative rounded-md p-1 ${drawerOpen ? 'bg-overlay text-ink' : 'text-muted hover:bg-overlay hover:text-ink'}`}
        >
          <Terminal sx={{ fontSize: 16 }} />
          {unread > 0 && <span className="absolute -right-0.5 -top-0.5 min-w-[14px] rounded-full bg-danger px-1 text-center text-[9px] font-semibold text-white">{unread}</span>}
        </button>
        <button
          type="button"
          title={browserStrings.screenshot}
          onClick={() => { void addScreenshot() }}
          className="rounded-md p-1 text-muted hover:bg-overlay hover:text-ink"
        >
          <PhotoCamera sx={{ fontSize: 16 }} />
        </button>
        <button
          type="button"
          title={browserStrings.inspector}
          onClick={() => useBrowserStore.getState().setInspectorOpen(!inspectorOpen)}
          className={`rounded-md p-1 ${inspectorOpen ? 'bg-overlay text-ink' : 'text-muted hover:bg-overlay hover:text-ink'}`}
        >
          <Tune sx={{ fontSize: 16 }} />
        </button>
        <div className="relative">
          <button type="button" onClick={() => setDevicesOpen((v) => !v)} className="rounded-md px-2 py-1 text-[11px] text-muted hover:bg-overlay hover:text-ink">
            {VIEWPORTS.find((item) => item.id === presetId)?.label || 'Responsive'}
          </button>
          {devicesOpen && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setDevicesOpen(false)} />
              <div className="absolute right-0 top-full z-50 mt-1 w-44 rounded-lg border border-border bg-surface py-1 shadow-xl">
                {VIEWPORTS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => { useBrowserStore.getState().setPreset(item.id); setDevicesOpen(false) }}
                    className={`block w-full px-3 py-1.5 text-left text-[12px] hover:bg-overlay ${presetId === item.id ? 'text-accent' : 'text-ink'}`}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
        <button type="button" title="Close browser" onClick={() => useAppStore.getState().setBrowserOpen(false)} className="rounded-md p-1 text-muted hover:bg-overlay hover:text-ink">
          <Close sx={{ fontSize: 16 }} />
        </button>
      </div>
      <div className="flex items-center gap-3 px-3 pb-1 text-[10px] text-muted">
        <label className="inline-flex items-center gap-1">
          <input type="checkbox" checked={attachShot} onChange={(ev) => useBrowserStore.getState().setAttachShot(ev.target.checked)} />
          Attach screenshot
        </label>
        <label className="inline-flex items-center gap-1" title={browserStrings.reloadAfterApply}>
          <input type="checkbox" checked={reloadAfterApply} onChange={(ev) => useBrowserStore.getState().setReloadAfterApply(ev.target.checked)} />
          {browserStrings.reloadAfterApply}
        </label>
      </div>
      {loading && <div className="h-0.5 w-full overflow-hidden bg-accent/15"><div className="h-full w-1/3 animate-pulse bg-accent" /></div>}
    </div>
  )
}

async function addScreenshot() {
  const sessionId = useAppStore.getState().activeSessions
  if (!sessionId) {
    toast.info('Open a chat first', 'A screenshot needs a session to attach to.')
    return
  }
  try {
    const blob = await fetchScreenshot()
    const dataUrl = await blobToDataUrl(blob)
    const file = await api.sessionFiles.upload(sessionId, {
      filename: `browser-${Date.now()}.png`,
      content: '',
      base64_data: dataUrl,
      file_type: 'image',
    })
    useAppStore.getState().addSessionFile(file)
    const thumb = await thumbFromBlob(blob)
    useAppStore.getState().addBrowserShot({
      id: file.id,
      fileId: file.id,
      filename: file.filename,
      thumb,
      pageUrl: useBrowserStore.getState().displayUrl,
    })
    useAppStore.getState().setBrowserOpen(false)
  } catch (err: any) {
    toast.error('Screenshot failed', err?.message || '')
  }
}

async function thumbFromBlob(blob: Blob): Promise<string> {
  const bmp = await createImageBitmap(blob)
  const width = 160
  const height = Math.max(1, Math.round(bmp.height * (width / Math.max(1, bmp.width))))
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) {
    bmp.close()
    return ''
  }
  ctx.drawImage(bmp, 0, 0, width, height)
  bmp.close()
  return canvas.toDataURL('image/jpeg', 0.7)
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}
