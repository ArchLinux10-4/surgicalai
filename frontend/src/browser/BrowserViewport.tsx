import React, { useEffect, useRef, useState } from 'react'
import { buttonName, modifierMask } from './input'
import { setFramePainter } from './framePump'
import { useBrowserStore } from './browserStore'
import { QUICK_URLS, VIEWPORTS, browserStrings } from './strings'

interface Props {
  box: { w: number; h: number; dpr: number } | null
  onBox: (box: { w: number; h: number; dpr: number }) => void
  send: (msg: object) => void
  onNavigate: (url: string) => void
  onReload: () => void
}

export function BrowserViewport({ box, onBox, send, onNavigate, onReload }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const painted = useRef(false)
  const lastBox = useRef<{ w: number; h: number; dpr: number } | null>(null)
  const [hasFrame, setHasFrame] = useState(false)
  const hover = useBrowserStore((s) => s.hover)
  const pickMode = useBrowserStore((s) => s.pickMode)
  const cursor = useBrowserStore((s) => s.cursor)
  const presetId = useBrowserStore((s) => s.presetId)
  const viewport = useBrowserStore((s) => s.viewport)
  const displayUrl = useBrowserStore((s) => s.displayUrl)
  const starting = useBrowserStore((s) => s.starting)
  const lastError = useBrowserStore((s) => s.lastError)
  const navError = useBrowserStore((s) => s.navError)
  const connected = useBrowserStore((s) => s.connected)
  const preset = VIEWPORTS.find((item) => item.id === presetId) || VIEWPORTS[0]
  const fixed = preset.w > 0

  useEffect(() => {
    if (fixed) {
      onBox({ w: preset.w, h: preset.h, dpr: 1 })
      return
    }
    const el = wrapRef.current
    if (!el) return
    const publish = () => {
      const rect = el.getBoundingClientRect()
      const next = {
        w: Math.max(320, Math.round(rect.width)),
        h: Math.max(240, Math.round(rect.height)),
        dpr: window.devicePixelRatio || 1,
      }
      const prev = lastBox.current
      if (
        prev
        && Math.abs(prev.w - next.w) < 16
        && Math.abs(prev.h - next.h) < 16
        && prev.dpr === next.dpr
      ) return
      lastBox.current = next
      onBox(next)
    }
    publish()
    const observer = new ResizeObserver(publish)
    observer.observe(el)
    return () => observer.disconnect()
  }, [fixed, preset.w, preset.h, onBox])

  useEffect(() => {
    const canvas = canvasRef.current
    const stage = stageRef.current
    const wrap = wrapRef.current
    setFramePainter((bmp) => {
      if (!canvas || !stage) {
        bmp.close()
        return
      }
      if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
        canvas.width = bmp.width
        canvas.height = bmp.height
      }
      const dpr = window.devicePixelRatio || 1
      const wrapW = wrap?.clientWidth || bmp.width / dpr
      const wrapH = wrap?.clientHeight || bmp.height / dpr
      const scale = Math.min((wrapW / bmp.width) * dpr, (wrapH / bmp.height) * dpr)
      const cssW = Math.max(1, (bmp.width / dpr) * scale)
      const cssH = Math.max(1, (bmp.height / dpr) * scale)
      canvas.style.width = `${cssW}px`
      canvas.style.height = `${cssH}px`
      stage.style.width = `${cssW}px`
      stage.style.height = `${cssH}px`
      const ctx = canvas.getContext('2d')
      if (!ctx) {
        bmp.close()
        return
      }
      ctx.imageSmoothingQuality = 'high'
      ctx.drawImage(bmp, 0, 0)
      bmp.close()
      if (!painted.current) {
        painted.current = true
        setHasFrame(true)
      }
    }, () => {
      const ctx = canvas?.getContext('2d')
      if (canvas && ctx) ctx.clearRect(0, 0, canvas.width, canvas.height)
    })
    return () => setFramePainter(null)
  }, [])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault()
      const rect = canvas.getBoundingClientRect()
      if (!rect.width || !rect.height) return
      const width = useBrowserStore.getState().viewport.w || rect.width
      const height = useBrowserStore.getState().viewport.h || rect.height
      send({
        t: 'mouse',
        kind: 'mouseWheel',
        x: ((ev.clientX - rect.left) / rect.width) * width,
        y: ((ev.clientY - rect.top) / rect.height) * height,
        dx: ev.deltaX,
        dy: ev.deltaY,
        modifiers: (ev.altKey ? 1 : 0) | (ev.ctrlKey ? 2 : 0) | (ev.metaKey ? 4 : 0) | (ev.shiftKey ? 8 : 0),
      })
    }
    canvas.addEventListener('wheel', onWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', onWheel)
  }, [send, displayUrl])

  const scalePoint = (ev: React.PointerEvent | React.WheelEvent) => {
    const canvas = canvasRef.current
    if (!canvas) return null
    const rect = canvas.getBoundingClientRect()
    const width = viewport.w || rect.width
    const height = viewport.h || rect.height
    return {
      x: ((ev.clientX - rect.left) / rect.width) * width,
      y: ((ev.clientY - rect.top) / rect.height) * height,
    }
  }

  const onKeyDown = (ev: React.KeyboardEvent) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'l') {
      ev.preventDefault()
      useBrowserStore.getState().requestAddressFocus()
      return
    }
    if ((ev.metaKey || ev.ctrlKey) && !ev.shiftKey && ev.key.toLowerCase() === 'r') {
      ev.preventDefault()
      onReload()
      return
    }
    if (ev.key === 'Escape' && pickMode) {
      ev.preventDefault()
      send({ t: 'pick', on: false })
      return
    }
    const text = ev.key.length === 1 ? ev.key : ''
    const mask = modifierMask(ev)
    send({ t: 'key', kind: 'keyDown', key: ev.key, code: ev.code, text, modifiers: mask, keyCode: ev.keyCode })
    if (text && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      send({ t: 'key', kind: 'char', key: ev.key, code: ev.code, text, modifiers: mask, keyCode: ev.keyCode })
    }
    ev.preventDefault()
  }

  const showCanvas = hasFrame || !!displayUrl
  const installError = lastError && /playwright|chromium|not installed|docker-rebuild/i.test(lastError)
  const hoverStyle = hover && viewport.w
    ? {
        left: `${(hover.rect.x / viewport.w) * 100}%`,
        top: `${(hover.rect.y / viewport.h) * 100}%`,
        width: `${(hover.rect.width / viewport.w) * 100}%`,
        height: `${(hover.rect.height / viewport.h) * 100}%`,
      }
    : null

  return (
    <div ref={wrapRef} className="relative flex flex-1 min-h-0 min-w-0 items-center justify-center bg-[#0c0e12]">
      <div
        className="relative flex h-full w-full items-center justify-center"
        style={fixed
          ? { width: preset.w, height: preset.h, maxWidth: '100%', maxHeight: '100%' }
          : undefined}
      >
        <div ref={stageRef} className={`relative ${showCanvas ? (hasFrame ? '' : 'h-full w-full') : 'hidden'}`}>
          <canvas
            ref={canvasRef}
            tabIndex={0}
            className={`block bg-white outline-none focus-visible:ring-2 focus-visible:ring-accent ${hasFrame ? '' : 'h-full w-full'}`}
            style={{ cursor: pickMode ? 'crosshair' : cursor }}
            onPointerDown={(ev) => {
              canvasRef.current?.focus()
              const point = scalePoint(ev)
              if (!point) return
              send({ t: 'mouse', kind: 'mousePressed', ...point, button: buttonName(ev.button), buttons: ev.buttons, clickCount: ev.detail || 1, modifiers: modifierMask(ev) })
            }}
            onPointerUp={(ev) => {
              const point = scalePoint(ev)
              if (!point) return
              send({ t: 'mouse', kind: 'mouseReleased', ...point, button: buttonName(ev.button), buttons: ev.buttons, clickCount: ev.detail || 1, modifiers: modifierMask(ev) })
            }}
            onPointerMove={(ev) => {
              const point = scalePoint(ev)
              if (!point) return
              send({ t: 'mouse', kind: 'mouseMoved', ...point, button: 'none', buttons: ev.buttons, modifiers: modifierMask(ev) })
            }}
            onContextMenu={(ev) => ev.preventDefault()}
            onKeyDown={onKeyDown}
            onKeyUp={(ev) => {
              send({ t: 'key', kind: 'keyUp', key: ev.key, code: ev.code, text: '', modifiers: modifierMask(ev), keyCode: ev.keyCode })
            }}
            onPaste={(ev) => {
              const text = ev.clipboardData.getData('text')
              if (!text) return
              ev.preventDefault()
              send({ t: 'text', text })
            }}
          />
          {hoverStyle && (
            <div className="pointer-events-none absolute border-2 border-[#5b8cff] bg-[#5b8cff]/10" style={hoverStyle} />
          )}
        </div>
        {!showCanvas && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center">
            <p className="text-sm font-semibold text-ink">
              {installError ? browserStrings.missingTitle : starting ? browserStrings.starting : browserStrings.emptyTitle}
            </p>
            <p className="max-w-md text-[12px] text-muted">
              {installError ? browserStrings.missingBody : !connected && !starting ? browserStrings.connecting : browserStrings.emptyBody}
            </p>
            {!installError && !starting && (
              <div className="flex flex-wrap items-center justify-center gap-2">
                {QUICK_URLS.map((url) => (
                  <button
                    key={url}
                    type="button"
                    onClick={() => onNavigate(url)}
                    className="rounded-lg border border-border bg-surface px-2.5 py-1 text-[12px] text-ink hover:bg-overlay"
                  >
                    {url.replace('http://', '')}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {navError && (
          <div className="absolute left-1/2 top-4 z-10 w-[min(420px,90%)] -translate-x-1/2 rounded-xl border border-danger/40 bg-surface px-3 py-2 text-[12px] text-ink shadow-lg">
            <div className="font-semibold">{browserStrings.navErrorTitle}</div>
            <p className="mt-1 text-muted">{navError}</p>
            <button
              type="button"
              className="mt-2 rounded-md bg-accent px-2 py-1 text-[11px] font-semibold text-white"
              onClick={() => displayUrl && onNavigate(displayUrl)}
            >
              {browserStrings.retry}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
