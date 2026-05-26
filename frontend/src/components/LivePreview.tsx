import React, { useState, useCallback, Component } from 'react'
import { SandpackProvider, SandpackPreview } from '@codesandbox/sandpack-react'
import { useThemeStore } from '../stores/themeStore'
import { Fullscreen, FullscreenExit, Refresh } from '@mui/icons-material'

/* ─── Public API ───────────────────────────────────────────────── */
export function isVisualFile(filename: string): boolean {
  return /\.(tsx|jsx|html|htm)$/i.test(filename)
}

interface LivePreviewProps {
  code: string
  filename: string
  modifiedCode?: string
  sessionId?: string
  fileId?: string
}

/* ─── Fix 6: Error boundary — shows compile errors instead of blank screen ── */
interface EBState { error: string | null }
class PreviewErrorBoundary extends Component<
  { children: React.ReactNode; onError?: (msg: string) => void },
  EBState
> {
  state: EBState = { error: null }
  static getDerivedStateFromError(e: Error): EBState {
    return { error: e.message || String(e) }
  }
  componentDidCatch(e: Error) {
    this.props.onError?.(e.message || String(e))
  }
  reset() { this.setState({ error: null }) }
  render() {
    if (this.state.error) {
      return (
        <div className="h-full flex flex-col items-start justify-start p-4 bg-red-950/30 overflow-auto">
          <div className="text-[11px] font-semibold text-red-400 mb-2">⚠ Preview error</div>
          <pre className="text-[11px] text-red-300/80 whitespace-pre-wrap font-mono leading-relaxed">
            {this.state.error}
          </pre>
        </div>
      )
    }
    return this.props.children
  }
}

