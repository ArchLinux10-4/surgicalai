import React, { useState, useCallback, useEffect, useMemo, Component } from 'react'
import { SandpackProvider, SandpackPreview } from '@codesandbox/sandpack-react'
import { useThemeStore } from '../stores/themeStore'
import { Fullscreen, FullscreenExit, Refresh } from '@mui/icons-material'
import { api } from '../api/client'

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

/* The resolved import graph returned by the backend preview-bundle endpoint. */
interface PreviewBundle {
  entry: string
  entryImport: string
  files: Record<string, string>
  dependencies: Record<string, string>
  external: string[]
  unresolved: string[]
  component: string
}

/* ─── Error boundary — shows compile errors instead of a blank screen ── */
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

/* ─── Fallback helpers (used only when no session graph is available) ───── */
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
  code = code.replace(
    /^import\s+type\s+.+?from\s+['"][^'"]+['"]\s*;?\s*$/gm,
    '// [type import removed for preview]'
  )
  code = code.replace(
    /^import\s+['"]([^'"]+)['"]\s*;?\s*$/gm,
    (_, path) => `// [side-effect import removed: ${path}]`
  )
  code = code.replace(
    /^import\s+(.+?)\s+from\s+['"](@\/[^'"]+)['"]\s*;?\s*$/gm,
    (_, spec, path) => buildStub(spec, `@/ alias: ${path}`)
  )
  code = code.replace(
    /^import\s+(.+?)\s+from\s+['"](\.{1,2}[^'"]+)['"]\s*;?\s*$/gm,
    (_, spec, path) => buildStub(spec, path)
  )
  if (/import\.meta\.env/.test(code)) {
    const envStub = `const __import_meta_env__ = typeof import.meta !== 'undefined' && import.meta.env ? import.meta.env : {};\n`
    code = envStub + code.replace(/import\.meta\.env/g, '__import_meta_env__')
  }
  return code
}

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

/* Base files always present in the Sandpack workspace. */
const BASE_INDEX_CSS = [
  '*, *::before, *::after { box-sizing: border-box; }',
  'html, body { margin: 0; padding: 0; width: 100%; height: 100%; background: transparent; }',
  '#root { width: 100%; height: 100%; }',
].join(' ')

const BASE_DEPS: Record<string, string> = {
  'lucide-react': 'latest',
  'class-variance-authority': 'latest',
  'clsx': 'latest',
  'tailwind-merge': 'latest',
}

