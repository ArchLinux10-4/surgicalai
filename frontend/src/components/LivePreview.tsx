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

/* ─── Detect main component name from source ─────────────────── */
function detectComponentName(code: string): string | null {
  // Priority 1: export default function/class Name
  const defaultFn = code.match(/export\s+default\s+(?:function|class)\s+([A-Z]\w*)/)
  if (defaultFn) return defaultFn[1]
  // Priority 2: export default Name (reference)
  const defaultRef = code.match(/export\s+default\s+([A-Z]\w*)/)
  if (defaultRef) return defaultRef[1]
  // Priority 3: last PascalCase function/const in file
  const fnMatches = [...code.matchAll(/(?:function|const)\s+([A-Z][a-zA-Z0-9]*)\s*[=(]/g)]
  return fnMatches.length > 0 ? fnMatches[fnMatches.length - 1][1] : null
}

/* ─── Compile TSX → JS — Babel handles ALL TypeScript (no regex hacks) ── */
async function compileJsx(
  rawCode: string,
  filename: string
): Promise<{ js: string; component: string | null; error?: string }> {
  const isHtml = /\.(html|htm)$/i.test(filename)
  if (isHtml) return { js: rawCode, component: null }

  const componentName = detectComponentName(rawCode)

  try {
    const Babel = await loadBabel()

    // Strategy 1: react + typescript presets with CommonJS module transform plugin
    // Babel handles ALL TypeScript stripping (types, interfaces, generics, annotations)
    // and converts ES imports → require() calls that our iframe shim handles
    try {
      const result = Babel.transform(rawCode, {
        presets: [
          'react',
          ['typescript', { isTSX: true, allExtensions: true }]
        ],
        plugins: ['transform-modules-commonjs'],
        filename: filename || 'component.tsx',
      })
      return { js: result.code, component: componentName }
    } catch {
      // Strategy 2: fall back to env preset (includes module transform)
      const result = Babel.transform(rawCode, {
        presets: [
          ['env', { modules: 'commonjs', targets: { esmodules: true } }],
          'react',
          ['typescript', { isTSX: true, allExtensions: true }]
        ],
        filename: filename || 'component.tsx',
      })
      return { js: result.code, component: componentName }
    }
  } catch (e: any) {
    return { js: '', component: null, error: e.message || String(e) }
  }
}

/* ─── require() shim + mocks for iframe ──────────────────────── */
const REQUIRE_SHIM = `
// Icon mock factory
var __icon = function(name) {
  return function(props) {
    var sz = (props && props.size) || 16;
    return React.createElement('span', {
      style: { display:'inline-block', width:sz, height:sz, background:'currentColor',
        borderRadius:2, opacity:0.6, verticalAlign:'middle', flexShrink:0 },
      title: name
    });
  };
};

// Module cache
var __moduleCache = {};

// CommonJS require() shim — returns mock modules for all imports
function require(id) {
  if (__moduleCache[id]) return __moduleCache[id];

  // ── React ──
  if (id === 'react') {
    return __moduleCache[id] = Object.assign({}, React, { default: React, __esModule: true });
  }
  if (id === 'react-dom' || id === 'react-dom/client') {
    return __moduleCache[id] = Object.assign({}, ReactDOM, { default: ReactDOM, __esModule: true });
  }

  // ── Lucide icons ──
  if (id === 'lucide-react') {
    var icons = { __esModule: true };
    var names = [
      'Eye','EyeOff','Shield','Zap','Lock','Code','User','Key','AlertTriangle','Check','X',
      'ChevronRight','ChevronDown','ChevronUp','ChevronLeft',
      'ArrowRight','ArrowLeft','Settings','LogOut','LogIn','Plus','Trash','Trash2','Edit',
      'Search','Menu','Home','File','Folder','Save','Download','Upload','Copy',
      'ExternalLink','Info','CheckCircle','XCircle','Circle','Star','Heart','Bell','Send',
      'MessageSquare','FileCode','Terminal','Loader','Loader2','RefreshCw',
      'Paperclip','Sparkles','Maximize2','Minimize2','MoreHorizontal','MoreVertical','Cpu',
      'Database','Server','Globe','Mail','Phone','Camera','Image','Video','Music',
      'Play','Pause','Square','Triangle','Slash','Hash','AtSign','Monitor',
      'Smartphone','Tablet','Wifi','WifiOff','Battery','BatteryCharging',
      'Sun','Moon','Cloud','CloudRain','Thermometer','Wind',
      'Github','Gitlab','Twitter','Linkedin','Facebook','Instagram',
      'Activity','Bookmark','Calendar','Clock','Compass','Disc',
      'Flag','Gift','Map','Package','Percent','Power','Printer','Share','Tag',
      'Target','Unlock','Voicemail','Watch','Crosshair','Feather','Hexagon','Layers',
      'LifeBuoy','PenTool','Repeat','RotateCcw','RotateCw','Scissors','ShoppingCart',
      'Sidebar','SkipBack','SkipForward','Sliders','Aperture','Award','BarChart',
      'Bold','Italic','Underline','Type','AlignLeft','AlignCenter','AlignRight',
      'Columns','Layout','Grid','List','Mic','MicOff','Volume','Volume1','Volume2','VolumeX',
      'ZoomIn','ZoomOut','Maximize','Minimize','Move','Navigation','Octagon',
      'PanelLeft','PanelRight','FilePlus','FolderOpen','GitBranch','GitCommit','GitPullRequest',
      'Braces','Binary','Bug','Workflow','Wrench','Plug','PlugZap','Brain','Flame','Rocket'
    ];
    names.forEach(function(n) { icons[n] = __icon(n); });
    icons.default = icons;
    return __moduleCache[id] = icons;
  }

  // ── react-router-dom ──
  if (id === 'react-router-dom' || id === 'react-router') {
    return __moduleCache[id] = {
      __esModule: true,
      useNavigate: function() { return function() {}; },
      useLocation: function() { return { pathname: '/preview', search: '', hash: '', state: null }; },
      useParams: function() { return {}; },
      useSearchParams: function() { return [new URLSearchParams(), function() {}]; },
      Link: function(p) { return React.createElement('a', { href: p.to || '#', onClick: function(e){ e.preventDefault(); } }, p.children); },
      Navigate: function() { return null; },
      Outlet: function() { return null; },
      BrowserRouter: function(p) { return React.createElement(React.Fragment, null, p.children); },
      Routes: function(p) { return React.createElement(React.Fragment, null, p.children); },
      Route: function() { return null; },
    };
  }

  // ── Zustand ──
  if (id === 'zustand' || id === 'zustand/middleware') {
    var createStore = function(fn) {
      var state = {};
      try { state = fn(function(){}, function(){ return state; }, {}) || {}; } catch(e) {}
      return function(sel) { return sel ? sel(state) : state; };
    };
    return __moduleCache[id] = {
      __esModule: true, default: createStore, create: createStore,
      persist: function(fn) { return fn; },
      devtools: function(fn) { return fn; },
    };
  }

  // ── Toast libraries ──
  if (id === 'react-hot-toast' || id === 'sonner') {
    var t = Object.assign(function(){}, { success:function(){}, error:function(){}, info:function(){}, warning:function(){} });
    return __moduleCache[id] = { __esModule: true, default: t, toast: t, Toaster: function() { return null; } };
  }

  // ── Axios ──
  if (id === 'axios') {
    var noop = function() { return Promise.resolve({ data: {} }); };
    var inst = { get: noop, post: noop, put: noop, patch: noop, delete: noop,
      interceptors: { request: { use: function(){} }, response: { use: function(){} } },
      defaults: { headers: { common: {} } }
    };
    inst.create = function() { return Object.assign({}, inst); };
    return __moduleCache[id] = Object.assign(inst, { __esModule: true, default: inst });
  }

  // ── Catch-all: Proxy that gracefully stubs ANY import ──
  try {
    return __moduleCache[id] = new Proxy({ __esModule: true }, {
      get: function(target, prop) {
        if (prop === '__esModule') return true;
        if (prop === 'default') return function(props) {
          return props && props.children ? React.createElement(React.Fragment, null, props.children) : null;
        };
        if (typeof prop === 'symbol') return undefined;
        if (typeof prop === 'string' && prop.length > 0 && prop[0] === prop[0].toUpperCase()) return __icon(prop);
        if (typeof prop === 'string') return function() { return {}; };
        return undefined;
      }
    });
  } catch(e) {
    return __moduleCache[id] = { __esModule: true, default: function() { return null; } };
  }
}
`

/* ─── Build final iframe HTML ─────────────────────────────────── */
function buildIframeHtml(
  compiledJs: string,
  filename: string,
  rawCode: string,
  componentName: string | null,
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
  const twTag = hasTailwind ? '<script crossorigin="anonymous" src="https://cdn.tailwindcss.com"></script>' : ''

  if (error) {
    const safeErr = error.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    return `<!DOCTYPE html><html><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:20px;font-family:'SF Mono',Menlo,monospace;background:#1a1a2e;color:#fca5a5;font-size:13px;white-space:pre-wrap;line-height:1.6;">
<div style="color:#ef4444;font-weight:bold;margin-bottom:8px;">⚠ Compilation Error</div>
${safeErr}
<div style="margin-top:16px;color:#6b7280;font-size:11px;">This usually means the component uses syntax the preview can't handle. The diff is still valid — you can Apply it normally.</div>
</body></html>`
  }

  // Escape </script> inside compiled JS
  const safeJs = compiledJs.replace(/<\/script>/gi, '<\\/script>')

  // Mount block: use CommonJS exports set by Babel's module transform
  const mountBlock = componentName
    ? `
try {
  var __C = module.exports.default || module.exports['${componentName}'];
  if (typeof __C === 'function') {
    ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(__C));
  } else {
    var __b = document.getElementById('error-banner');
    if (__b) { __b.style.display='block'; __b.textContent = 'Could not find component "${componentName}" in exports: ' + Object.keys(module.exports).join(', '); }
  }
} catch(__e) {
  var __b = document.getElementById('error-banner');
  if (__b) { __b.style.display='block'; __b.textContent = 'Render error: ' + (__e && __e.message ? __e.message : String(__e)); }
}`
    : `
var __b = document.getElementById('error-banner');
if (__b) { __b.style.display='block'; __b.textContent = 'No React component detected in file.'; }`

  const stubBannerDisplay = stubbedVars && stubbedVars.length ? 'block' : 'none'
  const stubBannerText = stubbedVars && stubbedVars.length ? '⚠ Partial preview — stubbed: ' + stubbedVars.join(', ') : ''
  const rootPadding = stubbedVars && stubbedVars.length ? 'padding-top:30px' : ''

  return `<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
${twTag}
<script crossorigin="anonymous" src="https://unpkg.com/react@18/umd/react.development.js"></script>
<script crossorigin="anonymous" src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}
#root{min-height:100vh;}
#error-banner{display:none;position:fixed;top:0;left:0;right:0;background:#7f1d1d;color:#fca5a5;
padding:8px 12px;font-size:11px;font-family:monospace;z-index:9999;white-space:pre-wrap;word-break:break-all;}
#stub-banner{display:${stubBannerDisplay};position:fixed;top:0;left:0;right:0;
background:#78350f;color:#fcd34d;padding:7px 12px;font-size:11px;font-family:monospace;z-index:9998;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
</style>
<script>
window.onerror = function(msg, src, line, col, err) {
  var str = err && err.message ? err.message : String(msg);
  var mv = str.match(/Can't find variable[:\\s]+(\\w+)/i) || str.match(/(\\w+) is not defined/i);
  if (mv) {
    try { window.parent.postMessage({ type: 'sai_missing_var', name: mv[1] }, '*'); } catch(_) {}
  }
  var b = document.getElementById('error-banner');
  if (b) { b.style.display = 'block'; b.textContent = 'Runtime error: ' + str; }
  return true;
};
</script>
</head>
<body>
<div id="stub-banner">${stubBannerText}</div>
<div id="error-banner"></div>
<div id="root" style="${rootPadding}"></div>
<script>
${REQUIRE_SHIM}
</script>
<script>
${extraStubs || ''}
</script>
<script>
var module = { exports: {} };
var exports = module.exports;
try {
${safeJs}
} catch(__e) {
  var __b = document.getElementById('error-banner');
  if (__b) { __b.style.display='block'; __b.textContent = 'Load error: ' + (__e && __e.message ? __e.message : String(__e)); }
}
${mountBlock}
</script>
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

  // Compile TSX → JS on the main page, then build iframe HTML
  const compile = useCallback(async (extraStubs = '', extraStubbedVars: string[] = []) => {
    setLoading(true)
    setCompileError(null)
    try {
      const { js, component, error } = await compileJsx(displayCode, filename)
      const html = buildIframeHtml(js, filename, displayCode, component, error || undefined, extraStubs, extraStubbedVars)

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
      const html = buildIframeHtml('', filename, displayCode, null, msg, extraStubs, extraStubbedVars)
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
