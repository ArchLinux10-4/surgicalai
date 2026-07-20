import { useCallback, useEffect, useRef, useState } from 'react'
import { AdsClick, Close, Language, Launch, PanTool, RestartAlt } from '@mui/icons-material'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import { toast } from '../lib/toast'

type FrameMetadata = {
  deviceWidth?: number
  deviceHeight?: number
}

/** Element Picker — full-screen live-view modal, local-install only.
 *
 * Streams the connected Chrome tab over a CDP screencast (Page.startScreencast)
 * so the app shows a real, continuously-updating browser view — not a static
 * screenshot the user has to manually refresh. Two interaction modes, same
 * convention as Chrome DevTools' own element inspector:
 *   - Browse (default): clicks/scroll/typing relay into the real page, so the
 *     user can navigate anywhere to reach the element they want.
 *   - Pick (toggle): the next click never touches the page — it calls the
 *     existing non-mutating elementFromPoint() endpoint and adds a chip above
 *     the chat composer instead. This avoids ever accidentally submitting a
 *     form or navigating away just because the user meant to "select" something.
 *
 * Zero Railway footprint: gated by the same is_hosted signal as the rest of
 * this feature (see ElementPickerPanel's original docstring for the full
 * rationale) — this modal is only ever mounted when settings.is_hosted is
 * false, and every backend call it makes 404s on a hosted deploy regardless.
 */