/* ─── Component ────────────────────────────────────────────────── */
export function LivePreview({ code, filename, modifiedCode, sessionId, fileId }: LivePreviewProps) {
  const { theme } = useThemeStore()
  const [expanded, setExpanded] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [sandpackError, setSandpackError] = useState<string | null>(null)
  const [bundle, setBundle] = useState<PreviewBundle | null>(null)
  const [bundleLoading, setBundleLoading] = useState(false)

  const handleRefresh = useCallback(() => {
    setSandpackError(null)
    setRefreshKey((k) => k + 1)
  }, [])

  // Auto-refresh when Apply All fires from ChatPanel
  useEffect(() => {
    const onApplied = () => {
      setSandpackError(null)
      setRefreshKey((k) => k + 1)
    }
    window.addEventListener('sai-applied-refresh', onApplied)
    return () => window.removeEventListener('sai-applied-refresh', onApplied)
  }, [])

  const src = modifiedCode ?? code
  const isHtml = /\.html?$/i.test(filename)
  const apiBase = (import.meta as any).env?.VITE_API_URL || ''

  /* ── Resolve the full import graph from the session (components + CSS + deps) ── */
  useEffect(() => {
    if (isHtml || !sessionId || !fileId) {
      setBundle(null)
      return
    }
    let cancelled = false
    setBundleLoading(true)
    api.sessionFiles
      .previewBundle(sessionId, fileId, src)
      .then((b: PreviewBundle) => {
        if (!cancelled) setBundle(b && b.files ? b : null)
      })
      .catch(() => {
        if (!cancelled) setBundle(null) // graceful fall back to single-file mode
      })
      .finally(() => {
        if (!cancelled) setBundleLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [isHtml, sessionId, fileId, src, refreshKey])

  const previewStyle: React.CSSProperties = expanded
    ? { flex: 1, minHeight: 0, width: '100%' }
    : { height: '440px', width: '100%' }

  const containerCls = `flex flex-col rounded-lg border border-border overflow-hidden${
    expanded ? ' fixed inset-4 z-50 bg-base' : ''
  }`

  const resolvedCount = bundle ? Object.keys(bundle.files).length : 0
  const unresolvedCount = bundle ? bundle.unresolved.length : 0

  const toolbar = (
    <div className="flex items-center justify-between px-3 py-1.5 bg-surface border-b border-border flex-shrink-0">
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-[11px] text-muted font-mono truncate">👁 {filename}</span>
        {resolvedCount > 1 && (
          <span
            className="text-[10px] text-accent/80 bg-accent/10 px-1.5 py-0.5 rounded flex-shrink-0"
            title="Local files (components + CSS) resolved into this preview"
          >
            {resolvedCount} files
          </span>
        )}
        {unresolvedCount > 0 && (
          <span
            className="text-[10px] text-amber-500 bg-amber-500/10 px-1.5 py-0.5 rounded flex-shrink-0"
            title={`Imports not found in this session (stubbed): ${bundle?.unresolved.join(', ')}`}
          >
            {unresolvedCount} stubbed
          </span>
        )}
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

  /* ── While the graph is resolving, hold the frame (avoids a flash of the
        single-file fallback that then remounts as the full bundle). ── */
  if (sessionId && fileId && bundleLoading && !bundle) {
    return (
      <div className={containerCls}>
        {toolbar}
        <div style={previewStyle} className="flex items-center justify-center bg-base">
          <span className="text-[11px] text-muted animate-pulse">Resolving imports…</span>
        </div>
      </div>
    )
  }

  /* ── Option B: TSX/JSX via Sandpack ──────────────────────────── */
  let sandpackFiles: Record<string, string>
  let sandpackDeps: Record<string, string>

  if (bundle && bundle.files) {
    // Full module graph: real components + CSS + declared npm deps.
    const harness = [
      "import React from 'react';",
      "import { createRoot } from 'react-dom/client';",
      "import './index.css';",
      `import App from '${bundle.entryImport}';`,
      "createRoot(document.getElementById('root')!).render(<App />);",
    ].join('\n')
    sandpackFiles = {
      ...bundle.files,
      '/index.css': BASE_INDEX_CSS,
      '/index.tsx': harness,
    }
    sandpackDeps = { ...BASE_DEPS, ...(bundle.dependencies || {}) }
  } else {
    // Fallback: single-file stub mode (no session context available).
    const componentName = detectComponent(src)
    const hasDefault = /export\s+default\s+/.test(src)
    const processedCode = prepareCode(src)
    const appCode = hasDefault ? processedCode : `${processedCode}\nexport default ${componentName}`
    sandpackFiles = {
      '/App.tsx': appCode,
      '/index.css': BASE_INDEX_CSS,
      '/index.tsx': [
        "import React from 'react';",
        "import { createRoot } from 'react-dom/client';",
        "import './index.css';",
        "import App from './App';",
        "createRoot(document.getElementById('root')!).render(<App />);",
      ].join('\n'),
    }
    sandpackDeps = BASE_DEPS
  }

  // Re-mount Sandpack whenever the file set or refresh key changes.
  // Content hash so Sandpack remounts on any edit (not just length changes)
  const srcHash = useMemo(() => {
    let h = 5381
    for (let i = 0; i < src.length; i++) h = ((h << 5) + h + src.charCodeAt(i)) | 0
    return h
  }, [src])
  const sandpackKey = `${refreshKey}-${bundle ? 'graph' : 'single'}-${Object.keys(sandpackFiles).length}-${srcHash}`

  return (
    <div className={containerCls}>
      {toolbar}
      <div style={previewStyle}>
        <PreviewErrorBoundary onError={setSandpackError}>
          <SandpackProvider
            key={sandpackKey}
            template="react-ts"
            style={{ height: '100%', width: '100%' }}
            files={sandpackFiles}
            theme={theme === 'dark' ? 'dark' : 'light'}
            customSetup={{ dependencies: sandpackDeps }}
            options={{
              // Load Tailwind synchronously before first paint so utility
              // classes are present on the initial render, not a frame late.
              externalResources: ['https://cdn.tailwindcss.com/3.4.1'],
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
