import React, { useState, useEffect, useRef } from 'react'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'

// ─── Inline SVGs ────────────────────────────────────────────
const IconPlay = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none">
    <polygon points="5 3 19 12 5 21 5 3"/>
  </svg>
)
const IconCheck = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="20 6 9 17 4 12"/>
  </svg>
)
const IconX = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <line x1="18" x2="6" y1="6" y2="18"/><line x1="6" x2="18" y1="6" y2="18"/>
  </svg>
)
const IconSkip = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10"/>
    <line x1="8" x2="16" y1="12" y2="12"/>
  </svg>
)

interface TestResult {
  name: string
  status: 'passed' | 'failed' | 'skipped' | 'error'
  duration_ms?: number
  error?: string
  file?: string
}

interface RunResult {
  run_id: string
  framework: string
  total: number
  passed: number
  failed: number
  skipped: number
  duration_ms: number
  exit_code: number
  tests: TestResult[]
  stdout?: string
  stderr?: string
  status: 'running' | 'done' | 'error'
  error?: string
}

function TestRow({ t }: { t: TestResult }) {
  const [expanded, setExpanded] = useState(false)
  const icon = t.status === 'passed' ? <span className="text-green-400"><IconCheck /></span>
    : t.status === 'failed' ? <span className="text-red-400"><IconX /></span>
    : <span className="text-muted"><IconSkip /></span>
  return (
    <div className={`border-b border-border/50 ${t.status === 'failed' ? 'bg-red-500/5' : ''}`}>
      <button
        className="w-full flex items-start gap-2 px-3 py-1.5 text-left hover:bg-overlay transition group"
        onClick={() => t.error && setExpanded(e => !e)}
      >
        <span className="mt-0.5 flex-shrink-0">{icon}</span>
        <div className="flex-1 min-w-0">
          <span className="text-xs text-ink line-clamp-1">{t.name}</span>
          {t.file && <span className="text-[10px] text-faint block">{t.file}</span>}
        </div>
        {t.duration_ms != null && (
          <span className="text-[10px] text-faint flex-shrink-0">{t.duration_ms}ms</span>
        )}
      </button>
      {expanded && t.error && (
        <div className="mx-3 mb-2 p-2 rounded bg-red-500/10 text-[10px] text-red-300 font-mono whitespace-pre-wrap overflow-x-auto max-h-40">
          {t.error}
        </div>
      )}
    </div>
  )
}

interface Props {
  /** Show as inline chip after apply, not full panel */
  inline?: boolean
  onClose?: () => void
}

