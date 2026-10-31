import { useEffect, useRef } from 'react'
import { api } from '../api/client'
import { useAppStore, type PickedElementRef } from '../stores/appStore'
import {
  browserApi,
  browserSocketUrl,
  type BrowserStatus,
  type PickedNode,
  type SourceHit,
} from './browserClient'
import { parseFrame } from './frame'
import { clearPaintedFrame, createFrameGate, nextBinaryFrame, paintFrame, takeBinaryFrame } from './framePump'
import { pushConsole, useBrowserStore } from './browserStore'

const frameGate = createFrameGate()
let hoverQueued = false
let hoverPending: { rect: { x: number; y: number; width: number; height: number }; tag: string } | null = null

function queueHover(rect: { x: number; y: number; width: number; height: number }, tag: string) {
  hoverPending = { rect, tag }
  if (hoverQueued) return
  hoverQueued = true
  requestAnimationFrame(() => {
    hoverQueued = false
    const next = hoverPending
    hoverPending = null
    if (next) useBrowserStore.setState({ hover: next })
  })
}

async function decodeFrame(buf: ArrayBuffer) {
  let current: ArrayBuffer | null = buf
  while (current) {
    try {
      const parsed = parseFrame(current)
      const bytes = parsed.jpeg
      const copy = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)
      const bitmap = await createImageBitmap(new Blob([copy as ArrayBuffer], { type: 'image/jpeg' }))
      paintFrame(bitmap)
    } catch { /* a bad frame is dropped; the next one paints */ }
    current = nextBinaryFrame(frameGate)
  }
}

export function applyStatus(status: BrowserStatus) {
  useBrowserStore.setState({
    displayUrl: status.display_url || '',
    title: status.title || '',
    favicon: status.favicon || '',
    loading: !!status.loading,
    canGoBack: !!status.can_go_back,
    canGoForward: !!status.can_go_forward,
    pickMode: !!status.pick_mode,
    lastError: status.last_error,
    tabs: status.tabs || [],
    activeTab: status.active_tab ?? 0,
    sessionUsed: status.running || useBrowserStore.getState().sessionUsed,
  })
  if (status.display_url) {
    useBrowserStore.getState().rememberUrl(useAppStore.getState().activeSessions, status.display_url)
  }
}

function toRef(node: PickedNode, source?: SourceHit[]): PickedElementRef {
  return {
    id: node.token || `${Date.now()}`,
    tag: node.tag,
    elId: node.el_id,
    className: node.classes?.length ? node.classes.join(' ') : null,
    text: node.text,
    outerHTML: node.outer_html,
    pageUrl: node.page_url || null,
    cssPath: node.css_path,
    xpath: node.xpath,
    rect: node.rect,
    computed: node.computed,
    react: node.react,
    screenshot: node.screenshot_png_b64 ? `data:image/png;base64,${node.screenshot_png_b64}` : undefined,
    source,
    token: node.token,
  }
}

async function onPick(node: PickedNode) {
  const ref = toRef(node)
  useAppStore.getState().addPickedElement(ref)
  useBrowserStore.setState({
    lastPick: node,
    inspector: { node, edits: {}, source: [] },
    pickMode: node.shift_key ? useBrowserStore.getState().pickMode : false,
  })
  if (!node.shift_key) browserApi.pickMode(false).catch(() => {})
  const sessionId = useAppStore.getState().activeSessions
  if (!sessionId) return
  browserApi.locate(sessionId, node).then((res) => {
    const source = res.candidates || []
    useAppStore.getState().updatePickedElement(ref.id, { source })
    const inspector = useBrowserStore.getState().inspector
    if (inspector && inspector.node.token === node.token) {
      useBrowserStore.setState({ inspector: { ...inspector, source } })
    }
  }).catch(() => {})
  if (useBrowserStore.getState().attachShot && node.screenshot_png_b64) {
    try {
      const file = await api.sessionFiles.upload(sessionId, {
        filename: `picked-${node.tag}-${Date.now()}.png`,
        content: '',
        base64_data: `data:image/png;base64,${node.screenshot_png_b64}`,
        file_type: 'image',
      })
      useAppStore.getState().addSessionFile(file)
      useAppStore.getState().updatePickedElement(ref.id, { screenshotFileId: file.id })
    } catch { /* the chip still carries the thumbnail */ }
  }
}

