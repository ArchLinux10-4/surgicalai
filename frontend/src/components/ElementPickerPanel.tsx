import { useCallback, useEffect, useRef, useState } from 'react'
import { AdsClick, Close, Language, Launch, PanTool, Refresh, RestartAlt } from '@mui/icons-material'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import { toast } from '../lib/toast'

type FrameMetadata = {
  deviceWidth?: number
  deviceHeight?: number
}

/** Element Picker — inline docked panel, local-install only.
 *
 * Docks into the same "third pane" slot CodePanel uses (see Layout.tsx),
 * beside the chat panel, instead of taking over the whole screen with a
 * modal — this matches how Cursor's own in-app Browser tool behaves (an
 * embedded pane alongside your other context, never a full takeover) and
 * was a direct, deliberate fix for user feedback that the old full-screen
 * modal felt bolted-on rather than native to the app.
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
 * Frames arrive as binary WebSocket messages (4-byte metadata-length header +
 * JSON metadata + raw JPEG bytes — see routers/element_picker.py) decoded via
 * createImageBitmap, and are captured at the panel's real CSS size ×
 * devicePixelRatio so the view is sharp on Retina/HiDPI screens instead of a
 * fixed low-res frame stretched to fill a bigger box.
 *
 * Zero Railway footprint: gated by the same is_hosted signal as the rest of
 * this feature — this panel is only ever mounted when settings.is_hosted is
 * false, and every backend call it makes 404s on a hosted deploy regardless.
 */
