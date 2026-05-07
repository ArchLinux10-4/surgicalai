import React, { useRef, useState, useEffect, useCallback } from 'react'
import { Eye, EyeOff, RefreshCw, AlertTriangle, Maximize2, Minimize2, Loader2 } from 'lucide-react'

/* ─── Types ────────────────────────────────────────────────────── */
interface LivePreviewProps {
  code: string
  filename: string
  modifiedCode?: string
}

export function isVisualFile(filename: string): boolean {
  return /\.(tsx|jsx|html|htm|css|svg)$/i.test(filename)
}

/* ─── Babel loader — runs ONCE on the main page, cached ──────── */
let _babelPromise: Promise<any> | null = null

function loadBabel(): Promise<any> {
  if ((window as any).Babel) return Promise.resolve((window as any).Babel)
  if (_babelPromise) return _babelPromise
  _babelPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = 'https://unpkg.com/@babel/standalone@7/babel.min.js'
    s.crossOrigin = 'anonymous'
    s.onload = () => {
      const B = (window as any).Babel
      if (B) resolve(B)
      else { _babelPromise = null; reject(new Error('Babel loaded but window.Babel missing')) }
    }
    s.onerror = () => { _babelPromise = null; reject(new Error('Failed to load Babel from CDN — check your network.')) }
    document.head.appendChild(s)
  })
  return _babelPromise
}

