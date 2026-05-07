import React, { useRef, useState } from 'react'
import { Eye, EyeOff, RefreshCw, AlertTriangle, Maximize2, Minimize2 } from 'lucide-react'

interface LivePreviewProps {
  code: string
  filename: string
  modifiedCode?: string
}

function isVisualFile(filename: string): boolean {
  return /\.(tsx|jsx|html|htm|css|svg)$/i.test(filename)
}

function buildIframeSrc(code: string, filename: string): string {
  const isHtml = /\.(html|htm)$/i.test(filename)

  if (isHtml) {
    const hasTailwind = /class[Name]*=["'][^"']*(?:flex|grid|text-|bg-|p-|m-|w-|h-|rounded|border|shadow)/i.test(code)
    const withTailwind = hasTailwind
      ? code.replace('</head>', '<script src="https://cdn.tailwindcss.com"></script></head>')
      : code
    return withTailwind
  }

  const hasTailwind = /className=["'][^"']*(?:flex|grid|text-|bg-|p-|m-|w-|h-|rounded|border|shadow)/i.test(code)

  const defaultExportMatch = code.match(/export\s+default\s+(?:function\s+)?(\w+)/)
  const functionMatches = [...code.matchAll(/(?:function|const)\s+([A-Z][a-zA-Z0-9]*)\s*[=(]/g)]
  const componentName =
    defaultExportMatch?.[1] ||
    (functionMatches.length > 0 ? functionMatches[functionMatches.length - 1][1] : null)

  let cleaned = code
    .replace(/^import\s+.*?from\s+['"][^'"]+['"]\s*;?\s*$/gm, '')
    .replace(/^export\s+default\s+/gm, '')
    .replace(/^export\s+(?:const|function|class|type|interface)\s+/gm, (m) => m.replace('export ', ''))
    .replace(/^type\s+\w+\s*=[\s\S]*?(?=\n(?:const|function|class|export|interface|type|\s*$))/gm, '')
    .replace(/^interface\s+\w+\s*\{[\s\S]*?\n\}/gm, '')

  const mountCall = componentName
    ? `\ntry {\n  const __root = ReactDOM.createRoot(document.getElementById('root'));\n  __root.render(React.createElement(${componentName}));\n} catch(__e) {\n  var __b = document.getElementById('error-banner');\n  if (__b) { __b.style.display = 'block'; __b.textContent = 'Render error: ' + (__e && __e.message ? __e.message : String(__e)); }\n}`
    : `\n// Could not detect component name`

  const tailwindTag = hasTailwind ? '<script src="https://cdn.tailwindcss.com"><\/script>' : ''

  const mockBlock = `
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

    // API client mock — returns rejected promise so .catch() handlers fire correctly
    var apiClient = {
      get: function() { return Promise.reject(new Error('preview-offline')); },
      post: function() { return Promise.reject(new Error('preview-offline')); },
      put: function() { return Promise.reject(new Error('preview-offline')); },
      patch: function() { return Promise.reject(new Error('preview-offline')); },
      delete: function() { return Promise.reject(new Error('preview-offline')); },
    };

    // Common Zustand-style store mocks
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
    var Eye=__icon('Eye'); var EyeOff=__icon('EyeOff'); var Shield=__icon('Shield');
    var Zap=__icon('Zap'); var Lock=__icon('Lock'); var Code=__icon('Code');
    var User=__icon('User'); var Key=__icon('Key'); var AlertTriangle=__icon('AlertTriangle');
    var Check=__icon('Check'); var X=__icon('X'); var ChevronRight=__icon('ChevronRight');
    var ChevronDown=__icon('ChevronDown'); var ChevronUp=__icon('ChevronUp');
    var ArrowRight=__icon('ArrowRight'); var ArrowLeft=__icon('ArrowLeft');
    var Settings=__icon('Settings'); var LogOut=__icon('LogOut'); var LogIn=__icon('LogIn');
    var Plus=__icon('Plus'); var Trash=__icon('Trash'); var Edit=__icon('Edit');
    var Search=__icon('Search'); var Menu=__icon('Menu'); var Home=__icon('Home');
    var File=__icon('File'); var Folder=__icon('Folder'); var Save=__icon('Save');
    var Download=__icon('Download'); var Upload=__icon('Upload'); var Copy=__icon('Copy');
    var ExternalLink=__icon('ExternalLink'); var Info=__icon('Info');
    var CheckCircle=__icon('CheckCircle'); var XCircle=__icon('XCircle');
    var Circle=__icon('Circle'); var Star=__icon('Star'); var Heart=__icon('Heart');
    var Bell=__icon('Bell'); var Send=__icon('Send'); var MessageSquare=__icon('MessageSquare');
    var FileCode=__icon('FileCode'); var Terminal=__icon('Terminal');
    var Loader=__icon('Loader'); var RefreshCw=__icon('RefreshCw');
    var Paperclip=__icon('Paperclip'); var Sparkles=__icon('Sparkles');
    var Maximize2=__icon('Maximize2'); var Minimize2=__icon('Minimize2');
    var MoreHorizontal=__icon('MoreHorizontal'); var MoreVertical=__icon('MoreVertical');
    var Cpu=__icon('Cpu'); var Database=__icon('Database'); var Server=__icon('Server');
    var Globe=__icon('Globe'); var Mail=__icon('Mail'); var Phone=__icon('Phone');
    var Camera=__icon('Camera'); var Image=__icon('Image'); var Video=__icon('Video');
    var Music=__icon('Music'); var Play=__icon('Play'); var Pause=__icon('Pause');
    var Square=__icon('Square'); var Triangle=__icon('Triangle');
    var Slash=__icon('Slash'); var Hash=__icon('Hash'); var AtSign=__icon('AtSign');
  `

  return `<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
${tailwindTag}
<script crossorigin src="https://unpkg.com/react@18/umd/react.development.js"><\/script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"><\/script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"><\/script>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}
#root{min-height:100vh;}
#error-banner{display:none;position:fixed;top:0;left:0;right:0;background:#7f1d1d;color:#fca5a5;
padding:8px 12px;font-size:11px;font-family:monospace;z-index:9999;white-space:pre-wrap;word-break:break-all;}
</style>
<script>
window.onerror = function(msg, src, line, col, err) {
  var b = document.getElementById('error-banner');
  if (b) { b.style.display='block'; b.textContent = 'JS Error: ' + msg + (err ? ' — ' + (err.stack ? err.stack.slice(0,400) : '') : ''); }
  return true;
};
<\/script>
</head>
<body>
<div id="error-banner"></div>
<div id="root"></div>
<script>${mockBlock}<\/script>
<script type="text/babel" data-presets="react,typescript">
const { useState, useEffect, useRef, useCallback, useMemo, useContext, createContext, forwardRef, memo } = React;
${cleaned}
${mountCall}
<\/script>
</body>
</html>`
}

export function LivePreview({ code, filename, modifiedCode }: LivePreviewProps) {
  const [open, setOpen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [key, setKey] = useState(0)
  const iframeRef = useRef<HTMLIFrameElement>(null)

  const displayCode = modifiedCode || code
  const iframeContent = buildIframeSrc(displayCode, filename)

  const refresh = () => setKey(k => k + 1)

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
    <div className={`mt-3 border border-indigo-500/30 rounded-xl overflow-hidden ${expanded ? 'fixed inset-4 z-50 shadow-2xl' : ''}`}>
      <div className="flex items-center gap-2 px-3 py-2 bg-indigo-950/60 border-b border-indigo-500/20">
        <Eye size={12} className="text-indigo-400" />
        <span className="text-[11px] font-semibold text-indigo-300 uppercase tracking-wide">Live Preview</span>
        <span className="text-[10px] text-indigo-500 ml-1">{filename}</span>
        <div className="ml-auto flex items-center gap-1.5">
          <button onClick={refresh} className="p-1 text-indigo-400 hover:text-indigo-200 transition-colors" title="Refresh">
            <RefreshCw size={11} />
          </button>
          <button onClick={() => setExpanded(e => !e)} className="p-1 text-indigo-400 hover:text-indigo-200 transition-colors">
            {expanded ? <Minimize2 size={11} /> : <Maximize2 size={11} />}
          </button>
          <button onClick={() => setOpen(false)} className="p-1 text-indigo-400 hover:text-indigo-200 transition-colors">
            <EyeOff size={11} />
          </button>
        </div>
      </div>
      <div className="flex items-center gap-1.5 px-3 py-1 bg-yellow-500/5 border-b border-yellow-500/10">
        <AlertTriangle size={10} className="text-yellow-500/60" />
        <span className="text-[10px] text-yellow-500/60">Sandboxed preview — no network, no auth. Visual rendering only.</span>
      </div>
      <iframe
        key={key}
        ref={iframeRef}
        srcDoc={iframeContent}
        sandbox="allow-scripts"
        className={`w-full bg-white ${expanded ? 'h-[calc(100vh-120px)]' : 'h-[480px]'}`}
        title={`Preview: ${filename}`}
      />
    </div>
  )
}

export { isVisualFile }
