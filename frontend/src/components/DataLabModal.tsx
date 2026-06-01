import React, { useState, useEffect, useRef } from 'react'
import { api } from '../api/client'
import type { SessionFile } from '../types'

/**
 * DataLab transform modal — the spreadsheet/CSV power-house surface.
 *
 * Self-contained and theme-safe (semantic tokens adapt to light + dark).
 * Talks ONLY to the isolated /api/datalab endpoints — the code-surgery
 * pipeline is never touched. Reused by desktop (SessionFilesTray) and mobile
 * (MobileFilesPanel) so behaviour + look stay in parity.
 */

interface DataLabModalProps {
  sessionId: string
  file: SessionFile
  onClose: () => void
  /** Called after a successful transform so the caller can refresh its file list. */
  onTransformed?: () => void
}

type Phase = 'idle' | 'running' | 'done' | 'blocked' | 'error'

interface QA {
  passed: boolean
  score: number
  verdict: string
  issues: string[]
  warnings: string[]
  diff?: {
    rows_before?: number
    rows_after?: number
    cols_before?: number
    cols_after?: number
    added_cols?: string[]
    removed_cols?: string[]
  }
}

const EXAMPLES = [
  'Keep only rows where status is active, sorted by signup date',
  'Add a column that totals Q1–Q4 for each row',
  'Remove duplicate emails, keeping the most recent',
  'Summarise total revenue per region',
]

