import React, { useEffect, useRef, useState } from 'react'
import { useThemeStore } from '../stores/themeStore'
import { Copy, Check, Download } from 'lucide-react'

interface Props {
  chart: string
}

// Module-level singleton loader — only fetches the script once across all instances
let _mermaidLoading: Promise<void> | null = null

async function loadMermaid(): Promise<any> {
  if ((window as any).__mermaidLoaded) return (window as any).mermaid
  if (_mermaidLoading) { await _mermaidLoading; return (window as any).mermaid }
  _mermaidLoading = new Promise<void>((resolve, reject) => {
    const s = document.createElement('script')
    s.src = 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js'
    s.onload = () => { (window as any).__mermaidLoaded = true; resolve() }
    s.onerror = () => reject(new Error('Failed to load mermaid'))
    document.head.appendChild(s)
  })
  await _mermaidLoading
  return (window as any).mermaid
}

// Monotonically increasing counter — guarantees globally unique render IDs
let _renderIdN = 0

export function MermaidDiagram({ chart }: Props) {
  const theme = useThemeStore(s => s.theme)
  const ref = useRef<HTMLDivElement>(null)
  const [err, setErr] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let cancelled = false
    setErr(null)
    setReady(false)
    if (!ref.current) return

    const go = async () => {
      try {
        const m = await loadMermaid()

        m.initialize({
          startOnLoad: false,
          theme: theme === 'dark' ? 'dark' : 'default',
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
          fontSize: 13,
          sequence: { actorMargin: 60, mirrorActors: false },
          themeVariables: theme === 'dark' ? {
            primaryColor: '#1c2128', primaryTextColor: '#e6edf3',
            primaryBorderColor: '#30363d', lineColor: '#8b949e',
            secondaryColor: '#161b22', background: 'transparent',
            actorBkg: '#1c2128', actorBorder: '#30363d',
            actorTextColor: '#e6edf3', signalColor: '#8b949e',
            signalTextColor: '#c9d1d9', noteBkgColor: '#1c2128',
            noteBorderColor: '#30363d', noteTextColor: '#c9d1d9',
            activationBorderColor: '#58a6ff', activationBkgColor: '#1f6feb33',
            labelBoxBkgColor: '#161b22', labelBoxBorderColor: '#30363d',
            labelTextColor: '#e6edf3', loopTextColor: '#8b949e',
          } : {
            primaryColor: '#f6f8fa', primaryTextColor: '#1f2328',
            primaryBorderColor: '#d0d7de', lineColor: '#656d76',
            secondaryColor: '#eaeef2', background: 'transparent',
            actorBkg: '#f6f8fa', actorBorder: '#d0d7de',
            actorTextColor: '#1f2328', signalColor: '#656d76',
            signalTextColor: '#1f2328', noteBkgColor: '#faeeda',
            noteBorderColor: '#d0d7de', noteTextColor: '#1f2328',
            activationBorderColor: '#0969da', activationBkgColor: '#0969da1a',
            labelBoxBkgColor: '#f6f8fa', labelBoxBorderColor: '#d0d7de',
            labelTextColor: '#1f2328',
          }
        })

        // Use a fresh unique ID on every render call to avoid mermaid's
        // "id already registered" error when the chart or theme changes.
        const renderId = `mermaid-svg-${++_renderIdN}`

        // Clear any previous SVG before injecting the new one
        if (ref.current) ref.current.innerHTML = ''

        const { svg } = await m.render(renderId, chart.trim())

        if (cancelled || !ref.current) return

        ref.current.innerHTML = svg

        // Make SVG fill its container width
        const svgEl = ref.current.querySelector('svg')
        if (svgEl) {
          svgEl.removeAttribute('height')
          svgEl.style.width = '100%'
          svgEl.style.maxWidth = '100%'
        }
        setReady(true)
      } catch (e: any) {
        if (!cancelled) setErr(e?.message || 'Render failed')
      }
    }

    go()
    return () => { cancelled = true }
  }, [chart, theme])

  const copy = () => {
    navigator.clipboard.writeText(chart)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const download = () => {
    const svg = ref.current?.querySelector('svg')
    if (!svg) return
    const url = URL.createObjectURL(new Blob([svg.outerHTML], { type: 'image/svg+xml' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'diagram.svg'
    a.click()
    URL.revokeObjectURL(url)
  }

  // Fallback: render raw source in a plain block — nothing is lost
  if (err) return (
    <div className="my-3 rounded-xl overflow-hidden border border-danger/40 bg-danger/5">
      <div className="px-3 py-2 bg-danger/10 border-b border-danger/30 flex items-center gap-2">
        <span className="text-[11px] font-semibold text-danger">Diagram render error</span>
        <span className="text-[11px] text-muted ml-auto truncate max-w-xs">{err}</span>
      </div>
      <pre className="p-4 text-[12px] font-mono text-muted overflow-x-auto whitespace-pre">{chart}</pre>
    </div>
  )

  return (
    <div className="my-3 rounded-xl overflow-hidden border border-border/60 bg-base">

      {/* Toolbar — same style as CodeBlock */}
      <div className="flex items-center justify-between px-3.5 py-2 bg-surface/80 border-b border-border/60">
        <div className="flex items-center gap-2">
          <div className="flex gap-1.5">
            <span className="w-3 h-3 rounded-full bg-red-500/60" />
            <span className="w-3 h-3 rounded-full bg-yellow-500/60" />
            <span className="w-3 h-3 rounded-full bg-green-500/60" />
          </div>
          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded border text-purple bg-purple/10 border-purple/20">
            DIAGRAM
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={copy}
            className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-muted hover:text-ink hover:bg-overlay/60 transition-colors"
          >
            {copied
              ? <><Check size={12} className="text-success" /><span className="text-success">Copied</span></>
              : <><Copy size={12} /><span>Copy source</span></>}
          </button>
          {ready && (
            <button
              onClick={download}
              className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-muted hover:text-ink hover:bg-overlay/60 transition-colors"
            >
              <Download size={12} /><span>SVG</span>
            </button>
          )}
        </div>
      </div>

      {/* Diagram output */}
      <div
        ref={ref}
        className="p-4 overflow-x-auto"
        style={{ minHeight: ready ? undefined : '60px' }}
      />

      {!ready && !err && (
        <div className="pb-4 text-center">
          <span className="text-[12px] text-muted animate-pulse">Rendering diagram...</span>
        </div>
      )}
    </div>
  )
}
