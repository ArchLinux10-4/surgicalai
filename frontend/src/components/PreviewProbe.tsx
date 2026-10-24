import React, { useEffect, useRef, useState } from 'react'
import { SandpackPreview, SandpackProvider, useSandpack } from '@codesandbox/sandpack-react'
import { useThemeStore } from '../stores/themeStore'
import { useAppStore, type PreviewBundlePayload } from '../stores/appStore'

/**
 * Visible Sandpack probe for the agent's read_preview tool.
 *
 * The iframe stays on screen. Sandpack's bundler iframe is the preview
 * target, and hiding it with display:none makes the bundler fail to load.
 * The probe reports evidence only. The backend decides pass or fail.
 */
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

const PER_RUN_MS = 8000
const TOTAL_MS = 18000

interface ProbeRun {
  errors: { file: string; line: number; message: string }[]
  logs: string[]
  timedOut?: boolean
}

function sandpackFiles(bundle: PreviewBundlePayload): { files: Record<string, string>; deps: Record<string, string> } {
  const harness = [
    "import React from 'react';",
    "import { createRoot } from 'react-dom/client';",
    "import './index.css';",
    `import App from '${bundle.entryImport || './App'}';`,
    "createRoot(document.getElementById('root')!).render(<App />);",
  ].join('\n')
  return {
    files: {
      ...(bundle.files || {}),
      '/index.css': BASE_INDEX_CSS,
      '/index.tsx': harness,
    },
    deps: { ...BASE_DEPS, ...(bundle.dependencies || {}) },
  }
}

function isNetworkNoise(text: string): boolean {
  return /favicon|net::ERR|Failed to fetch|NetworkError|ERR_CONNECTION/i.test(text)
}

function RunWatcher({
  onDone,
  onSameOrigin,
}: {
  onDone: (run: ProbeRun) => void
  onSameOrigin: () => void
}) {
  const { listen, sandpack } = useSandpack()
  const errors = useRef<ProbeRun['errors']>([])
  const logs = useRef<string[]>([])
  const finished = useRef(false)
  const armed = useRef(false)
  const statusRef = useRef(sandpack.status)
  const onDoneRef = useRef(onDone)
  const onSameOriginRef = useRef(onSameOrigin)
  statusRef.current = sandpack.status
  onDoneRef.current = onDone
  onSameOriginRef.current = onSameOrigin

  useEffect(() => {
    const finish = (run: ProbeRun) => {
      if (finished.current) return
      finished.current = true
      onDoneRef.current(run)
    }
    const stop = listen((message: any) => {
      const kind = message?.type || message?.action
      if (kind === 'show-error') {
        errors.current.push({
          file: String(message.path || message.title || '').slice(0, 180),
          line: Number(message.line) || 0,
          message: String(message.message || message.title || 'preview error').slice(0, 400),
        })
      }
      if (message?.type === 'console') {
        const items = Array.isArray(message.log) ? message.log : message.log ? [message.log] : []
        for (const item of items) {
          if ((item?.method || item?.type) !== 'error') continue
          const data = item?.data
          const text = Array.isArray(data)
            ? data.map((part: unknown) => String(part)).join(' ')
            : String(data ?? item?.message ?? '')
          if (text && !isNetworkNoise(text)) logs.current.push(text.slice(0, 400))
        }
      }
    })
    const snapshot = (): ProbeRun => ({
      errors: errors.current.slice(0, 5),
      logs: logs.current.slice(0, 5),
    })
    let settleTimer = 0
    const timer = window.setInterval(() => {
      const iframe = document.querySelector('[data-preview-probe] iframe') as HTMLIFrameElement | null
      if (iframe?.src && iframe.src !== 'about:blank') {
        try {
          const origin = new URL(iframe.src, window.location.href).origin
          if (origin === window.location.origin) {
            finished.current = true
            onSameOriginRef.current()
            return
          }
        } catch {
          /* keep waiting */
        }
      }
      const status = statusRef.current
      if ((status === 'idle' || status === 'done') && !armed.current && !finished.current) {
        armed.current = true
        settleTimer = window.setTimeout(() => finish(snapshot()), 500)
      }
    }, 250)
    const cap = window.setTimeout(() => finish({ ...snapshot(), timedOut: true }), PER_RUN_MS)
    return () => {
      stop()
      window.clearInterval(timer)
      window.clearTimeout(cap)
      window.clearTimeout(settleTimer)
    }
  }, [listen])

  return null
}