/* ─── Helpers ──────────────────────────────────────────────────── */
function detectComponent(code: string): string {
  return (
    code.match(/export\s+default\s+(?:function|class)\s+([A-Z]\w*)/)?.[1] ||
    code.match(/export\s+default\s+([A-Z]\w*)/)?.[1] ||
    code.match(/export\s+(?:function|class)\s+([A-Z][a-z]\w*)/)?.[1] ||
    [...code.matchAll(/(?:function|const)\s+([A-Z][a-z][a-zA-Z0-9]*)\s*[=(]/g)].pop()?.[1] ||
    'App'
  )
}

function prepareCode(raw: string): string {
  let code = raw

  // ── Fix 2: remove `import type` entirely — they're compile-time only ──────
  // Must run BEFORE the relative-import stub so type imports don't get stubbed
  code = code.replace(
    /^import\s+type\s+.+?from\s+['"][^'"]+['"]\s*;?\s*$/gm,
    '// [type import removed for preview]'
  )

  // ── Fix 1: remove bare CSS/asset side-effect imports (no 'from' clause) ───
  // e.g. import './styles.css'  import '../../globals.css'  import './foo.svg'
  code = code.replace(
    /^import\s+['"]([^'"]+)['"]\s*;?\s*$/gm,
    (_, path) => `// [side-effect import removed: ${path}]`
  )

  // ── Fix 3: stub @/ path alias imports (shadcn/ui, workspace aliases) ───────
  // Treat @/... exactly like ./... — proxy stub
  code = code.replace(
    /^import\s+(.+?)\s+from\s+['"](@\/[^'"]+)['"]\s*;?\s*$/gm,
    (_, spec, path) => buildStub(spec, `@/ alias: ${path}`)
  )

  // ── Original: stub relative imports (./foo, ../bar) ───────────────────────
  code = code.replace(
    /^import\s+(.+?)\s+from\s+['"](\.{1,2}[^'"]+)['"]\s*;?\s*$/gm,
    (_, spec, path) => buildStub(spec, path)
  )

  // ── Fix 4: neutralise import.meta.env references ─────────────────────────
  // Replace import.meta.env.FOO → (__import_meta_env__.FOO)
  // and inject a safe empty object at the top
  if (/import\.meta\.env/.test(code)) {
    const envStub = `const __import_meta_env__ = typeof import.meta !== 'undefined' && import.meta.env ? import.meta.env : {};\n`
    code = envStub + code.replace(/import\.meta\.env/g, '__import_meta_env__')
  }

  return code
}

/** Build a two-tier Proxy stub for a named import specifier string */
function buildStub(spec: string, pathComment: string): string {
  const names = spec
    .replace(/\*\s+as\s+(\w+)/, '$1')
    .replace(/[{}]/g, '')
    .split(',')
    .map((s: string) => s.trim().split(/\s+as\s+/).pop()?.trim())
    .filter((n): n is string => !!n && /^\w+$/.test(n))

  if (!names.length) return `// [no names to stub from: ${pathComment}]`

  return names.map((n) =>
    `const ${n}: any = (() => { ` +
    `const DATA=/^(user|loading|isLoading|error|data|status|count|list|items|token|id|name|email|value|config|options|state|type|mode|size|length|theme|session|message|result|response|success|ready|open|visible|active|enabled|disabled|selected|checked|collapsed|expanded|setupRequired|isAuthenticated|isAdmin|isDark|isLight|isOpen|isMobile|isDesktop)$/;` +
    `const mk=(t)=>new Proxy(function(){},{` +
    `get:(_,k)=>{if(typeof k==='symbol')return undefined;if(k==='__esModule')return undefined;` +
    `if(k==='then'){if(!t)return undefined;return function(r){try{r&&r({data:{}});}catch(e){}return mk(true);};}` +
    `if(DATA.test(String(k)))return undefined;return mk(true);},` +
    `apply:()=>mk(true),construct:()=>({})});return mk(false); })(); ` +
    `// stubbed from: ${pathComment}`
  ).join('\n')
}

/* ─── Component ────────────────────────────────────────────────── */
export function LivePreview({ code, filename, modifiedCode, sessionId, fileId }: LivePreviewProps) {
  const { theme } = useThemeStore()
  const [expanded, setExpanded] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [sandpackError, setSandpackError] = useState<string | null>(null)

  const handleRefresh = useCallback(() => {
    setSandpackError(null)
    setRefreshKey((k) => k + 1)
  }, [])

  const src = modifiedCode ?? code
  const isHtml = /\.html?$/i.test(filename)
  const apiBase = (import.meta as any).env?.VITE_API_URL || ''

  const previewStyle: React.CSSProperties = expanded
    ? { flex: 1, minHeight: 0, width: '100%' }
    : { height: '440px', width: '100%' }

  const containerCls = `flex flex-col rounded-lg border border-border overflow-hidden${
    expanded ? ' fixed inset-4 z-50 bg-base' : ''
  }`

  const toolbar = (
    <div className="flex items-center justify-between px-3 py-1.5 bg-surface border-b border-border flex-shrink-0">
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-[11px] text-muted font-mono truncate">👁 {filename}</span>
        {/* Fix 6: show error indicator in toolbar */}
        {sandpackError && (
          <span className="text-[10px] text-red-400 bg-red-500/10 px-1.5 py-0.5 rounded flex-shrink-0">
            ⚠ error
          </span>
        )}
      </div>
      <div className="flex gap-1">
        <button
          onClick={handleRefresh}
          className="p-1 rounded hover:bg-hover text-muted hover:text-fg"
          title="Reload preview"
        >
          <Refresh sx={{ fontSize: 11 }} />
        </button>
        <button
          onClick={() => setExpanded((e) => !e)}
          className="p-1 rounded hover:bg-hover text-muted hover:text-fg"
          title={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? <FullscreenExit sx={{ fontSize: 11 }} /> : <Fullscreen sx={{ fontSize: 11 }} />}
        </button>
      </div>
    </div>
  )

  /* ── Option A: HTML via backend URL or srcdoc ─────────────────── */
  if (isHtml) {
    const previewUrl =
      sessionId && fileId
        ? `${apiBase}/api/chat/${sessionId}/files/${fileId}/preview`
        : null

    return (
      <div className={containerCls}>
        {toolbar}
        <div style={previewStyle}>
          {previewUrl ? (
            <iframe
              key={`${refreshKey}-url`}
              src={previewUrl}
              sandbox="allow-scripts allow-same-origin"
              style={{ width: '100%', height: '100%', border: 'none', background: 'transparent', display: 'block' }}
              title={`Preview: ${filename}`}
            />
          ) : (
            <iframe
              key={`${refreshKey}-doc`}
              sandbox="allow-scripts"
              srcDoc={src}
              style={{ width: '100%', height: '100%', border: 'none', background: 'transparent', display: 'block' }}
              title={`Preview: ${filename}`}
            />
          )}
        </div>
      </div>
    )
  }

  /* ── Option B: TSX/JSX via Sandpack ──────────────────────────── */
  const componentName = detectComponent(src)
  const hasDefault = /export\s+default\s+/.test(src)
  const processedCode = prepareCode(src)
  const appCode = hasDefault
    ? processedCode
    : `${processedCode}\nexport default ${componentName}`

  return (
    <div className={containerCls}>
      {toolbar}
      <div style={previewStyle}>
        <PreviewErrorBoundary onError={setSandpackError}>
          <SandpackProvider
            key={refreshKey}
            template="react-ts"
            style={{ height: '100%', width: '100%' }}
            files={{
              '/App.tsx': appCode,
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
                'class-variance-authority': 'latest',
                'clsx': 'latest',
                'tailwind-merge': 'latest',
              },
            }}
            options={{
              // Fix 5: load Tailwind synchronously before first paint so
              // styles are present on the initial render, not a frame late
              externalResources: [
                'https://cdn.tailwindcss.com/3.4.1',
              ],
            }}
          >
            <SandpackPreview
              style={{ height: '100%', width: '100%' }}
              showOpenInCodeSandbox={false}
              showRefreshButton={false}
            />
          </SandpackProvider>
        </PreviewErrorBoundary>
      </div>
    </div>
  )
}