export function ElementPickerPanel() {
  const { elementPickerModalOpen, setElementPickerModalOpen, addPickedElement } = useAppStore()
  const [connected, setConnected] = useState(false)
  const [pageUrl, setPageUrl] = useState<string | null>(null)
  const [addressInput, setAddressInput] = useState('')
  const [launching, setLaunching] = useState(false)
  const [mode, setMode] = useState<'browse' | 'pick'>('browse')
  const [picking, setPicking] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [liveError, setLiveError] = useState<string | null>(null)
  // True from the moment `connected` flips on until the very first frame is
  // painted — without this the user sees a blank gray box with zero
  // feedback while the screencast spins up (CDP session start, first
  // repaint) which reads as broken rather than loading.
  const [awaitingFirstFrame, setAwaitingFirstFrame] = useState(false)

  const canvasRef = useRef<HTMLCanvasElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const metaRef = useRef<FrameMetadata>({})

  // Decode pipeline state — drop-if-busy: while a frame is mid-decode, any
  // newer frames that arrive just overwrite `pendingRef` rather than queuing;
  // once the in-flight decode finishes we jump straight to the latest frame,
  // never a stale backlog. This is what stops the stutter/lag-buildup the
  // old base64+JSON+<img> pipeline had under any slow decode.
  const decodingRef = useRef(false)
  const pendingRef = useRef<{ blob: Blob; metadata: FrameMetadata } | null>(null)

  const closeWs = useCallback(() => {
    try { wsRef.current?.close() } catch { /* best-effort */ }
    wsRef.current = null
  }, [])

  const drawBitmap = useCallback((bitmap: ImageBitmap, metadata: FrameMetadata) => {
    const canvas = canvasRef.current
    if (!canvas) { bitmap.close(); return }
    canvas.width = bitmap.width
    canvas.height = bitmap.height
    const ctx = canvas.getContext('2d')
    ctx?.drawImage(bitmap, 0, 0)
    bitmap.close()
    metaRef.current = metadata
    setAwaitingFirstFrame(false)
  }, [])

  const decodeNext = useCallback(() => {
    const next = pendingRef.current
    pendingRef.current = null
    if (!next) { decodingRef.current = false; return }
    decodingRef.current = true
    createImageBitmap(next.blob)
      .then(bitmap => drawBitmap(bitmap, next.metadata))
      .catch(() => { /* corrupt/partial frame — just skip it */ })
      .finally(() => decodeNext())
  }, [drawBitmap])

  const handleBinaryFrame = useCallback((buf: ArrayBuffer) => {
    if (buf.byteLength < 4) return
    const view = new DataView(buf)
    const metaLen = view.getUint32(0, false)
    if (buf.byteLength < 4 + metaLen) return
    let metadata: FrameMetadata = {}
    try {
      metadata = JSON.parse(new TextDecoder().decode(buf.slice(4, 4 + metaLen)))
    } catch { /* keep last-known metadata */ }
    const jpegBytes = buf.slice(4 + metaLen)
    const blob = new Blob([jpegBytes], { type: 'image/jpeg' })
    pendingRef.current = { blob, metadata }
    if (!decodingRef.current) decodeNext()
  }, [decodeNext])

  const startLiveView = useCallback(() => {
    closeWs()
    setLiveError(null)
    setAwaitingFirstFrame(true)
    const dpr = window.devicePixelRatio || 1
    const w = bodyRef.current?.clientWidth || 1400
    const h = bodyRef.current?.clientHeight || 900
    const ws = new WebSocket(api.elementPicker.wsStreamUrl({ w, h, dpr }))
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws
    ws.onmessage = (evt) => {
      if (evt.data instanceof ArrayBuffer) {
        handleBinaryFrame(evt.data)
        return
      }
      // Non-frame control messages (errors) still arrive as JSON text.
      try {
        const msg = JSON.parse(evt.data)
        if (msg.type === 'error') setLiveError(msg.message || 'Live view error')
      } catch { /* ignore malformed message */ }
    }
    ws.onerror = () => setLiveError('Live view connection failed.')
    ws.onclose = () => { if (wsRef.current === ws) wsRef.current = null }
  }, [closeWs, handleBinaryFrame])

  // Open/close lifecycle: connect status + start/stop live view with the panel.
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

  // Re-negotiate resolution on window resize (debounced) — otherwise
  // dragging the browser window wider than the size we originally requested
  // means the backend keeps streaming at the old, smaller resolution and it
  // gets stretched to fill the new space: the exact "blurry" bug the
  // DPI-aware sizing fix was meant to kill, just re-triggered by a resize
  // instead of a fresh open. Only rebinds while actually connected+live.
  useEffect(() => {
    if (!elementPickerModalOpen || !connected) return
    let debounceTimer: ReturnType<typeof setTimeout> | null = null
    const onResize = () => {
      if (debounceTimer) clearTimeout(debounceTimer)
      // Small delay after the resize settles, then a further beat so the
      // backend's own screencast-stop from the closing socket has time to
      // finish before the replacement socket's start_screencast runs (that
      // call no-ops if the prior session is still marked active).
      debounceTimer = setTimeout(() => startLiveView(), 400)
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      if (debounceTimer) clearTimeout(debounceTimer)
    }
  }, [elementPickerModalOpen, connected, startLiveView])

  // Esc closes the panel — matches every other overlay/pane in the app.
  useEffect(() => {
    if (!elementPickerModalOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setElementPickerModalOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [elementPickerModalOpen, setElementPickerModalOpen])

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
    // disconnect() tears down the CDP session (and its inspect-mode state)
    // server-side already — reset local UI state to match so a future
    // reconnect starts clean in Browse mode rather than showing "Pick" as
    // selected with no highlight actually armed.
    setMode('browse')
  }

  const handleNavigate = () => {
    if (!addressInput.trim() || !wsRef.current) return
    let url = addressInput.trim()
    if (!/^https?:\/\//i.test(url)) url = `https://${url}`
    wsRef.current.send(JSON.stringify({ type: 'navigate', url }))
  }

  // Hard refresh — bypasses the browser cache, the exact gap that made
  // iterating on frontend changes painful: without this the only way to
  // force the live view to pick up a fresh build was disconnect + relaunch.
  // Goes over the REST endpoint (not the WS control channel) since it's a
  // one-off action, not a continuous input stream like mouse/keyboard.
  const handleHardRefresh = useCallback(async () => {
    if (!connected || refreshing) return
    setRefreshing(true)
    try {
      await api.elementPicker.reload(true)
    } catch (e: any) {
      toast.error('Refresh failed', e.message ?? String(e))
    } finally {
      // The reload itself is fire-and-forget from the backend's point of
      // view (CDP doesn't wait for it) — the screencast just starts
      // showing the reloading/reloaded page on its own. This delay is
      // purely so the spinner reads as "did something" instead of
      // flashing for a single frame.
      setTimeout(() => setRefreshing(false), 400)
    }
  }, [connected, refreshing])

  // Switching modes toggles the live CDP hover-highlight (Overlay.setInspectMode
  // — see backend/services/element_picker.py). Only ever armed while Pick mode
  // is on: while it's on, Chrome intercepts clicks for node-selection instead
  // of letting them reach the page, which is correct for Pick mode but would
  // silently break normal navigation/clicking if left on during Browse mode.
  // Best-effort — if it fails the picker still works exactly as before, the
  // user just loses the live highlight box.
  const handleModeChange = useCallback((next: 'browse' | 'pick') => {
    setMode(next)
    if (!connected) return
    api.elementPicker.setInspectMode(next === 'pick').catch((e: any) => {
      toast.error('Live highlight unavailable', e.message ?? String(e))
    })
  }, [connected])

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

  // Relays live cursor position into the page so Chrome's own hover
  // highlight (Overlay.setInspectMode, armed in Pick mode — see
  // handleModeChange above) has motion to react to. Without this, our
  // synthetic input never generates a mousemove, so Chrome's renderer never
  // sees the cursor "enter" any element inside our docked view, and the
  // Overlay agent has nothing to paint into the frames we stream back —
  // confirmed root cause of "highlight shows in the real Chrome window but
  // not in the app". Fires in both modes (not just Pick) so Browse mode
  // also gets real hover feedback (e.g. link/button hover states), matching
  // normal browser behavior. High-frequency by nature, so it goes through
  // the same coalescing path as mouseWheel — see _queue_continuous.
  const handleCanvasMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const coords = toPageCoords(e)
    if (!coords || !wsRef.current) return
    wsRef.current.send(JSON.stringify({ type: 'mouseMoved', x: coords.x, y: coords.y }))
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
    // Ctrl/Cmd+Shift+R while the live view is focused — same shortcut as a
    // real browser's hard refresh, intercepted here (preventDefault) so it
    // triggers OUR reload endpoint instead of hard-refreshing the surgicalai
    // app itself, which is what would happen if this fell through.
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'R' || e.key === 'r')) {
      e.preventDefault()
      handleHardRefresh()
      return
    }
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
    <div className="flex-1 flex flex-col bg-base min-w-0 border-l border-border">
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
              onClick={handleHardRefresh}
              disabled={refreshing}
              className="flex items-center gap-1 px-2 py-1.5 rounded-lg bg-overlay text-muted text-[11px] font-semibold hover:text-ink transition-colors shrink-0 disabled:opacity-50"
              title="Hard refresh — reload the page bypassing cache (⌘/Ctrl+Shift+R while focused on the page)"
            >
              <Refresh sx={{ fontSize: 14 }} className={refreshing ? 'animate-spin' : ''} />
            </button>
            {/* Browse/Pick segmented control — both states always visible side
                by side (not a single button whose label silently swaps), so
                the current mode is unambiguous at a glance, matching how
                Chrome DevTools' own inspect-toggle stays visually distinct
                from the rest of its toolbar. */}
            <div className="flex items-center gap-0.5 p-0.5 rounded-lg bg-overlay shrink-0" role="group" aria-label="Interaction mode">
              <button
                onClick={() => handleModeChange('browse')}
                aria-pressed={mode === 'browse'}
                className={`flex items-center gap-1 px-2 py-1 rounded-md text-[11.5px] font-semibold transition-colors ${
                  mode === 'browse' ? 'bg-surface text-ink shadow-soft' : 'text-muted hover:text-ink'
                }`}
                title="Browse mode — clicks, scroll, and typing go to the real page"
              >
                <PanTool sx={{ fontSize: 13 }} /> Browse
              </button>
              <button
                onClick={() => handleModeChange('pick')}
                aria-pressed={mode === 'pick'}
                className={`flex items-center gap-1 px-2 py-1 rounded-md text-[11.5px] font-semibold transition-colors ${
                  mode === 'pick' ? 'bg-accent text-white' : 'text-muted hover:text-ink'
                }`}
                title="Pick mode — hover to highlight, click an element to add it as a chip"
              >
                <AdsClick sx={{ fontSize: 13 }} /> {picking ? 'Picking…' : 'Pick'}
              </button>
            </div>
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
          title="Close (back to your file/chat)"
        >
          <Close sx={{ fontSize: 18 }} />
        </button>
      </div>

      {/* Body */}
      <div ref={bodyRef} className="flex-1 min-h-0 flex items-center justify-center bg-overlay/40 relative">
        {!connected ? (
          <div className="flex flex-col items-center gap-3 text-center px-6">
            <div className="w-14 h-14 rounded-2xl bg-surface flex items-center justify-center">
              <AdsClick sx={{ fontSize: 26 }} className="text-muted/70" />
            </div>
            <div>
              <p className="text-[14px] font-semibold text-muted">Pick an element from a live page</p>
              <p className="text-[12px] text-faint mt-1 leading-relaxed max-w-[320px]">
                Opens a separate picker browser window, shown right here beside your chat —
                browse to any page, then click an element to describe your change without
                copy-pasting HTML by hand.
              </p>
            </div>
            <button
              onClick={handleLaunchAndConnect}
              disabled={launching}
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
            {awaitingFirstFrame && !liveError && (
              <div className="absolute inset-0 flex items-center justify-center bg-overlay/40 z-10 pointer-events-none">
                <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-surface text-muted text-[12px] font-medium">
                  <span className="w-3.5 h-3.5 rounded-full border-2 border-muted/40 border-t-accent animate-spin" />
                  Connecting live view…
                </div>
              </div>
            )}
            {/* Persistent mode badge — unlike the toolbar toggle above (which
                you have to glance up and to the side to read), this sits
                right on the page you're looking at, so which mode you're in
                is never ambiguous mid-workflow. Deliberately always visible,
                not just on hover, since the whole point is a mistake here
                (clicking through a form vs. picking it) matters. */}
            {!awaitingFirstFrame && !liveError && (
              <div
                className={`absolute top-2 left-2 z-10 flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold pointer-events-none transition-colors ${
                  mode === 'pick'
                    ? 'bg-accent text-white shadow-glow-accent'
                    : 'bg-surface/90 text-muted border border-border'
                }`}
              >
                {mode === 'pick' ? <AdsClick sx={{ fontSize: 13 }} /> : <PanTool sx={{ fontSize: 13 }} />}
                {mode === 'pick' ? 'Pick mode — click an element' : 'Browse mode'}
              </div>
            )}
            <canvas
              ref={canvasRef}
              tabIndex={0}
              onClick={handleCanvasClick}
              onMouseMove={handleCanvasMouseMove}
              onWheel={handleCanvasWheel}
              onKeyDown={handleCanvasKeyDown}
              className={`max-w-full max-h-full outline-none transition-shadow duration-150 ${
                mode === 'pick' ? 'ring-2 ring-accent shadow-glow-accent' : ''
              } ${mode === 'pick' ? 'cursor-crosshair' : 'cursor-default'} ${picking ? 'opacity-70' : ''}`}
              style={mode === 'pick' ? undefined : { boxShadow: '0 0 0 1px rgba(255,255,255,0.06)' }}
            />
          </>
        )}
      </div>
    </div>
  )
}
