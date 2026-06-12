/**
 * PickablePreview — a self-contained preview with element picking.
 *
 * Renders the same content as LivePreview but with the picker script
 * injected. Accepts the same props so InlineDiffCard can swap between
 * LivePreview (normal mode) and PickablePreview (pick mode) with zero
 * changes to LivePreview.tsx.
 *
 * HTML files  → srcDoc iframe with <script> appended
 * TSX/JSX     → Sandpack with picker as an extra bundled file
 */

import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { SandpackProvider, SandpackPreview } from '@codesandbox/sandpack-react'
import { useThemeStore } from '../../stores/themeStore'
import { useElementPickerStore } from '../../stores/elementPickerStore'
import { api } from '../../api/client'
import { PICKER_SCRIPT } from './pickerScript'
import { detectComponent, prepareCode, BASE_INDEX_CSS, BASE_DEPS } from './previewUtils'

interface PickablePreviewProps {
  code: string
  filename: string
  modifiedCode?: string
  sessionId?: string
  fileId?: string
}

interface PreviewBundle {
  entry: string
  entryImport: string
  files: Record<string, string>
  dependencies: Record<string, string>
  external: string[]
  unresolved: string[]
  component: string
}

/* ── Shared postMessage listener hook ──────────────────────────── */
function usePickerMessages() {
  const addElement = useElementPickerStore((s) => s.addElement)
  const removeElement = useElementPickerStore((s) => s.removeElement)
  const setPickMode = useElementPickerStore((s) => s.setPickMode)

  useEffect(() => {
    function onMsg(e: MessageEvent) {
      if (!e.data?.type) return
      if (e.data.type === 'sai-element-selected') {
        addElement({ index: e.data.index, ...e.data.data })
      }
      if (e.data.type === 'sai-element-deselected') {
        removeElement(e.data.index)
      }
      if (e.data.type === 'sai-picker-escaped') {
        setPickMode(false)
      }
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [addElement, removeElement, setPickMode])
}

/* ── Send pick-mode state to iframe ────────────────────────────── */
function useSendPickMode(iframeRef: React.RefObject<HTMLIFrameElement | null>) {
  const pickMode = useElementPickerStore((s) => s.pickMode)

  useEffect(() => {
    const iframe = iframeRef.current
    if (!iframe?.contentWindow) return
    // Small delay to ensure script is initialized
    const t = setTimeout(() => {
      iframe.contentWindow?.postMessage(
        { type: pickMode ? 'sai-picker-enable' : 'sai-picker-disable' },
        '*'
      )
    }, 100)
    return () => clearTimeout(t)
  }, [pickMode, iframeRef])
}

/* ── HTML Preview (srcDoc iframe) ──────────────────────────────── */
function HtmlPickablePreview({ code, filename }: { code: string; filename: string }) {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  usePickerMessages()
  useSendPickMode(iframeRef)

  const htmlWithPicker = useMemo(() => {
    const scriptTag = `<script>${PICKER_SCRIPT}<\/script>`
    if (code.includes('</body>')) return code.replace('</body>', scriptTag + '</body>')
    if (code.includes('</html>')) return code.replace('</html>', scriptTag + '</html>')
    return code + scriptTag
  }, [code])

  return (
    <iframe
      ref={iframeRef}
      srcDoc={htmlWithPicker}
      sandbox="allow-scripts"
      style={{ width: '100%', height: '100%', border: 'none', background: '#fff', display: 'block' }}
      title={`Pick elements: ${filename}`}
    />
  )
}

/* ── Sandpack Preview (TSX/JSX) ────────────────────────────────── */
function SandpackPickablePreview({
  code,
  filename,
  sessionId,
  fileId,
}: {
  code: string
  filename: string
  sessionId?: string
  fileId?: string
}) {
  const { theme } = useThemeStore()
  const [bundle, setBundle] = useState<PreviewBundle | null>(null)
  const pickMode = useElementPickerStore((s) => s.pickMode)

  usePickerMessages()

  // Listen for sai-picker-ready from Sandpack iframe, then send mode
  useEffect(() => {
    function onReady(e: MessageEvent) {
      if (e.data?.type === 'sai-picker-ready' && e.source) {
        ;(e.source as Window).postMessage(
          { type: pickMode ? 'sai-picker-enable' : 'sai-picker-disable' },
          '*'
        )
      }
    }
    window.addEventListener('message', onReady)
    return () => window.removeEventListener('message', onReady)
  }, [pickMode])

  // Broadcast pick mode changes to all iframes
  useEffect(() => {
    const iframes = document.querySelectorAll('iframe')
    iframes.forEach((f) => {
      try {
        f.contentWindow?.postMessage(
          { type: pickMode ? 'sai-picker-enable' : 'sai-picker-disable' },
          '*'
        )
      } catch { /* cross-origin — picker will catch via sai-picker-ready */ }
    })
  }, [pickMode])

  // Fetch bundle graph
  useEffect(() => {
    if (!sessionId || !fileId) { setBundle(null); return }
    let cancelled = false
    api.sessionFiles
      .previewBundle(sessionId, fileId, code)
      .then((b: PreviewBundle) => { if (!cancelled) setBundle(b?.files ? b : null) })
      .catch(() => { if (!cancelled) setBundle(null) })
    return () => { cancelled = true }
  }, [sessionId, fileId, code])

  // Build Sandpack file set with picker injected
  let sandpackFiles: Record<string, string>
  let sandpackDeps: Record<string, string>

  if (bundle?.files) {
    const harness = [
      "import './sai-picker';",
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
      '/sai-picker.js': PICKER_SCRIPT,
    }
    sandpackDeps = { ...BASE_DEPS, ...(bundle.dependencies || {}) }
  } else {
    const componentName = detectComponent(code)
    const hasDefault = /export\s+default\s+/.test(code)
    const processed = prepareCode(code)
    const appCode = hasDefault ? processed : `${processed}\nexport default ${componentName}`
    sandpackFiles = {
      '/App.tsx': appCode,
      '/index.css': BASE_INDEX_CSS,
      '/index.tsx': [
        "import './sai-picker';",
        "import React from 'react';",
        "import { createRoot } from 'react-dom/client';",
        "import './index.css';",
        "import App from './App';",
        "createRoot(document.getElementById('root')!).render(<App />);",
      ].join('\n'),
      '/sai-picker.js': PICKER_SCRIPT,
    }
    sandpackDeps = BASE_DEPS
  }

  return (
    <SandpackProvider
      template="react-ts"
      style={{ height: '100%', width: '100%' }}
      files={sandpackFiles}
      theme={theme === 'dark' ? 'dark' : 'light'}
      customSetup={{ dependencies: sandpackDeps }}
      options={{ externalResources: ['https://cdn.tailwindcss.com/3.4.1'] }}
    >
      <SandpackPreview
        style={{ height: '100%', width: '100%' }}
        showOpenInCodeSandbox={false}
        showRefreshButton={false}
      />
    </SandpackProvider>
  )
}

/* ── Main export ───────────────────────────────────────────────── */
export function PickablePreview({ code, filename, modifiedCode, sessionId, fileId }: PickablePreviewProps) {
  const src = modifiedCode ?? code
  const isHtml = /\.html?$/i.test(filename)

  const containerStyle: React.CSSProperties = {
    width: '100%',
    height: '100%',
    minHeight: 200,
    position: 'relative',
  }

  return (
    <div style={containerStyle}>
      {isHtml ? (
        <HtmlPickablePreview code={src} filename={filename} />
      ) : (
        <SandpackPickablePreview
          code={src}
          filename={filename}
          sessionId={sessionId}
          fileId={fileId}
        />
      )}
    </div>
  )
}