export function ElementPickerModal() {
  const { elementPickerModalOpen, setElementPickerModalOpen, addPickedElement } = useAppStore()
  const [connected, setConnected] = useState(false)
  const [pageUrl, setPageUrl] = useState<string | null>(null)
  const [addressInput, setAddressInput] = useState('')
  const [launching, setLaunching] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [mode, setMode] = useState<'browse' | 'pick'>('browse')
  const [picking, setPicking] = useState(false)
  const [liveError, setLiveError] = useState<string | null>(null)

  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const metaRef = useRef<FrameMetadata>({})

  const closeWs = useCallback(() => {
    try { wsRef.current?.close() } catch { /* best-effort */ }
    wsRef.current = null
  }, [])

  const drawFrame = useCallback((base64Jpeg: string, metadata: FrameMetadata) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const img = new Image()
    img.onload = () => {
      canvas.width = img.naturalWidth
      canvas.height = img.naturalHeight
      const ctx = canvas.getContext('2d')
      ctx?.drawImage(img, 0, 0)
      metaRef.current = metadata
    }
    img.src = `data:image/jpeg;base64,${base64Jpeg}`
  }, [])

  const startLiveView = useCallback(() => {
    closeWs()
    setLiveError(null)
    const ws = new WebSocket(api.elementPicker.wsStreamUrl())
    wsRef.current = ws
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data)
        if (msg.type === 'frame') drawFrame(msg.data, msg.metadata || {})
        else if (msg.type === 'error') setLiveError(msg.message || 'Live view error')
      } catch { /* ignore malformed frame */ }
    }
    ws.onerror = () => setLiveError('Live view connection failed.')
    ws.onclose = () => { if (wsRef.current === ws) wsRef.current = null }
  }, [closeWs, drawFrame])

  // Open/close lifecycle: connect status + start/stop live view with the modal.
  useEffect(() => {
    if (!elementPickerModalOpen) { closeWs(); return }
    api.elementPicker.status()
      .then(s => {
        setConnected(s.connected)
        setPageUrl(s.page_url ?? null)
        setAddressInput(s.page_url ?? '')
        if (s.connected) startLiveView()
      })
      .catch(() => { /* best-effort */ })
    return () => closeWs()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [elementPickerModalOpen])

  const handleLaunchAndConnect = async () => {
    setLaunching(true)
    try {
      const launchRes = await api.elementPicker.launch('http://localhost:9222')
      if (launchRes.launched) {
        toast.success('Picker browser opened', 'A separate Chrome window just opened — your regular Chrome is untouched.')
      }
      const res = await api.elementPicker.connect('http://localhost:9222')
      setConnected(res.connected)
      setPageUrl(res.page_url ?? null)
      setAddressInput(res.page_url ?? '')
      if (res.connected) startLiveView()
    } catch (e: any) {
      toast.error('Could not open picker browser', e.message ?? String(e))
    } finally {
      setLaunching(false)
    }
  }

  const handleDisconnect = async () => {
    closeWs()
    try { await api.elementPicker.disconnect() } catch { /* best-effort */ }
    setConnected(false)
    setPageUrl(null)
  }

  const handleNavigate = () => {
    if (!addressInput.trim() || !wsRef.current) return
    let url = addressInput.trim()
    if (!/^https?:\/\//i.test(url)) url = `https://${url}`
    wsRef.current.send(JSON.stringify({ type: 'navigate', url }))
  }

  // Map a canvas click into real page coordinates using the frame's own
  // pixel buffer size vs. the original page's device size (screencast
  // frames are scaled down to maxWidth/maxHeight — see start_screencast).
  const toPageCoords = (e: React.MouseEvent<HTMLCanvasElement>): { x: number; y: number } | null => {
    const canvas = canvasRef.current
    if (!canvas || canvas.width === 0) return null
    const rect = canvas.getBoundingClientRect()
    const frameX = (e.clientX - rect.left) * (canvas.width / rect.width)
    const frameY = (e.clientY - rect.top) * (canvas.height / rect.height)
    const { deviceWidth, deviceHeight } = metaRef.current
    const scaleX = deviceWidth ? deviceWidth / canvas.width : 1
    const scaleY = deviceHeight ? deviceHeight / canvas.height : 1
    return { x: frameX * scaleX, y: frameY * scaleY }
  }

  const handleCanvasClick = async (e: React.MouseEvent<HTMLCanvasElement>) => {
    const coords = toPageCoords(e)
    if (!coords) return

    if (mode === 'pick') {
      setPicking(true)
      try {
        const result = await api.elementPicker.pick(coords.x, coords.y)
        addPickedElement({
          id: `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
          tag: result.tag,
          elId: result.id,
          className: result.className,
          text: result.text,
          outerHTML: result.outerHTML,
          pageUrl,
        })
        toast.success('Added', 'Element added — see the chip above your message box.')
        setMode('browse')
      } catch (e: any) {
        toast.error('Pick failed', e.message ?? String(e))
      } finally {
        setPicking(false)
      }
      return
    }

    // Browse mode: relay a real click into the live page.
    wsRef.current?.send(JSON.stringify({ type: 'mousePressed', x: coords.x, y: coords.y, button: 'left' }))
    wsRef.current?.send(JSON.stringify({ type: 'mouseReleased', x: coords.x, y: coords.y, button: 'left' }))
  }

  const handleCanvasWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    if (mode !== 'browse') return
    const coords = toPageCoords(e as unknown as React.MouseEvent<HTMLCanvasElement>)
    if (!coords || !wsRef.current) return
    wsRef.current.send(JSON.stringify({
      type: 'mouseWheel', x: coords.x, y: coords.y, deltaX: e.deltaX, deltaY: e.deltaY,
    }))
  }

  const handleCanvasKeyDown = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (mode !== 'browse' || !wsRef.current) return
    const specialKeys: Record<string, string> = {
      Enter: 'Enter', Backspace: 'Backspace', Tab: 'Tab', Escape: 'Escape',
      ArrowLeft: 'ArrowLeft', ArrowRight: 'ArrowRight', ArrowUp: 'ArrowUp', ArrowDown: 'ArrowDown', Delete: 'Delete',
    }
    if (specialKeys[e.key]) {
      e.preventDefault()
      wsRef.current.send(JSON.stringify({ type: 'key', key: e.key, code: e.code }))
    } else if (e.key.length === 1) {
      e.preventDefault()
      wsRef.current.send(JSON.stringify({ type: 'text', text: e.key }))
    }
  }

  if (!elementPickerModalOpen) return null

  return (
    <div className="fixed inset-0 bg-black/70 z-[100] flex items-center justify-center backdrop-blur-sm">
      <div className="w-[94vw] h-[92vh] max-w-[1600px] bg-base border border-border rounded-2xl shadow-2xl flex flex-col overflow-hidden">
        {/* Header — address bar, mode toggle, close */}
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface/60 flex-shrink-0">
          <AdsClick sx={{ fontSize: 17 }} className="text-accent shrink-0" />
          <span className="text-[13px] font-semibold text-ink shrink-0">Element Picker</span>
          {connected && (
            <>
              <div className="flex-1 flex items-center gap-1.5 mx-2">
                <Language sx={{ fontSize: 14 }} className="text-muted/60 shrink-0" />
                <input
                  value={addressInput}
                  onChange={e => setAddressInput(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') handleNavigate() }}
                  placeholder="Enter a URL and press Enter…"
                  className="flex-1 px-2.5 py-1.5 rounded-lg bg-overlay text-ink text-[12px] border border-border focus:outline-none focus:ring-1 focus:ring-accent"
                />
              </div>
              <button
                onClick={() => setMode(m => m === 'browse' ? 'pick' : 'browse')}
                className={`flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-[11.5px] font-semibold transition-colors shrink-0 ${
                  mode === 'pick' ? 'bg-accent text-white' : 'bg-overlay text-muted hover:text-ink'
                }`}
                title={mode === 'pick' ? 'Click any element on the page to add it as a chip' : 'Switch to picking mode'}
              >
                {mode === 'pick' ? <AdsClick sx={{ fontSize: 14 }} /> : <PanTool sx={{ fontSize: 14 }} />}
                {mode === 'pick' ? (picking ? 'Picking…' : 'Click to pick') : 'Browse'}
              </button>
              <button
                onClick={handleDisconnect}
                className="flex items-center gap-1 px-2 py-1.5 rounded-lg bg-overlay text-muted text-[11px] font-semibold hover:bg-red-500/10 hover:text-red-500 transition-colors shrink-0"
                title="Disconnect (your Chrome window stays open)"
              >
                <RestartAlt sx={{ fontSize: 13 }} /> Disconnect
              </button>
            </>
          )}
          <button
            onClick={() => setElementPickerModalOpen(false)}
            className="p-1.5 rounded-lg text-muted hover:text-ink hover:bg-overlay transition-colors shrink-0"
            title="Close"
          >
            <Close sx={{ fontSize: 18 }} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 flex items-center justify-center bg-overlay/40 relative">
          {!connected ? (
            <div className="flex flex-col items-center gap-3 text-center px-6">
              <div className="w-14 h-14 rounded-2xl bg-surface flex items-center justify-center">
                <AdsClick sx={{ fontSize: 26 }} className="text-muted/70" />
              </div>
              <div>
                <p className="text-[14px] font-semibold text-muted">Pick an element from a live page</p>
                <p className="text-[12px] text-faint mt-1 leading-relaxed max-w-[320px]">
                  Opens a separate picker browser window, full-size right here in the app —
                  browse to any page, then click an element to describe your change without
                  copy-pasting HTML by hand.
                </p>
              </div>
              <button
                onClick={handleLaunchAndConnect}
                disabled={launching || connecting}
                className="flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg bg-accent text-white text-[13px] font-semibold hover:bg-accent/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Launch sx={{ fontSize: 16 }} /> {launching ? 'Opening picker browser…' : 'Launch Picker Browser'}
              </button>
              <p className="text-[10.5px] text-faint">Your regular Chrome window stays exactly as it is.</p>
            </div>
          ) : (
            <>
              {liveError && (
                <div className="absolute top-2 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-[11.5px] font-medium z-10">
                  {liveError}
                </div>
              )}
              <canvas
                ref={canvasRef}
                tabIndex={0}
                onClick={handleCanvasClick}
                onWheel={handleCanvasWheel}
                onKeyDown={handleCanvasKeyDown}
                className={`max-w-full max-h-full outline-none ${mode === 'pick' ? 'cursor-crosshair' : 'cursor-default'} ${picking ? 'opacity-70' : ''}`}
                style={{ boxShadow: '0 0 0 1px rgba(255,255,255,0.06)' }}
              />
            </>
          )}
        </div>
      </div>
    </div>
  )
}
