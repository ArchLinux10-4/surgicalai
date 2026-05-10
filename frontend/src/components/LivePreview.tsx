import React, { useState } from 'react'
import { RefreshCw, Maximize2, Minimize2 } from 'lucide-react'
import { SandpackProvider, SandpackPreview } from '@codesandbox/sandpack-react'
import { useThemeStore } from '../stores/themeStore'

/* ─── Public API ───────────────────────────────────────────────── */
export function isVisualFile(filename: string): boolean {
  return /\.(tsx|jsx|html|htm)$/i.test(filename)
}

interface LivePreviewProps {
  code: string
  filename: string
  modifiedCode?: string
  /** Optional — enables backend-served HTML preview (resolves relative paths) */
  sessionId?: string
  fileId?: string
}

/* ─── Helpers ──────────────────────────────────────────────────── */

function detectComponent(code: string): string {
  return (
    // 1. export default function/class Foo
    code.match(/export\s+default\s+(?:function|class)\s+([A-Z]\w*)/)?.[1] ||
    // 2. export default Foo
    code.match(/export\s+default\s+([A-Z]\w*)/)?.[1] ||
    // 3. named export: export function FooBar (PascalCase only)
    code.match(/export\s+(?:function|class)\s+([A-Z][a-z]\w*)/)?.[1] ||
    // 4. last PascalCase const/function
    [...code.matchAll(/(?:function|const)\s+([A-Z][a-z][a-zA-Z0-9]*)\s*[=(]/g)].pop()?.[1] ||
    'App'
  )
}

/**
 * Replace relative imports with lightweight stubs so Sandpack doesn't throw
 * on missing local files. Package imports (react, lucide-react, etc.) are left
 * untouched and resolved by Sandpack's bundler.
 */
function stubRelativeImports(code: string): string {
  return code.replace(
    /^import\s+(.+?)\s+from\s+['"](\.{1,2}[^'"]+)['"]\s*;?\s*$/gm,
    (_, spec) => {
      const names = spec
        .replace(/\*\s+as\s+(\w+)/, '$1')
        .replace(/[{}]/g, '')
        .split(',')
        .map((s: string) => s.trim().split(/\s+as\s+/).pop()?.trim())
        .filter(Boolean) as string[]
      // Each stub is a self-referential Proxy: calling, destructuring, or accessing
      // any property all return the same proxy. This handles every realistic pattern:
      //   useAuthStore()                   → returns proxy (apply trap)
      //   const { x, y } = useAuthStore()  → x and y are proxies (get trap)
      //   apiClient.get('/x').then(...)    → chained calls all return proxy
      //   <Component />                    → renders nothing (proxy as component)
      // Symbols and special keys (then/Symbol.iterator) return undefined so the
      // proxy isn't mistaken for a thenable or iterable by React/JS internals.
      return names
        .map(
          (n) =>
            // Two-tier proxy:
            //   Top-level export: then=undefined → NOT thenable (await proxy returns proxy)
            //   Call/property result: then=function → IS thenable (apiClient.get('/x').then(cb) works)
            //   Both tiers: apply fires → returns tier-2 proxy (useAuthStore() → destructurable)
            `const ${n}: any = (() => { const mk=(t)=>new Proxy(function(){},{get:(_,k)=>{if(typeof k==='symbol')return undefined;if(k==='__esModule')return undefined;if(k==='then'){if(!t)return undefined;return function(r){try{r&&r({data:{}});}catch(e){}return mk(true);};}return mk(true);},apply:()=>mk(true),construct:()=>({})});return mk(false); })();`
        )
        .join('\n')
    }
  )
}

/* ─── Component ────────────────────────────────────────────────── */
export function LivePreview({ code, filename, modifiedCode, sessionId, fileId }: LivePreviewProps) {
  const { theme } = useThemeStore()
  const [expanded, setExpanded] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)

  const src = modifiedCode ?? code
  const isHtml = /\.html?$/i.test(filename)
  const apiBase = (import.meta as any).env?.VITE_API_URL || ''
  // flex:1 + minHeight:0 fills the fixed overlay correctly (height:'100%' overshoots)
  const previewStyle: React.CSSProperties = expanded
    ? { flex: 1, minHeight: 0, width: '100%' }
    : { height: '440px', width: '100%' }
  const containerCls = `flex flex-col rounded-lg border border-border overflow-hidden${expanded ? ' fixed inset-4 z-50 bg-base' : ''}`

  const toolbar = (
    <div className="flex items-center justify-between px-3 py-1.5 bg-surface border-b border-border flex-shrink-0">
      <span className="text-[11px] text-muted font-mono truncate">👁 {filename}</span>
      <div className="flex gap-1">
        <button
          onClick={() => setRefreshKey((k) => k + 1)}
          className="p-1 rounded hover:bg-hover text-muted hover:text-fg"
          title="Reload"
        >
          <RefreshCw size={11} />
        </button>
        <button
          onClick={() => setExpanded((e) => !e)}
          className="p-1 rounded hover:bg-hover text-muted hover:text-fg"
          title={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? <Minimize2 size={11} /> : <Maximize2 size={11} />}
        </button>
      </div>
    </div>
  )

  /* ── Option A: HTML via backend URL ─────────────────────────── */
  if (isHtml) {
    const previewUrl =
      sessionId && fileId
        ? `${apiBase}/api/chat/${sessionId}/files/${fileId}/preview`
        : null

    return (
      <div className={containerCls}>
        {toolbar}
        {previewUrl ? (
          <iframe
            key={`${refreshKey}-url`}
            src={previewUrl}
            // allow-same-origin lets relative sub-resources (CSS/JS in same origin) load
            sandbox="allow-scripts allow-same-origin"
            style={{ ...previewStyle, border: 'none', background: 'transparent', display: 'block' }}
            title={`Preview: ${filename}`}
          />
        ) : (
          /* Fallback: srcdoc (no IDs — e.g. preview before file is uploaded) */
          <iframe
            key={`${refreshKey}-doc`}
            sandbox="allow-scripts"
            srcDoc={src}
            style={{ ...previewStyle, border: 'none', background: 'transparent', display: 'block' }}
            title={`Preview: ${filename}`}
          />
        )}
      </div>
    )
  }

  /* ── Option B: TSX/JSX via Sandpack ─────────────────────────── */
  const componentName = detectComponent(src)
  const hasDefault = /export\s+default\s+/.test(src)
  const processedCode = stubRelativeImports(src)
  // Ensure Sandpack's default index.tsx can do `import App from './App'`
  const appCode = hasDefault
    ? processedCode
    : `${processedCode}\nexport default ${componentName}`

  return (
    <div className={containerCls}>
      {toolbar}
      <SandpackProvider
        key={refreshKey}
        template="react-ts"
        files={{
          '/App.tsx': appCode,
          // Reset browser defaults so the component's own background fills the iframe
          // (without this, body has margin:8px and a white background that bleeds through)
          '/index.css': [
            '*, *::before, *::after { box-sizing: border-box; }',
            'html, body { margin: 0; padding: 0; width: 100%; height: 100%; background: transparent; }',
            '#root { width: 100%; height: 100%; }',
          ].join(' '),
          '/index.tsx': [
            "import React from 'react';",
            "import { createRoot } from 'react-dom/client';",
            "import './index.css';",
            "import App from './App';",
            "createRoot(document.getElementById('root')!).render(<App />);",
          ].join('\n'),
        }}
        theme={theme === 'dark' ? 'dark' : 'light'}
        customSetup={{
          dependencies: {
            'lucide-react': 'latest',
          },
        }}
        options={{
          externalResources: ['https://cdn.tailwindcss.com'],
        }}
      >
        <SandpackPreview
          style={previewStyle}
          showOpenInCodeSandbox={false}
          showRefreshButton={false}
        />
      </SandpackProvider>
    </div>
  )
}