export function DataLabModal({ sessionId, file, onClose, onTransformed }: DataLabModalProps) {
  const [prompt, setPrompt] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  const [result, setResult] = useState<any>(null)
  const [error, setError] = useState<string>('')
  const [downloading, setDownloading] = useState(false)
  const [showTransform, setShowTransform] = useState(false)
  const taRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => { taRef.current?.focus() }, [])

  // Close on Escape (but not mid-run, to avoid losing a result).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && phase !== 'running') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [phase, onClose])

  const run = async () => {
    const p = prompt.trim()
    if (!p || phase === 'running') return
    setPhase('running'); setError(''); setResult(null)
    try {
      const r = await api.datalab.transform(sessionId, file.id, p)
      if (r?.ok) {
        setResult(r)
        setPhase('done')
        onTransformed?.()
      } else {
        setResult(r)
        setPhase('blocked')
      }
    } catch (e: any) {
      setError(e?.message || 'Something went wrong running the transform.')
      setPhase('error')
    }
  }

  const handleDownload = async () => {
    if (!result?.file) return
    setDownloading(true)
    try {
      await api.datalab.download(sessionId, result.file.file_id, result.file.filename)
    } catch (e: any) {
      setError(e?.message || 'Download failed.')
    } finally {
      setDownloading(false)
    }
  }

  const qa: QA | undefined = result?.qa
  const d = qa?.diff

  return (
    <div
      className="fixed inset-0 z-[120] flex items-end sm:items-center justify-center bg-black/50 backdrop-blur-sm p-0 sm:p-4"
      onClick={(e) => { if (e.target === e.currentTarget && phase !== 'running') onClose() }}
    >
      <div className="w-full sm:max-w-lg bg-surface border border-border rounded-t-2xl sm:rounded-2xl shadow-2xl max-h-[92vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 px-4 sm:px-5 py-3.5 border-b border-border/60 shrink-0">
          <span className="flex items-center justify-center w-8 h-8 rounded-lg bg-success/10 text-success shrink-0">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18M3 15h18M9 3v18M15 3v18" />
            </svg>
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-semibold text-ink flex items-center gap-1.5">
              Transform with AI
              <span className="text-[9px] font-semibold px-1.5 py-0.5 rounded-md bg-accent/10 text-accent border border-accent/20">Data</span>
            </div>
            <div className="text-[11px] text-muted/70 truncate" title={file.filename}>{file.filename}</div>
          </div>
          <button
            onClick={onClose}
            disabled={phase === 'running'}
            className="shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-muted/60 hover:text-ink hover:bg-overlay disabled:opacity-40 transition-colors"
            title="Close"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        {/* Body */}
        <div className="overflow-y-auto px-4 sm:px-5 py-4 space-y-3.5" style={{ scrollbarWidth: 'thin' }}>
          {(phase === 'idle' || phase === 'running' || phase === 'error') && (
            <>
              <p className="text-[12px] text-muted/80 leading-relaxed">
                Describe the change in plain English. The AI reads your full dataset, writes a transformation,
                and we run it on every row — your original file is preserved and a new version is created.
              </p>
              <textarea
                ref={taRef}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') run() }}
                placeholder="e.g. Keep only active customers and sort by total spend, highest first"
                rows={3}
                disabled={phase === 'running'}
                className="w-full resize-none rounded-xl bg-base/60 border border-border/70 px-3.5 py-2.5 text-[13px] text-ink placeholder:text-muted/50 outline-none focus:border-accent/50 focus:ring-1 focus:ring-accent/30 transition-all disabled:opacity-60"
              />
              {phase !== 'running' && (
                <div className="flex flex-wrap gap-1.5">
                  {EXAMPLES.map((ex) => (
                    <button
                      key={ex}
                      onClick={() => { setPrompt(ex); taRef.current?.focus() }}
                      className="text-[10.5px] px-2 py-1 rounded-lg bg-overlay/60 hover:bg-overlay text-muted/80 hover:text-ink border border-border/50 transition-colors text-left"
                    >
                      {ex}
                    </button>
                  ))}
                </div>
              )}
              {phase === 'running' && (
                <div className="flex items-center gap-2.5 px-3.5 py-3 rounded-xl bg-accent/5 border border-accent/20">
                  <svg className="w-4 h-4 animate-spin text-accent shrink-0" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth={4} />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  <span className="text-[12px] text-ink">Reading data → authoring transform → validating result…</span>
                </div>
              )}
              {phase === 'error' && (
                <div className="px-3.5 py-2.5 rounded-xl bg-danger/10 border border-danger/30 text-[12px] text-danger">
                  {error}
                </div>
              )}
            </>
          )}

          {phase === 'done' && qa && (
            <>
              {/* Data-validated badge (NOT a bare "QA skipped") */}
              <div className="flex items-center gap-2 px-3.5 py-2.5 rounded-xl bg-success/10 border border-success/25">
                <svg className="w-4 h-4 text-success shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.2}><path d="M20 6L9 17l-5-5" /></svg>
                <span className="text-[12px] font-medium text-success">Data validated — code QA not applicable</span>
                <span className="ml-auto text-[10px] font-semibold px-1.5 py-0.5 rounded-md bg-success/15 text-success border border-success/25">{qa.score}/10</span>
              </div>

              {/* Before / after */}
              {d && (
                <div className="grid grid-cols-2 gap-2">
                  <div className="rounded-xl border border-border/60 bg-base/40 px-3 py-2.5">
                    <div className="text-[10px] uppercase tracking-wide text-muted/60 mb-1">Rows</div>
                    <div className="text-[13px] font-semibold text-ink flex items-center gap-1.5">
                      {d.rows_before?.toLocaleString()}
                      <svg className="w-3 h-3 text-muted/50" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path d="M5 12h14M13 6l6 6-6 6" /></svg>
                      <span className="text-success">{d.rows_after?.toLocaleString()}</span>
                    </div>
                  </div>
                  <div className="rounded-xl border border-border/60 bg-base/40 px-3 py-2.5">
                    <div className="text-[10px] uppercase tracking-wide text-muted/60 mb-1">Columns</div>
                    <div className="text-[13px] font-semibold text-ink flex items-center gap-1.5">
                      {d.cols_before}
                      <svg className="w-3 h-3 text-muted/50" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path d="M5 12h14M13 6l6 6-6 6" /></svg>
                      <span className="text-success">{d.cols_after}</span>
                    </div>
                  </div>
                </div>
              )}
              {d && ((d.added_cols?.length ?? 0) > 0 || (d.removed_cols?.length ?? 0) > 0) && (
                <div className="flex flex-wrap gap-1.5">
                  {d.added_cols?.map((c) => (
                    <span key={`a-${c}`} className="text-[10px] px-1.5 py-0.5 rounded-md bg-success/10 text-success border border-success/20">+ {c}</span>
                  ))}
                  {d.removed_cols?.map((c) => (
                    <span key={`r-${c}`} className="text-[10px] px-1.5 py-0.5 rounded-md bg-danger/10 text-danger border border-danger/20">− {c}</span>
                  ))}
                </div>
              )}
              {qa.warnings?.length > 0 && (
                <ul className="text-[11px] text-warning space-y-0.5 list-disc list-inside">
                  {qa.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              )}

              {/* New file + download */}
              <div className="flex items-center gap-3 px-3.5 py-3 rounded-xl border border-border/60 bg-base/40">
                <span className="flex items-center justify-center w-8 h-8 rounded-lg bg-success/10 text-success shrink-0">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round"><path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6" /><path d="M13 3v6h6" /><path d="M18.5 7.5l2 2L16 14l-2.5.5.5-2.5z" /></svg>
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-[12.5px] font-medium text-ink truncate">{result.file.filename}</div>
                  <div className="text-[10.5px] text-muted/60">Added to your session files · original preserved</div>
                </div>
                <button
                  onClick={handleDownload}
                  disabled={downloading}
                  className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-accent/15 hover:bg-accent/25 text-accent text-[11.5px] font-semibold border border-accent/25 disabled:opacity-50 transition-colors"
                >
                  {downloading ? 'Downloading…' : 'Download'}
                </button>
              </div>

              {/* Transformation trail — preserved, collapsed by default */}
              {result.sql && (
                <button
                  onClick={() => setShowTransform((s) => !s)}
                  className="flex items-center gap-1.5 text-[11px] text-muted/70 hover:text-ink transition-colors"
                >
                  <svg className={`w-3 h-3 transition-transform ${showTransform ? 'rotate-90' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}><path d="M9 5l7 7-7 7" /></svg>
                  {showTransform ? 'Hide' : 'Show'} transformation{result.attempts > 1 ? ` · ${result.attempts} attempts` : ''}
                </button>
              )}
              {showTransform && result.sql && (
                <pre className="text-[10.5px] leading-relaxed text-muted/80 bg-base/60 border border-border/50 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">{result.sql}</pre>
              )}
            </>
          )}

          {phase === 'blocked' && (
            <>
              <div className="flex items-start gap-2 px-3.5 py-3 rounded-xl bg-warning/10 border border-warning/30">
                <svg className="w-4 h-4 text-warning shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path d="M12 9v4M12 17h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" /></svg>
                <div className="text-[12px] text-ink">
                  <div className="font-semibold mb-0.5">Couldn't complete this safely</div>
                  <div className="text-muted/80">
                    {result?.error || 'The transform didn’t pass the data-integrity check after several attempts, so nothing was changed.'}
                    {' '}Try rephrasing your request and run it again.
                  </div>
                </div>
              </div>
              {Array.isArray(result?.trail) && result.trail.length > 0 && (
                <details className="text-[11px] text-muted/70">
                  <summary className="cursor-pointer hover:text-ink">What was tried ({result.trail.length})</summary>
                  <ul className="mt-1.5 space-y-1 list-disc list-inside">
                    {result.trail.map((t: any, i: number) => (
                      <li key={i}>{typeof t === 'string' ? t : (t.note || t.reason || JSON.stringify(t))}</li>
                    ))}
                  </ul>
                </details>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center gap-2 px-4 sm:px-5 py-3 border-t border-border/60 shrink-0">
          {phase === 'done' || phase === 'blocked' ? (
            <>
              <button
                onClick={() => { setPhase('idle'); setResult(null); setPrompt(''); setTimeout(() => taRef.current?.focus(), 0) }}
                className="text-[12px] font-medium text-accent hover:underline"
              >
                ↺ New transform
              </button>
              <button
                onClick={onClose}
                className="ml-auto px-4 py-1.5 rounded-lg bg-accent text-white text-[12px] font-semibold hover:opacity-90 transition-opacity"
              >
                Done
              </button>
            </>
          ) : (
            <>
              <span className="text-[10.5px] text-muted/50 hidden sm:block">⌘↵ to run</span>
              <button
                onClick={run}
                disabled={!prompt.trim() || phase === 'running'}
                className="ml-auto flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-accent text-white text-[12px] font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed transition-opacity"
              >
                {phase === 'running' ? 'Working…' : 'Run transform'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