export function TestRunnerPanel({ inline = false, onClose }: Props) {
  const { activeSessions: activeSessionId } = useAppStore()
  const [framework, setFramework] = useState<string | null>(null)
  const [detecting, setDetecting] = useState(false)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<RunResult | null>(null)
  const [showOutput, setShowOutput] = useState(false)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Detect test framework on mount
  useEffect(() => {
    if (!activeSessionId) return
    setDetecting(true)
    ;(api as any).tests.detect(activeSessionId)
      .then((r: any) => setFramework(r?.framework || null))
      .catch(() => setFramework(null))
      .finally(() => setDetecting(false))
  }, [activeSessionId])

  const runTests = async () => {
    if (!activeSessionId) return
    setRunning(true)
    setResult(null)
    try {
      const res = await (api as any).tests.run(activeSessionId)
      const runId = res?.run_id
      if (!runId) { setRunning(false); return }
      // Poll for result
      const poll = async () => {
        try {
          const status = await (api as any).tests.status(runId)
          if (status?.status === 'running') {
            pollRef.current = setTimeout(poll, 2000)
          } else {
            setResult(status)
            setRunning(false)
          }
        } catch { setRunning(false) }
      }
      poll()
    } catch { setRunning(false) }
  }

  useEffect(() => () => { if (pollRef.current) clearTimeout(pollRef.current) }, [])

  const summary = result ? (
    result.failed > 0
      ? <span className="text-red-400">{result.failed} failed</span>
      : <span className="text-green-400">All {result.passed} passed</span>
  ) : null

  if (inline && !result) {
    return (
      <div className="flex items-center gap-2 mt-2">
        {detecting ? (
          <span className="text-[11px] text-faint">Detecting tests…</span>
        ) : framework ? (
          <button
            onClick={runTests}
            disabled={running}
            className="flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-lg bg-overlay text-muted hover:text-ink hover:bg-overlay/80 transition disabled:opacity-40"
          >
            <IconPlay />
            {running ? 'Running tests…' : `Run ${framework} tests`}
          </button>
        ) : null}
      </div>
    )
  }

  if (inline && result) {
    return (
      <div className="mt-2 rounded-xl border border-border bg-surface overflow-hidden">
        <div className="flex items-center justify-between px-3 py-2 border-b border-border">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold text-ink">{result.framework} Tests</span>
            {summary}
            <span className="text-[10px] text-faint">{result.duration_ms}ms</span>
          </div>
          <div className="flex items-center gap-1">
            <button onClick={runTests} disabled={running} className="text-[10px] text-muted hover:text-ink px-1.5 py-0.5 rounded hover:bg-overlay transition">
              {running ? '…' : 'Re-run'}
            </button>
            {onClose && <button onClick={onClose} className="text-[10px] text-faint hover:text-ink px-1 rounded hover:bg-overlay">✕</button>}
          </div>
        </div>
        <div className="max-h-48 overflow-y-auto divide-y divide-border/50">
          {result.tests.slice(0, 50).map((t, i) => <TestRow key={i} t={t} />)}
          {result.tests.length > 50 && (
            <div className="px-3 py-1.5 text-[10px] text-faint">…and {result.tests.length - 50} more</div>
          )}
        </div>
        {(result.stdout || result.stderr) && (
          <div className="border-t border-border">
            <button
              onClick={() => setShowOutput(o => !o)}
              className="w-full px-3 py-1.5 text-left text-[10px] text-muted hover:text-ink hover:bg-overlay transition"
            >
              {showOutput ? '▲' : '▼'} Output
            </button>
            {showOutput && (
              <pre className="px-3 pb-3 text-[10px] text-faint font-mono whitespace-pre-wrap overflow-x-auto max-h-40">
                {result.stdout || ''}{result.stderr || ''}
              </pre>
            )}
          </div>
        )}
      </div>
    )
  }

  // Full panel mode
  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-border flex-shrink-0">
        <span className="text-xs font-semibold text-ink">Test Runner</span>
        {onClose && (
          <button onClick={onClose} className="text-faint hover:text-ink text-xs px-1 rounded hover:bg-overlay">✕</button>
        )}
      </div>

      <div className="p-3 border-b border-border flex-shrink-0">
        {detecting ? (
          <div className="text-xs text-faint">Detecting test framework…</div>
        ) : framework ? (
          <div className="flex items-center gap-2">
            <div className="flex-1">
              <div className="text-xs font-medium text-ink">{framework}</div>
              <div className="text-[10px] text-faint">Detected in this session</div>
            </div>
            <button
              onClick={runTests}
              disabled={running || !activeSessionId}
              className="flex items-center gap-1.5 btn-primary text-xs py-1 px-3 disabled:opacity-40"
            >
              <IconPlay />
              {running ? 'Running…' : 'Run Tests'}
            </button>
          </div>
        ) : (
          <div className="text-xs text-muted">
            No test files detected in uploaded files.<br />
            Upload test files (e.g. <code className="text-accent">*.test.ts</code>, <code className="text-accent">test_*.py</code>) to run them.
          </div>
        )}
      </div>

      {running && (
        <div className="flex-1 flex items-center justify-center text-faint text-xs">
          Running tests…
        </div>
      )}

      {!running && result && (
        <>
          <div className="px-3 py-2 border-b border-border flex-shrink-0">
            <div className="flex items-center gap-3 text-xs">
              <span className="text-green-400 font-medium">{result.passed} passed</span>
              {result.failed > 0 && <span className="text-red-400 font-medium">{result.failed} failed</span>}
              {result.skipped > 0 && <span className="text-muted">{result.skipped} skipped</span>}
              <span className="text-faint ml-auto">{result.duration_ms}ms</span>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {result.tests.map((t, i) => <TestRow key={i} t={t} />)}
          </div>
          {(result.stdout || result.stderr) && (
            <div className="border-t border-border flex-shrink-0">
              <button
                onClick={() => setShowOutput(o => !o)}
                className="w-full px-3 py-1.5 text-left text-[10px] text-muted hover:text-ink hover:bg-overlay transition"
              >
                {showOutput ? '▲' : '▼'} Console output
              </button>
              {showOutput && (
                <pre className="px-3 pb-3 text-[10px] text-faint font-mono whitespace-pre-wrap overflow-x-auto max-h-48">
                  {result.stdout || ''}{result.stderr || ''}
                </pre>
              )}
            </div>
          )}
        </>
      )}

      {!running && !result && !detecting && (
        <div className="flex-1 flex items-center justify-center text-faint text-xs">
          {framework ? 'Click Run Tests to start' : 'No test framework detected'}
        </div>
      )}
    </div>
  )
}