export function PreviewProbe() {
  const check = useAppStore(s => s.previewCheck)
  const { theme } = useThemeStore()
  const [phase, setPhase] = useState<'proposed' | 'retry' | 'baseline'>('proposed')
  const [mount, setMount] = useState(0)
  const runs = useRef<ProbeRun[]>([])
  const phaseRef = useRef(phase)
  const started = useRef(0)
  const replied = useRef(false)
  phaseRef.current = phase

  useEffect(() => {
    runs.current = []
    phaseRef.current = 'proposed'
    setPhase('proposed')
    setMount(0)
    started.current = Date.now()
    replied.current = false
  }, [check?.checkId])

  const replyAndClear = (payload: Record<string, unknown>) => {
    if (replied.current || !check) return
    replied.current = true
    try { check.reply(payload) } catch { /* the stream may already be gone */ }
    if (useAppStore.getState().previewCheck?.checkId === check.checkId) {
      useAppStore.getState().setPreviewCheck(null)
    }
  }

  const onDone = (run: ProbeRun) => {
    if (replied.current || !check) return
    const elapsed = Date.now() - started.current
    const failed = run.errors.length > 0 || run.logs.length > 0
    const current = phaseRef.current
    if (current === 'baseline') {
      replyAndClear({
        status: 'ok',
        proposed_runs: runs.current.slice(0, 2),
        baseline_run: run,
      })
      return
    }
    if (!failed) {
      replyAndClear({ status: 'ok', proposed_runs: [run] })
      return
    }
    if (current === 'proposed' && elapsed < TOTAL_MS - PER_RUN_MS) {
      runs.current = [run]
      phaseRef.current = 'retry'
      setPhase('retry')
      setMount(value => value + 1)
      return
    }
    const proposed = current === 'retry' ? [...runs.current, run].slice(0, 2) : [run]
    if (check.baselineBundle && elapsed < TOTAL_MS - 4000) {
      runs.current = proposed
      phaseRef.current = 'baseline'
      setPhase('baseline')
      setMount(value => value + 1)
      return
    }
    replyAndClear({
      status: run.timedOut && !proposed.some(item => item.errors.length || item.logs.length) ? 'timeout' : 'ok',
      proposed_runs: proposed,
    })
  }

  const onSameOrigin = () => {
    replyAndClear({ status: 'same_origin', proposed_runs: [] })
  }

  if (!check?.bundle?.files) return null
  const active = phase === 'baseline' && check.baselineBundle ? check.baselineBundle : check.bundle
  if (!active?.files) return null
  const packed = sandpackFiles(active)
  const label = phase === 'baseline' ? 'Comparing with the original' : 'Checking preview'

  return (
    <div
      data-preview-probe
      className="flex flex-col border-t border-border bg-surface flex-shrink-0"
      style={{ height: 168 }}
    >
      <div className="px-3 py-1 text-[11px] text-muted font-mono truncate">
        {label}{check.entry ? ` · ${check.entry}` : ''}
      </div>
      <SandpackProvider
        key={`${check.checkId}-${phase}-${mount}`}
        template="react-ts"
        style={{ height: 140, width: '100%' }}
        files={packed.files}
        theme={theme === 'dark' ? 'dark' : 'light'}
        customSetup={{ dependencies: packed.deps }}
        options={{ externalResources: ['https://cdn.tailwindcss.com/3.4.1'] }}
      >
        <RunWatcher onDone={onDone} onSameOrigin={onSameOrigin} />
        <SandpackPreview
          style={{ height: 140, width: '100%' }}
          showOpenInCodeSandbox={false}
          showRefreshButton={false}
        />
      </SandpackProvider>
    </div>
  )
}
