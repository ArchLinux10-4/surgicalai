import React, { useState, useEffect, useRef } from 'react'
import { Eye, EyeOff, RefreshCw, Maximize2, Minimize2 } from 'lucide-react'

/* ─── Public API ───────────────────────────────────────────────── */
export function isVisualFile(filename: string): boolean {
  return /\.(tsx|jsx|html|htm)$/i.test(filename)
}

interface LivePreviewProps {
  code: string
  filename: string
  modifiedCode?: string
}

/* ─── Helpers ──────────────────────────────────────────────────── */
let _babelReady: Promise<void> | null = null
function ensureBabel(): Promise<void> {
  if ((window as any).Babel) return Promise.resolve()
  if (_babelReady) return _babelReady
  _babelReady = new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = 'https://unpkg.com/@babel/standalone@7/babel.min.js'
    s.crossOrigin = 'anonymous'
    s.onload = () => (window as any).Babel ? resolve() : reject(new Error('Babel not found after load'))
    s.onerror = () => { _babelReady = null; reject(new Error('Could not load Babel from CDN')) }
    document.head.appendChild(s)
  })
  return _babelReady
}

function detectComponent(code: string): string {
  return (
    code.match(/export\s+default\s+(?:function|class)\s+([A-Z]\w*)/)?.[1] ||
    code.match(/export\s+default\s+([A-Z]\w*)/)?.[1] ||
    [...code.matchAll(/(?:function|const)\s+([A-Z][a-zA-Z0-9]*)\s*[=(]/g)].pop()?.[1] ||
    'App'
  )
}

/** Wrap every return() block in a <> fragment — fixes adjacent JSX element errors */
function wrapReturnsInFragments(code: string): string {
  const lines = code.split('\n')
  const result: string[] = []
  let i = 0
  while (i < lines.length) {
    const m = lines[i].match(/^(\s*)return\s*\(\s*$/)
    if (m) {
      const indent = m[1]
      result.push(lines[i])
      result.push(`${indent}  <>`)
      i++
      while (i < lines.length) {
        if (/^\s*\);\s*$/.test(lines[i])) {
          result.push(`${indent}  </>`)
          result.push(lines[i])
          i++
          break
        }
        result.push(lines[i])
        i++
      }
    } else {
      result.push(lines[i])
      i++
    }
  }
  return result.join('\n')
}

/** Strip relative imports (can't load ../stores/x in sandbox) and replace with stubs */
function stubRelativeImports(code: string): string {
  return code.replace(
    /^import\s+(.+?)\s+from\s+['"](\.[^'"]+)['"]\s*;?\s*$/gm,
    (_, spec) => {
      const names = spec
        .replace(/\*\s+as\s+(\w+)/, '$1')
        .replace(/[{}]/g, '')
        .split(',')
        .map((s: string) => s.trim().split(/\s+as\s+/).pop()?.trim())
        .filter(Boolean) as string[]
      return names.map(n => `var ${n} = function(){return null;};`).join(' ')
    }
  )
}

function buildHtml(compiledJs: string, component: string): string {
  // The compiled JS uses require() for all package imports.
  // We load React + ReactDOM as UMD globals from CDN, then provide a tiny shim.
  const shim = `
<script>
var module = { exports: {} }; var exports = module.exports;
function require(id) {
  var u = id.toLowerCase();
  if (u === 'react') return Object.assign({ default: React, __esModule: true }, React);
  if (u === 'react-dom' || u === 'react-dom/client') return Object.assign({ default: ReactDOM, createRoot: ReactDOM.createRoot, __esModule: true }, ReactDOM);
  // Lucide + anything else: return a Proxy of no-op components
  return new Proxy({}, { get: function(_, k) {
    if (k === '__esModule' || k === 'default') return true;
    return function(){ return null; };
  }});
}
<\/script>`

  return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>*{box-sizing:border-box}body{margin:0;padding:12px;font-family:system-ui,sans-serif;background:#fff;color:#111}</style>
<script src="https://unpkg.com/react@18/umd/react.development.js"><\/script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"><\/script>
${shim}
</head>
<body>
<div id="root"></div>
<div id="__err" style="display:none;color:#c00;background:#fee;padding:8px;border-radius:4px;font-size:12px;white-space:pre-wrap;margin:4px"></div>
<script>
window.onerror = function(msg,_,__,___,e){ var el=document.getElementById('__err'); el.style.display='block'; el.textContent=(e&&e.message)||msg; };
try {
${compiledJs}

var __C = module.exports['default'] || module.exports['${component}'];
if (typeof __C === 'function') {
  ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(__C));
} else {
  document.getElementById('root').textContent = 'No renderable component found (looked for: ${component})';
}
} catch(e) { var el=document.getElementById('__err'); el.style.display='block'; el.textContent=e.message||String(e); }
<\/script>
</body>
</html>`
}

/* ─── Component ────────────────────────────────────────────────── */
export function LivePreview({ code, filename, modifiedCode }: LivePreviewProps) {
  const [srcdoc, setSrcdoc] = useState('')
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(false)
  const [key, setKey] = useState(0)
  const src = modifiedCode ?? code

  useEffect(() => {
    setError('')
    setSrcdoc('')

    // Plain HTML — pass through as-is
    if (/\.html?$/i.test(filename)) {
      setSrcdoc(src)
      return
    }

    let cancelled = false
    ensureBabel()
      .then(() => {
        if (cancelled) return
        const B = (window as any).Babel
        let prepared = stubRelativeImports(src)
        let compiled: string
        try {
          compiled = B.transform(prepared, {
            presets: ['react', ['typescript', { isTSX: true, allExtensions: true }]],
            plugins: ['transform-modules-commonjs'],
            filename,
          }).code
        } catch (e1: any) {
          // Auto-fix: wrap every return() block in <> fragment and retry
          // Handles both "Adjacent JSX elements" and related parse errors
          const wrapped = wrapReturnsInFragments(prepared)
          try {
            compiled = B.transform(wrapped, {
              presets: ['react', ['typescript', { isTSX: true, allExtensions: true }]],
              plugins: ['transform-modules-commonjs'],
              filename,
            }).code
          } catch {
            throw e1  // both attempts failed — show original error
          }
        }
        const component = detectComponent(src)
        setSrcdoc(buildHtml(compiled, component))
      })
      .catch((e: any) => {
        if (!cancelled) setError(e.message || 'Preview failed')
      })

    return () => { cancelled = true }
  }, [src, filename])

  return (
    <div className={`flex flex-col rounded-lg border border-border overflow-hidden ${expanded ? 'fixed inset-4 z-50 bg-base' : ''}`}>
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-surface border-b border-border">
        <span className="text-[11px] text-muted font-mono truncate">👁 {filename}</span>
        <div className="flex gap-1">
          <button onClick={() => setKey(k => k + 1)} className="p-1 rounded hover:bg-hover text-muted hover:text-fg" title="Reload">
            <RefreshCw size={11} />
          </button>
          <button onClick={() => setExpanded(e => !e)} className="p-1 rounded hover:bg-hover text-muted hover:text-fg" title={expanded ? 'Collapse' : 'Expand'}>
            {expanded ? <Minimize2 size={11} /> : <Maximize2 size={11} />}
          </button>
        </div>
      </div>

      {error ? (
        <div className="p-3 text-xs text-red-400 bg-red-500/5 font-mono whitespace-pre-wrap">{error}</div>
      ) : srcdoc ? (
        <iframe
          key={key}
          sandbox="allow-scripts"
          srcDoc={srcdoc}
          className={`w-full bg-white border-none ${expanded ? 'flex-1' : 'h-[440px]'}`}
          title={`Preview: ${filename}`}
        />
      ) : (
        <div className={`flex items-center justify-center text-xs text-muted ${expanded ? 'flex-1' : 'h-[440px]'}`}>
          Compiling preview…
        </div>
      )}
    </div>
  )
}