async function onSocketData(data: string | ArrayBuffer) {
  if (typeof data === 'string') {
    let event: any
    try { event = JSON.parse(data) } catch { return }
    if (event.type === 'hello' && event.status) applyStatus(event.status)
    else if (event.type === 'nav') {
      useBrowserStore.setState({
        displayUrl: event.display_url || '',
        title: event.title || '',
        favicon: event.favicon || '',
        loading: !!event.loading,
        canGoBack: !!event.can_go_back,
        canGoForward: !!event.can_go_forward,
        navError: event.loading ? useBrowserStore.getState().navError : null,
      })
      if (event.display_url) {
        useBrowserStore.getState().rememberUrl(useAppStore.getState().activeSessions, event.display_url)
      }
    } else if (event.type === 'nav_error') {
      useBrowserStore.setState({ navError: event.message || 'This page did not open', loading: false })
    } else if (event.type === 'error') {
      useBrowserStore.setState({ lastError: event.message || 'Browser error' })
    } else if (event.type === 'cursor') {
      useBrowserStore.setState({ cursor: event.value || 'auto' })
    } else if (event.type === 'hover' && event.rect) {
      queueHover(event.rect, event.tag || '')
    } else if (event.type === 'console' && event.entry) {
      pushConsole(event.entry)
    } else if (event.type === 'pick' && event.node) {
      await onPick(event.node)
    } else if (event.type === 'pick_mode') {
      useBrowserStore.setState({ pickMode: !!event.on, hover: event.on ? useBrowserStore.getState().hover : null })
    } else if (event.type === 'tabs') {
      const prev = useBrowserStore.getState().activeTab
      const next = typeof event.active === 'number' ? event.active : prev
      if (next !== prev) clearPaintedFrame()
      useBrowserStore.setState({ tabs: event.tabs || [], activeTab: next })
    }
    return
  }
  const start = takeBinaryFrame(frameGate, data)
  if (start) void decodeFrame(start)
}

export function useBrowserSocket(open: boolean, box: { w: number; h: number; dpr: number } | null) {
  const sendRef = useRef<(msg: object) => void>(() => {})
  const boxRef = useRef(box)
  boxRef.current = box
  const ready = open && !!box

  useEffect(() => {
    if (!ready) return
    let ws: WebSocket | null = null
    let stopped = false
    let delay = 500
    let timer = 0
    let pending: object | null = null
    const connect = () => {
      const size = boxRef.current
      if (!size) return
      ws = new WebSocket(browserSocketUrl(size.w, size.h, size.dpr))
      ws.binaryType = 'arraybuffer'
      ws.onopen = () => {
        delay = 500
        useBrowserStore.setState({ connected: true })
        const queued = pending
        pending = null
        if (queued) ws?.send(JSON.stringify(queued))
      }
      ws.onmessage = (ev) => { void onSocketData(ev.data) }
      ws.onclose = () => {
        useBrowserStore.setState({ connected: false })
        if (!stopped) {
          timer = window.setTimeout(connect, delay)
          delay = Math.min(5000, delay * 2)
        }
      }
    }
    connect()
    sendRef.current = (msg) => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg))
      else pending = msg
    }
    return () => {
      stopped = true
      window.clearTimeout(timer)
      sendRef.current = () => {}
      ws?.close()
    }
  }, [ready])

  useEffect(() => {
    if (!ready || !box) return
    useBrowserStore.setState({ viewport: { w: box.w, h: box.h } })
    sendRef.current({ t: 'resize', w: box.w, h: box.h, dpr: box.dpr })
  }, [ready, box?.w, box?.h, box?.dpr])

  return { send: (msg: object) => sendRef.current(msg) }
}
