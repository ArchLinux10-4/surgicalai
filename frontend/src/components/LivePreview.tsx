import React, { useEffect, useRef, useState } from 'react'
import { Eye, EyeOff, RefreshCw, AlertTriangle, Maximize2, Minimize2 } from 'lucide-react'

interface LivePreviewProps {
  code: string
  filename: string
  modifiedCode?: string // post-apply code
}

function isVisualFile(filename: string): boolean {
  return /\.(tsx|jsx|html|htm|css|svg)$/i.test(filename)
}

function buildIframeSrc(code: string, filename: string): string {
  const isHtml = /\.(html|htm)$/i.test(filename)

  if (isHtml) {
    // Inject Tailwind CDN if it looks like it uses Tailwind
    const hasTailwind = /class[Name]*=["'][^"']*(?:flex|grid|text-|bg-|p-|m-|w-|h-|rounded|border|shadow)/i.test(code)
    const withTailwind = hasTailwind
      ? code.replace('</head>', '<script src="https://cdn.tailwindcss.com"></script></head>')
      : code
    return withTailwind
  }

  // React TSX/JSX — transpile with Babel standalone
  // Strip import/export statements, wrap in renderable sandbox
  const hasTailwind = /className=["'][^"']*(?:flex|grid|text-|bg-|p-|m-|w-|h-|rounded|border|shadow)/i.test(code)

  // Extract component name (default export or last PascalCase function)
  const defaultExportMatch = code.match(/export\s+default\s+(?:function\s+)?(\w+)/)
  const functionMatches = [...code.matchAll(/(?:function|const)\s+([A-Z][a-zA-Z0-9]*)\s*[=(]/g)]
  const componentName =
    defaultExportMatch?.[1] ||
    (functionMatches.length > 0 ? functionMatches[functionMatches.length - 1][1] : null)

  // Clean the code:
  // 1. Remove all import statements
  // 2. Remove export keywords (keep the function/const body)
  // 3. Remove TypeScript type annotations that Babel might choke on (interfaces, type aliases at top level)
  let cleaned = code
    .replace(/^import\s+.*?from\s+['"][^'"]+['"]\s*;?\s*$/gm, '') // remove imports
    .replace(/^export\s+default\s+/gm, '') // remove export default
    .replace(/^export\s+(?:const|function|class|type|interface)\s+/gm, (m) =>
      m.replace('export ', '')) // remove export keyword from declarations
    .replace(/^type\s+\w+\s*=[\s\S]*?(?=\n(?:const|function|class|export|interface|type|\s*$))/gm, '') // remove type aliases
    .replace(/^interface\s+\w+\s*\{[\s\S]*?\n\}/gm, '') // remove interface blocks

  const mountCall = componentName
    ? `\n\nconst __root = ReactDOM.createRoot(document.getElementById('root'));\n__root.render(React.createElement(${componentName}));`
    : `\n\n// Could not detect component to render`

  const html = `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  ${hasTailwind ? '<script src="https://cdn.tailwindcss.com"></script>' : ''}
  <script crossorigin src="https://unpkg.com/react@18/umd/react.development.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <style>
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
    #root { min-height: 100vh; }
    #error-banner {
      display: none;
      position: fixed; top: 0; left: 0; right: 0;
      background: #7f1d1d; color: #fca5a5;
      padding: 8px 12px; font-size: 12px; font-family: monospace;
      z-index: 9999; white-space: pre-wrap; word-break: break-all;
    }
  </style>
</head>
<body>
  <div id="error-banner"></div>
  <div id="root"></div>
  <script type="text/babel" data-presets="react,typescript">
    // Make React & ReactDOM available as globals
    const { useState, useEffect, useRef, useCallback, useMemo, useContext, createContext } = React;

    // ---- USER COMPONENT CODE ----
${cleaned}
    // ---- END USER CODE ----
${mountCall}
  </script>
  <script>
    window.addEventListener('error', function(e) {
      var b = document.getElementById('error-banner');
      b.style.display = 'block';
      b.textContent = '⚠ Preview error: ' + (e.message || e) + (e.filename ? ' (' + e.filename + ':' + e.lineno + ')' : '');
    });
  </script>
</body>
</html>`

  return html
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
    <div className={`mt-3 border border-indigo-500/30 rounded-xl overflow-hidden transition-all ${expanded ? 'fixed inset-4 z-50 shadow-2xl' : ''}`}>
      {/* Preview toolbar */}
      <div className="flex items-center gap-2 px-3 py-2 bg-indigo-950/60 border-b border-indigo-500/20">
        <Eye size={12} className="text-indigo-400" />
        <span className="text-[11px] font-semibold text-indigo-300 uppercase tracking-wide">Live Preview</span>
        <span className="text-[10px] text-indigo-500 ml-1">{filename}</span>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            onClick={refresh}
            className="p-1 text-indigo-400 hover:text-indigo-200 transition-colors"
            title="Refresh preview"
          >
            <RefreshCw size={11} />
          </button>
          <button
            onClick={() => setExpanded(e => !e)}
            className="p-1 text-indigo-400 hover:text-indigo-200 transition-colors"
            title={expanded ? 'Collapse' : 'Expand fullscreen'}
          >
            {expanded ? <Minimize2 size={11} /> : <Maximize2 size={11} />}
          </button>
          <button
            onClick={() => setOpen(false)}
            className="p-1 text-indigo-400 hover:text-indigo-200 transition-colors"
            title="Close preview"
          >
            <EyeOff size={11} />
          </button>
        </div>
      </div>

      {/* Sandbox notice */}
      <div className="flex items-center gap-1.5 px-3 py-1 bg-yellow-500/5 border-b border-yellow-500/10">
        <AlertTriangle size={10} className="text-yellow-500/60" />
        <span className="text-[10px] text-yellow-500/60">Sandboxed preview — no network, no auth. Visual rendering only.</span>
      </div>

      {/* The iframe */}
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
