import { useEffect, useRef, useState, useCallback } from 'react'
import { AdsClick, Link as LinkIcon, RestartAlt, Send } from '@mui/icons-material'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import { toast } from '../lib/toast'

type PickedElement = {
  tag: string
  id: string | null
  className: string | null
  text: string
  outerHTML: string
  rect: { x: number; y: number; width: number; height: number }
}

/** Element Picker — local-install only.
 *
 * Attaches to a Chrome the user already has running (via
 * `chrome --remote-debugging-port=9222`) over the Chrome DevTools Protocol,
 * lets them click an element in a live screenshot of that page, and injects
 * a description of the picked element into the chat compose box so they can
 * ask for a change to "this exact thing" without hand-describing it.
 *
 * Zero Railway footprint: the backend hides every /api/element-picker/*
 * endpoint behind the same is_hosted signal that gates Import Folder, and
 * this panel is only ever mounted when the sidebar tab exists at all, which
 * Sidebar.tsx already conditions on `!settings?.is_hosted`. Disconnecting
 * here only ends the CDP client link — the user's actual Chrome keeps
 * running untouched.
 */
export function ElementPickerPanel() {
  const { setPendingChatInput } = useAppStore()
  const [cdpUrl, setCdpUrl] = useState('http://localhost:9222')
  const [connected, setConnected] = useState(false)
  const [pageUrl, setPageUrl] = useState<string | null>(null)
  const [connecting, setConnecting] = useState(false)
  const [screenshot, setScreenshot] = useState<string | null>(null)
  const [loadingShot, setLoadingShot] = useState(false)
  const [picked, setPicked] = useState<PickedElement | null>(null)
  const [picking, setPicking] = useState(false)
  const imgRef = useRef<HTMLImageElement>(null)

  // Pick up any pre-existing connection (e.g. panel re-opened after a tab switch).
  useEffect(() => {
    api.elementPicker.status()
      .then(s => {
        setConnected(s.connected)
        setPageUrl(s.page_url ?? null)
        if (s.connected) refreshScreenshot()
      })
      .catch(() => { /* silent — status is best-effort */ })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const refreshScreenshot = useCallback(async () => {
    setLoadingShot(true)
    try {
      const res = await api.elementPicker.screenshot()
      setScreenshot(`data:${res.mime_type};base64,${res.image_base64}`)
    } catch (e: any) {
      toast.error('Screenshot failed', e.message ?? String(e))
    } finally {
      setLoadingShot(false)
    }
  }, [])

  const handleConnect = async () => {
    setConnecting(true)
    try {
      const res = await api.elementPicker.connect(cdpUrl)
      setConnected(res.connected)
      setPageUrl(res.page_url ?? null)
      if (res.connected) {
        toast.success('Connected', res.page_url ? `Attached to ${res.page_url}` : 'Attached to Chrome')
        await refreshScreenshot()
      }
    } catch (e: any) {
      toast.error('Connect failed', e.message ?? String(e))
    } finally {
      setConnecting(false)
    }
  }

  const handleDisconnect = async () => {
    try {
      await api.elementPicker.disconnect()
    } catch { /* best-effort */ }
    setConnected(false)
    setScreenshot(null)
    setPicked(null)
    setPageUrl(null)
  }

  const handleImageClick = async (e: React.MouseEvent<HTMLImageElement>) => {
    const img = imgRef.current
    if (!img || picking) return
    const rect = img.getBoundingClientRect()
    const scaleX = img.naturalWidth / rect.width
    const scaleY = img.naturalHeight / rect.height
    const x = (e.clientX - rect.left) * scaleX
    const y = (e.clientY - rect.top) * scaleY
    setPicking(true)
    try {
      const result = await api.elementPicker.pick(x, y)
      setPicked(result)
    } catch (e: any) {
      toast.error('Pick failed', e.message ?? String(e))
    } finally {
      setPicking(false)
    }
  }

  const handleInsertIntoChat = () => {
    if (!picked) return
    const snippet = picked.outerHTML.length > 800 ? picked.outerHTML.slice(0, 800) + '…' : picked.outerHTML
    const description = [
      `Regarding this element on the page${pageUrl ? ` (${pageUrl})` : ''}:`,
      '```html',
      snippet,
      '```',
      picked.text ? `Visible text: "${picked.text.slice(0, 200)}"` : null,
      '',
      'Please ',
    ].filter(Boolean).join('\n')
    setPendingChatInput(description)
    toast.success('Added to chat', 'Finish your request in the compose box below.')
  }

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border flex items-center justify-between">
        <span className="text-[11px] text-muted flex items-center gap-1">
          <AdsClick sx={{ fontSize: 13 }} />
          {connected ? 'Connected' : 'Element Picker'}
        </span>
        {connected && (
          <button
            onClick={handleDisconnect}
            className="flex items-center gap-1 px-2 py-1 rounded-lg bg-overlay text-muted text-[11px] font-semibold hover:bg-red-500/10 hover:text-red-500 transition-colors"
            title="Disconnect (your Chrome window stays open)"
          >
            <RestartAlt sx={{ fontSize: 12 }} /> Disconnect
          </button>
        )}
      </div>

      {!connected ? (
        <div className="flex-1 overflow-y-auto p-4">
          <div className="flex flex-col items-center justify-center gap-3 text-center pt-6 pb-4">
            <div className="w-12 h-12 rounded-2xl bg-surface flex items-center justify-center">
              <AdsClick sx={{ fontSize: 22 }} className="text-muted/70" />
            </div>
            <div>
              <p className="text-[13px] font-semibold text-muted">Pick an element from a live page</p>
              <p className="text-[11px] text-faint mt-1 leading-relaxed max-w-[220px]">
                Start Chrome with remote debugging on, then connect below —
                point at any element and describe your change without
                copy-pasting HTML by hand.
              </p>
            </div>
          </div>
          <label className="block text-[11px] font-semibold text-muted mb-1">CDP URL</label>
          <div className="flex gap-1.5">
            <input
              value={cdpUrl}
              onChange={e => setCdpUrl(e.target.value)}
              placeholder="http://localhost:9222"
              className="flex-1 px-2 py-1.5 rounded-lg bg-overlay text-ink text-[12px] border border-border focus:outline-none focus:ring-1 focus:ring-accent"
            />
            <button
              onClick={handleConnect}
              disabled={connecting || !cdpUrl}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-accent text-white text-[12px] font-semibold hover:bg-accent/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <LinkIcon sx={{ fontSize: 13 }} /> {connecting ? 'Connecting…' : 'Connect'}
            </button>
          </div>
          <p className="text-[10px] text-faint mt-3 leading-relaxed">
            Launch Chrome once with, e.g.:<br />
            <code className="text-[10px] bg-overlay px-1 py-0.5 rounded">
              chrome --remote-debugging-port=9222
            </code>
          </p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-3">
          {pageUrl && (
            <p className="text-[10px] text-faint truncate" title={pageUrl}>{pageUrl}</p>
          )}
          <div className="relative rounded-lg overflow-hidden border border-border bg-overlay/50">
            {screenshot ? (
              <img
                ref={imgRef}
                src={screenshot}
                onClick={handleImageClick}
                alt="Live page"
                className={`w-full h-auto ${picking ? 'cursor-wait opacity-70' : 'cursor-crosshair'}`}
              />
            ) : (
              <div className="flex items-center justify-center h-32 text-[11px] text-faint">
                {loadingShot ? 'Loading screenshot…' : 'No screenshot yet'}
              </div>
            )}
          </div>
          <button
            onClick={refreshScreenshot}
            disabled={loadingShot}
            className="self-start flex items-center gap-1 px-2 py-1 rounded-lg bg-overlay text-muted text-[11px] font-semibold hover:bg-accent/10 hover:text-accent transition-colors disabled:opacity-50"
          >
            <RestartAlt sx={{ fontSize: 12 }} /> {loadingShot ? 'Refreshing…' : 'Refresh screenshot'}
          </button>

          {picked && (
            <div className="rounded-lg border border-border bg-surface p-2.5 flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-mono font-semibold text-accent">
                  &lt;{picked.tag}{picked.id ? ` id="${picked.id}"` : ''}&gt;
                </span>
              </div>
              {picked.text && (
                <p className="text-[11px] text-muted line-clamp-2">"{picked.text}"</p>
              )}
              <pre className="text-[10px] text-faint bg-overlay rounded p-1.5 overflow-x-auto max-h-24 whitespace-pre-wrap break-all">
                {picked.outerHTML.slice(0, 300)}
              </pre>
              <button
                onClick={handleInsertIntoChat}
                className="flex items-center justify-center gap-1 px-3 py-1.5 rounded-lg bg-accent text-white text-[12px] font-semibold hover:bg-accent/90 transition-colors"
              >
                <Send sx={{ fontSize: 13 }} /> Insert into chat
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
