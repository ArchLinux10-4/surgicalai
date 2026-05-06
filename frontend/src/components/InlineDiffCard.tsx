import React, { useState } from 'react'
import { CheckCircle, XCircle, Download, ChevronDown, ChevronUp, AlertTriangle, FileCode } from 'lucide-react'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import type { SmartResult } from '../types'

interface Props {
  result: SmartResult
  sessionId: string
  onApplied?: (filename: string, modifiedContent: string) => void
}

function ConfidenceBadge({ score }: { score: number }) {
  const color = score >= 8 ? 'text-green-400 bg-green-400/10 border-green-400/30'
    : score >= 6 ? 'text-yellow-400 bg-yellow-400/10 border-yellow-400/30'
    : 'text-red-400 bg-red-400/10 border-red-400/30'
  const label = score >= 8 ? 'High' : score >= 6 ? 'Medium' : 'Low'
  return (
    <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-full border ${color}`}>
      {label} {score}/10
    </span>
  )
}

function DiffLine({ line }: { line: string }) {
  const isAdd = line.startsWith('+') && !line.startsWith('+++')
  const isRemove = line.startsWith('-') && !line.startsWith('---')
  const isHeader = line.startsWith('@@')
  return (
    <div className={`font-mono text-[12px] px-3 py-0.5 leading-relaxed whitespace-pre-wrap break-all ${
      isAdd ? 'bg-green-500/10 text-green-300' :
      isRemove ? 'bg-red-500/10 text-red-300' :
      isHeader ? 'bg-blue-500/10 text-blue-300' :
      'text-zinc-400'
    }`}>
      {line || ' '}
    </div>
  )
}

function FileChangeCard({ filename, fileData, sessionId, onApplied }: {
  filename: string
  fileData: { filename: string; file_id: string; changes: any[] }
  sessionId: string
  onApplied?: (filename: string, content: string) => void
}) {
  const [expanded, setExpanded] = useState(true)
  const [applying, setApplying] = useState<Record<string, boolean>>({})
  const [applied, setApplied] = useState<Record<string, boolean>>({})
  const [skipped, setSkipped] = useState<Record<string, boolean>>({})

  const handleApply = async (change: any) => {
    setApplying(p => ({ ...p, [change.id]: true }))
    try {
      // Get the file content from session
      const fileData2 = await api.sessionFiles.get(sessionId, fileData.file_id)
      const result = await api.surgical.apply({
        file_path: filename,
        changes: [change],
        file_content: fileData2.content,
      })

      if (result.cloud_mode || result.modified_content) {
        // Cloud mode: trigger download
        const blob = new Blob([result.modified_content], { type: 'text/plain' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = filename
        a.click()
        URL.revokeObjectURL(url)
        toast.success(`Downloaded modified ${filename}`)
        onApplied?.(filename, result.modified_content)
      } else {
        toast.success(`Applied change to ${filename}`)
        onApplied?.(filename, result.modified_content || '')
      }
      setApplied(p => ({ ...p, [change.id]: true }))
    } catch (e: any) {
      toast.error(e.message || 'Apply failed')
    } finally {
      setApplying(p => ({ ...p, [change.id]: false }))
    }
  }

  const handleDownload = async (change: any) => {
    try {
      const fileData2 = await api.sessionFiles.get(sessionId, fileData.file_id)
      const result = await api.surgical.apply({
        file_path: filename,
        changes: [change],
        file_content: fileData2.content,
      })
      const content = result.modified_content || fileData2.content
      const blob = new Blob([content], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
      toast.success(`Downloaded ${filename}`)
    } catch (e: any) {
      toast.error(e.message || 'Download failed')
    }
  }

  const handleApplyAll = async () => {
    const unapplied = fileData.changes.filter(c => !applied[c.id] && !skipped[c.id])
    for (const change of unapplied) {
      await handleApply(change)
    }
  }

  return (
    <div className="border border-zinc-700 rounded-xl overflow-hidden mb-3">
      {/* File header */}
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center gap-2.5 px-4 py-2.5 bg-zinc-800/80 hover:bg-zinc-800 transition-colors text-left"
      >
        <FileCode size={14} className="text-blue-400 flex-shrink-0" />
        <span className="text-sm font-semibold text-zinc-100">{filename}</span>
        <span className="text-[11px] text-zinc-500 ml-1">{fileData.changes.length} change{fileData.changes.length !== 1 ? 's' : ''}</span>
        <div className="ml-auto flex items-center gap-2">
          {fileData.changes.length > 1 && !Object.keys(applied).length && (
            <button
              onClick={e => { e.stopPropagation(); handleApplyAll() }}
              className="text-[11px] px-2.5 py-1 bg-green-500/20 text-green-400 border border-green-500/30 rounded-lg hover:bg-green-500/30 transition-colors font-semibold"
            >
              Apply All
            </button>
          )}
          {expanded ? <ChevronUp size={13} className="text-zinc-500" /> : <ChevronDown size={13} className="text-zinc-500" />}
        </div>
      </button>

      {/* Changes */}
      {expanded && fileData.changes.map((change, idx) => (
        <div key={change.id} className="border-t border-zinc-700/60">
          {/* Change header */}
          <div className="flex items-start gap-3 px-4 py-2.5 bg-zinc-900/60">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <code className="text-[12px] text-blue-300 font-mono">{change.symbol?.full_path || change.symbol?.name || 'unknown'}</code>
                <ConfidenceBadge score={change.confidence} />
                {change.confidence < 7 && (
                  <span className="flex items-center gap-1 text-[11px] text-yellow-400">
                    <AlertTriangle size={10} /> Review carefully
                  </span>
                )}
              </div>
              <p className="text-[12px] text-zinc-400 mt-0.5">{change.description}</p>
            </div>
          </div>

          {/* Diff Preview */}
          <div className="border-t border-zinc-800">
            <div className="flex items-center gap-2 px-4 py-1.5 bg-zinc-900 border-b border-zinc-800/60">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Diff Preview</span>
              <span className="text-[10px] text-zinc-600 ml-auto">
                {change.diff.split('\n').filter((l: string) => l.startsWith('+')).length - 1} added{' \u00b7 '}
                {change.diff.split('\n').filter((l: string) => l.startsWith('-')).length - 1} removed
              </span>
            </div>
            <div className="bg-zinc-950 max-h-96 overflow-y-auto">
              {change.diff.split('\n').map((line: string, i: number) => (
                <DiffLine key={i} line={line} />
              ))}
            </div>
          </div>

          {/* Action buttons */}
          <div className="flex items-center gap-2 px-4 py-2.5 bg-zinc-900/40 border-t border-zinc-800">
            {applied[change.id] ? (
              <span className="flex items-center gap-1.5 text-[12px] text-green-400 font-semibold">
                <CheckCircle size={13} /> Applied
              </span>
            ) : skipped[change.id] ? (
              <span className="flex items-center gap-1.5 text-[12px] text-zinc-500">
                <XCircle size={13} /> Skipped
              </span>
            ) : (
              <>
                <button
                  onClick={() => handleApply(change)}
                  disabled={applying[change.id]}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-green-500/20 text-green-400 border border-green-500/30 rounded-lg text-[12px] font-semibold hover:bg-green-500/30 transition-colors disabled:opacity-50"
                >
                  <CheckCircle size={12} />
                  {applying[change.id] ? 'Applying...' : 'Apply'}
                </button>
                <button
                  onClick={() => setSkipped(p => ({ ...p, [change.id]: true }))}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-zinc-800 text-zinc-400 border border-zinc-700 rounded-lg text-[12px] font-semibold hover:bg-zinc-700 transition-colors"
                >
                  <XCircle size={12} /> Skip
                </button>
                <button
                  onClick={() => handleDownload(change)}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-zinc-800 text-zinc-400 border border-zinc-700 rounded-lg text-[12px] font-semibold hover:bg-zinc-700 transition-colors ml-auto"
                  title="Download modified file"
                >
                  <Download size={12} /> Download
                </button>
              </>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

export function InlineDiffCard({ result, sessionId, onApplied }: Props) {
  const fileEntries = Object.entries(result.changes_by_file)
  const totalChanges = fileEntries.reduce((sum, [, v]) => sum + v.changes.length, 0)

  return (
    <div className="mt-2">
      {/* Summary header */}
      <div className="flex items-center gap-2 mb-3 pb-2 border-b border-zinc-700/50">
        <span className="text-sm font-semibold text-zinc-200">✂️ {result.summary || `${totalChanges} change${totalChanges !== 1 ? 's' : ''} ready`}</span>
      </div>

      {result.reasoning && (
        <p className="text-[12px] text-zinc-500 mb-3 italic">{result.reasoning}</p>
      )}

      {/* Per-file cards */}
      {fileEntries.map(([filename, fileData]) => (
        <FileChangeCard
          key={filename}
          filename={filename}
          fileData={fileData}
          sessionId={sessionId}
          onApplied={onApplied}
        />
      ))}

      {result.risks && result.risks.length > 0 && (
        <div className="mt-2 flex items-start gap-2 px-3 py-2 bg-yellow-500/10 border border-yellow-500/20 rounded-lg">
          <AlertTriangle size={13} className="text-yellow-400 mt-0.5 flex-shrink-0" />
          <div className="text-[12px] text-yellow-300">
            <strong>Risks:</strong>
            <ul className="mt-1 space-y-0.5 list-none">
              {result.risks.map((r: string, i: number) => (
                <li key={i} className="flex items-start gap-1.5">
                  <span className="mt-0.5 text-yellow-500">•</span>
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  )
}