/* ─── JSX → plain JS compilation (runs on the MAIN page) ─────── */
async function compileJsx(rawCode: string, filename: string): Promise<{ js: string; component: string | null; error?: string }> {
  const isHtml = /\.(html|htm)$/i.test(filename)
  if (isHtml) return { js: rawCode, component: null }

  // Strip imports, exports, TS types — same logic as before
  let cleaned = rawCode
    .replace(/^import\s+.*?from\s+['"][^'"]+['"]\s*;?\s*$/gm, '')
    .replace(/^export\s+default\s+/gm, '')
    .replace(/^export\s+(?:const|function|class|type|interface)\s+/gm, (m) => m.replace('export ', ''))
    .replace(/^type\s+\w+\s*=[\s\S]*?(?=\n(?:const|function|class|export|interface|type|\s*$))/gm, '')
    .replace(/^interface\s+\w+\s*\{[\s\S]*?\n\}/gm, '')

  // Detect component name
  const defaultMatch = rawCode.match(/export\s+default\s+(?:function\s+)?(\w+)/)
  const fnMatches = [...rawCode.matchAll(/(?:function|const)\s+([A-Z][a-zA-Z0-9]*)\s*[=(]/g)]
  const componentName = defaultMatch?.[1] || (fnMatches.length > 0 ? fnMatches[fnMatches.length - 1][1] : null)

  const mountCall = componentName
    ? `\ntry {\n  var __root = ReactDOM.createRoot(document.getElementById('root'));\n  __root.render(React.createElement(${componentName}));\n} catch(__e) {\n  var __b = document.getElementById('error-banner');\n  if (__b) { __b.style.display='block'; __b.textContent = 'Render error: ' + (__e && __e.message ? __e.message : String(__e)); }\n}`
    : '\n// Could not detect a React component to render'

  const fullSource = `
var { useState, useEffect, useRef, useCallback, useMemo, useContext, createContext, forwardRef, memo, Fragment } = React;
${cleaned}
${mountCall}
`

  try {
    const Babel = await loadBabel()
    const result = Babel.transform(fullSource, {
      presets: ['react', ['typescript', { isTSX: true, allExtensions: true }]],
      filename: filename || 'component.tsx',
    })
    return { js: result.code, component: componentName }
  } catch (e: any) {
    return { js: '', component: null, error: e.message || String(e) }
  }
}

/* ─── Mock block — same as before, just extracted for clarity ──── */
const MOCK_BLOCK = `
// react-router-dom mocks
var useNavigate = function() { return function() {}; };
var useLocation = function() { return { pathname: '/preview', search: '', hash: '', state: null }; };
var useParams = function() { return {}; };
var useSearchParams = function() { return [new URLSearchParams(), function() {}]; };
var Link = function(p) { return React.createElement('a', { href: p.to || '#', onClick: function(e){ e.preventDefault(); } }, p.children); };
var Navigate = function() { return null; };
var Outlet = function() { return null; };
var BrowserRouter = function(p) { return React.createElement(React.Fragment, null, p.children); };
var Routes = function(p) { return React.createElement(React.Fragment, null, p.children); };
var Route = function() { return null; };

// Store/api/toast mocks
var create = function() { return function() { return {}; }; };
var api = {};
var toast = { success: function() {}, error: function() {}, info: function() {}, warning: function() {} };
var apiClient = {
  get: function() { return Promise.reject(new Error('preview-offline')); },
  post: function() { return Promise.reject(new Error('preview-offline')); },
  put: function() { return Promise.reject(new Error('preview-offline')); },
  patch: function() { return Promise.reject(new Error('preview-offline')); },
  delete: function() { return Promise.reject(new Error('preview-offline')); },
};
var useAuthStore = function() { return { login: function() {}, logout: function() {}, currentUser: { username: 'preview', role: 'admin' }, token: null, isAuthenticated: true }; };
var useAppStore = function() { return { sessions: [], currentSessionId: null, setCurrentSession: function() {}, createSession: function() {}, deleteSession: function() {} }; };

// Lucide icon mock factory
var __icon = function(name) {
  return function(props) {
    var sz = (props && props.size) || 16;
    return React.createElement('span', {
      style: { display:'inline-block', width:sz, height:sz, background:'currentColor',
        borderRadius:2, opacity:0.6, verticalAlign:'middle', flexShrink:0 }, title:name });
  };
};
var Eye=__icon('Eye'),EyeOff=__icon('EyeOff'),Shield=__icon('Shield'),Zap=__icon('Zap'),Lock=__icon('Lock'),Code=__icon('Code');
var User=__icon('User'),Key=__icon('Key'),AlertTriangle=__icon('AlertTriangle'),Check=__icon('Check'),X=__icon('X');
var ChevronRight=__icon('ChevronRight'),ChevronDown=__icon('ChevronDown'),ChevronUp=__icon('ChevronUp');
var ArrowRight=__icon('ArrowRight'),ArrowLeft=__icon('ArrowLeft'),Settings=__icon('Settings');
var LogOut=__icon('LogOut'),LogIn=__icon('LogIn'),Plus=__icon('Plus'),Trash=__icon('Trash'),Edit=__icon('Edit');
var Search=__icon('Search'),Menu=__icon('Menu'),Home=__icon('Home'),File=__icon('File'),Folder=__icon('Folder');
var Save=__icon('Save'),Download=__icon('Download'),Upload=__icon('Upload'),Copy=__icon('Copy');
var ExternalLink=__icon('ExternalLink'),Info=__icon('Info'),CheckCircle=__icon('CheckCircle'),XCircle=__icon('XCircle');
var Circle=__icon('Circle'),Star=__icon('Star'),Heart=__icon('Heart'),Bell=__icon('Bell'),Send=__icon('Send');
var MessageSquare=__icon('MessageSquare'),FileCode=__icon('FileCode'),Terminal=__icon('Terminal');
var Loader=__icon('Loader'),Loader2=__icon('Loader2'),RefreshCw=__icon('RefreshCw');
var Paperclip=__icon('Paperclip'),Sparkles=__icon('Sparkles'),Maximize2=__icon('Maximize2'),Minimize2=__icon('Minimize2');
var MoreHorizontal=__icon('MoreHorizontal'),MoreVertical=__icon('MoreVertical'),Cpu=__icon('Cpu');
var Database=__icon('Database'),Server=__icon('Server'),Globe=__icon('Globe'),Mail=__icon('Mail'),Phone=__icon('Phone');
var Camera=__icon('Camera'),Image=__icon('Image'),Video=__icon('Video'),Music=__icon('Music');
var Play=__icon('Play'),Pause=__icon('Pause'),Square=__icon('Square'),Triangle=__icon('Triangle');
var Slash=__icon('Slash'),Hash=__icon('Hash'),AtSign=__icon('AtSign');
`

/* ─── Build final iframe HTML ─────────────────────────────────── */
function buildIframeHtml(
  compiledJs: string,
  filename: string,
  rawCode: string,
  error?: string,
  extraStubs?: string,
  stubbedVars?: string[]
): string {
  const isHtml = /\.(html|htm)$/i.test(filename)

  if (isHtml && !error) {
    const hasTw = /class[Name]*=["'][^"']*(?:flex|grid|text-|bg-|p-|m-|w-|h-|rounded|border|shadow)/i.test(rawCode)
    if (hasTw && !rawCode.includes('tailwindcss')) {
      return rawCode.replace('</head>', '<script src="https://cdn.tailwindcss.com"><\/script></head>')
    }
    return rawCode
  }

  const hasTailwind = /className=["'][^"']*(?:flex|grid|text-|bg-|p-|m-|w-|h-|rounded|border|shadow)/i.test(rawCode)
  const twTag = hasTailwind ? '<script crossorigin="anonymous" src="https://cdn.tailwindcss.com"><\/script>' : ''

  // Escape </script> inside compiled JS to prevent breaking the HTML
  const safeJs = compiledJs.replace(/<\/script>/gi, '<\\/script>')

  if (error) {
    const safeErr = error.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    return `<!DOCTYPE html><html><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:20px;font-family:'SF Mono',Menlo,monospace;background:#1a1a2e;color:#fca5a5;font-size:13px;white-space:pre-wrap;line-height:1.6;">
<div style="color:#ef4444;font-weight:bold;margin-bottom:8px;">⚠ Compilation Error</div>
${safeErr}
<div style="margin-top:16px;color:#6b7280;font-size:11px;">This usually means the component uses syntax or patterns that the preview can't handle. The diff is still valid — you can Apply it normally.</div>
</body></html>`
  }

  return `<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
${twTag}
<script crossorigin="anonymous" src="https://unpkg.com/react@18/umd/react.development.js"><\/script>
<script crossorigin="anonymous" src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"><\/script>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}
#root{min-height:100vh;}
#error-banner{display:none;position:fixed;top:0;left:0;right:0;background:#7f1d1d;color:#fca5a5;
padding:8px 12px;font-size:11px;font-family:monospace;z-index:9999;white-space:pre-wrap;word-break:break-all;}
#stub-banner{display:${stubbedVars && stubbedVars.length ? 'block' : 'none'};position:fixed;top:0;left:0;right:0;
background:#78350f;color:#fcd34d;padding:7px 12px;font-size:11px;font-family:monospace;z-index:9998;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
</style>
<script>
window.onerror = function(msg, src, line, col, err) {
  var str = err && err.message ? err.message : String(msg);
  // Detect missing variable → ask parent to inject a stub and retry
  var mv = str.match(/Can't find variable[:\\s]+(\\w+)/i) || str.match(/(\\w+) is not defined/i);
  if (mv) {
    try { window.parent.postMessage({ type: 'sai_missing_var', name: mv[1] }, '*'); } catch(_) {}
  }
  var b = document.getElementById('error-banner');
  if (b) { b.style.display = 'block'; b.textContent = 'Runtime error: ' + str; }
  return true;
};
<\/script>
</head>
<body>
<div id="stub-banner">${stubbedVars && stubbedVars.length ? '⚠ Partial preview — stubbed from other files: ' + stubbedVars.join(', ') : ''}</div>
<div id="error-banner"></div>
<div id="root" style="${stubbedVars && stubbedVars.length ? 'padding-top:30px' : ''}"></div>
<script>${MOCK_BLOCK}<\/script>
<script>${extraStubs || ''}<\/script>
<script>
try {
${safeJs}
} catch(__e) {
  var __b = document.getElementById('error-banner');
  if (__b) { __b.style.display='block'; __b.textContent = 'Runtime error: ' + (__e && __e.message ? __e.message : String(__e)); }
}
<\/script>
</body>
</html>`
}

/* ─── LivePreview Component ───────────────────────────────────── */
export function LivePreview({ code, filename, modifiedCode }: LivePreviewProps) {
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)
  const [key, setKey] = useState(0)
  const [loading, setLoading] = useState(false)
  const [iframeSrc, setIframeSrc] = useState<string | null>(null)
  const [compileError, setCompileError] = useState<string | null>(null)
  const [stubbedVars, setStubbedVars] = useState<string[]>([])
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const blobUrlRef = useRef<string | null>(null)
  const hasRetriedRef = useRef(false)  // only one stub-retry per open session

  const displayCode = modifiedCode || code

  // Compile JSX → JS on the main page, then build iframe HTML
  const compile = useCallback(async (extraStubs = '', extraStubbedVars: string[] = []) => {
    setLoading(true)
    setCompileError(null)
    try {
      const { js, error } = await compileJsx(displayCode, filename)
      const html = buildIframeHtml(js, filename, displayCode, error || undefined, extraStubs, extraStubbedVars)

      // Revoke old blob URL
      if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current)

      // Use blob URL so the iframe loads CDN scripts without sandbox origin issues
      const blob = new Blob([html], { type: 'text/html' })
      blobUrlRef.current = URL.createObjectURL(blob)
      setIframeSrc(blobUrlRef.current)

      if (error) setCompileError(error.length > 150 ? error.slice(0, 150) + '…' : error)
    } catch (e: any) {
      const msg = e.message || 'Unknown compilation error'
      setCompileError(msg)
      const html = buildIframeHtml('', filename, displayCode, msg, extraStubs, extraStubbedVars)
      if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current)
      const blob = new Blob([html], { type: 'text/html' })
      blobUrlRef.current = URL.createObjectURL(blob)
      setIframeSrc(blobUrlRef.current)
    } finally {
      setLoading(false)
    }
  }, [displayCode, filename])

  // Listen for missing-variable reports from the iframe → inject stubs and retry once
  useEffect(() => {
    const missingVarsRef: string[] = []
    const handler = (e: MessageEvent) => {
      if (e.data?.type !== 'sai_missing_var') return
      const name: string = e.data.name
      if (!name || missingVarsRef.includes(name)) return
      missingVarsRef.push(name)
      if (hasRetriedRef.current) return  // already retried once, don't loop
      // Debounce: collect all missing vars reported in this tick, then retry once
      clearTimeout((handler as any).__timer)
      ;(handler as any).__timer = setTimeout(() => {
        hasRetriedRef.current = true
        setStubbedVars([...missingVarsRef])
        const stubs = missingVarsRef
          .map(n => `var ${n} = function(props) { return React.createElement('span', { style:{opacity:0.3,fontSize:11} }, '[${n}]'); };`)
          .join('\n')
        compile(stubs, [...missingVarsRef])
      }, 50)
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [compile])

  // Compile when preview opens or code changes
  useEffect(() => {
    if (open) {
      hasRetriedRef.current = false
      setStubbedVars([])
      compile()
      // Scroll the panel into view smoothly so user doesn't miss it
      setTimeout(() => {
        panelRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
      }, 80)
    }
  }, [open, compile])

  // Cleanup blob URL on unmount
  useEffect(() => {
    return () => { if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current) }
  }, [])

  const refresh = () => {
    setKey(k => k + 1)
    compile()
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-1.5 px-2.5 py-1 bg-indigo-500/15 text-indigo-400 border border-indigo-500/30 rounded-lg text-[11px] font-semibold hover:bg-indigo-500/25 transition-colors"
        title="Live preview this component"
      >
        <Eye size={11} /> Preview
      </button>
    )
  }

  return (
    <div ref={panelRef} className={`mt-3 border border-indigo-500/30 rounded-xl overflow-hidden ${expanded ? 'fixed inset-4 z-50 shadow-2xl bg-base' : ''}`}>
      {/* Header bar */}
      <div className="flex items-center gap-2 px-3 py-2 bg-indigo-950 border-b border-indigo-500/20">
        <Eye size={12} className="text-indigo-400" />
        <span className="text-[11px] font-semibold text-indigo-300 uppercase tracking-wide">Live Preview</span>
        <span className="text-[10px] text-indigo-500 ml-1">{filename}</span>
        {loading && <Loader2 size={11} className="text-indigo-400 animate-spin" />}
        <div className="ml-auto flex items-center gap-1.5">
          <button onClick={refresh} className="p-1 text-indigo-400 hover:text-indigo-200 transition-colors" title="Refresh">
            <RefreshCw size={11} />
          </button>
          <button onClick={() => setExpanded(e => !e)} className="p-1 text-indigo-400 hover:text-indigo-200 transition-colors">
            {expanded ? <Minimize2 size={11} /> : <Maximize2 size={11} />}
          </button>
          <button onClick={() => { setOpen(false); setExpanded(false) }} className="p-1 text-indigo-400 hover:text-indigo-200 transition-colors">
            <EyeOff size={11} />
          </button>
        </div>
      </div>

      {/* Status bar — error or info */}
      {compileError ? (
        <div className="flex items-center gap-1.5 px-3 py-1.5 bg-red-500/10 border-b border-red-500/20">
          <AlertTriangle size={10} className="text-red-400 flex-shrink-0" />
          <span className="text-[10px] text-red-400 font-mono truncate">{compileError}</span>
        </div>
      ) : (
        <div className="flex items-center gap-1.5 px-3 py-1 bg-yellow-500/5 border-b border-yellow-500/10">
          <AlertTriangle size={10} className="text-yellow-500/60" />
          <span className="text-[10px] text-yellow-500/60">Sandboxed preview — visual rendering only.</span>
        </div>
      )}

      {/* Iframe — uses blob URL, not srcDoc */}
      {iframeSrc ? (
        <iframe
          key={key}
          ref={iframeRef}
          src={iframeSrc}
          sandbox="allow-scripts"
          className={`w-full bg-white ${expanded ? 'h-[calc(100vh-120px)]' : 'h-[480px]'}`}
          title={`Preview: ${filename}`}
        />
      ) : (
        <div className={`w-full bg-base flex items-center justify-center ${expanded ? 'h-[calc(100vh-120px)]' : 'h-[480px]'}`}>
          <Loader2 size={20} className="text-indigo-400 animate-spin" />
        </div>
      )}
    </div>
  )
}
