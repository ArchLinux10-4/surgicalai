import React, { useState, useRef, useCallback } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vscDarkPlus, oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { useThemeStore } from '../stores/themeStore'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import type { SmartResult, NewFile } from '../types'
import { InlineDiffCard } from './InlineDiffCard'
import { Add, Check, ContentCopy, Description, FileDownload, KeyboardArrowDown, KeyboardArrowUp } from '@mui/icons-material';

const LANG_LABELS: Record<string, string> = {
  typescript: 'TypeScript', tsx: 'TSX', javascript: 'JavaScript',
  jsx: 'JSX', python: 'Python', go: 'Go', rust: 'Rust',
  css: 'CSS', html: 'HTML', json: 'JSON', yaml: 'YAML',
  bash: 'Shell', sql: 'SQL', markdown: 'Markdown',
}

const LANG_COLORS: Record<string, string> = {
  typescript: 'text-cyan-400 bg-cyan-400/10 border-cyan-400/20',
  tsx:        'text-cyan-300 bg-cyan-300/10 border-cyan-300/20',
  javascript: 'text-yellow-400 bg-yellow-400/10 border-yellow-400/20',
  jsx:        'text-yellow-300 bg-yellow-300/10 border-yellow-300/20',
  python:     'text-blue-400 bg-blue-400/10 border-blue-400/20',
  go:         'text-teal-400 bg-teal-400/10 border-teal-400/20',
  rust:       'text-orange-400 bg-orange-400/10 border-orange-400/20',
}

function detectLang(filename: string, lang: string): string {
  if (lang && lang !== 'text') return lang.toLowerCase()
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const map: Record<string, string> = {
    ts: 'typescript', tsx: 'tsx', js: 'javascript', jsx: 'jsx',
    py: 'python', go: 'go', rs: 'rust', css: 'css', html: 'html',
    json: 'json', yaml: 'yaml', yml: 'yaml', sh: 'bash', sql: 'sql', md: 'markdown',
  }
  return map[ext] || 'text'
}

interface SingleFileCardProps {
  file: NewFile
  sessionId: string
  index: number
  onSaved: (filename: string) => void
}

function SingleFileCard({ file, sessionId, index, onSaved }: SingleFileCardProps) {
  const theme = useThemeStore(s => s.theme)
  // Collapsed by default for every file — keeps long files from blowing up scroll height
  const [collapsed, setCollapsed] = useState(true)
  const cardRef = useRef<HTMLDivElement>(null)
  const [saving, setSaving] = useState(false)
  const savedKey = `sai-added:${sessionId}:${file.filename}`
  const [saved, setSaved] = useState(() => localStorage.getItem(savedKey) === '1')
  const [copied, setCopied] = useState(false)

  const lang = detectLang(file.filename, file.language)
  const label = LANG_LABELS[lang] || lang.toUpperCase() || 'CODE'
  const colorClass = LANG_COLORS[lang] || 'text-muted bg-muted/10 border-muted/20'
  const lines = file.content.split('\n')
  const PREVIEW = 12
  const isLong = lines.length > PREVIEW
  const displayCode = collapsed ? lines.slice(0, PREVIEW).join('\n') : file.content

  const handleSave = async () => {
    if (saved || saving) return
    setSaving(true)
    try {
      await api.sessionFiles.upload(sessionId, {
        filename: file.filename,
        content: file.content,
        language: lang,
        origin: 'created',
      })
      setSaved(true)
      localStorage.setItem(savedKey, '1')
      onSaved(file.filename)
      // Immediately refresh the session file list in the store so the Files tray updates
      try {
        const files = await api.sessionFiles.list(sessionId)
        useAppStore.getState().setSessionFiles(files)
      } catch (_) {}
      // Open Files panel so user can see the file immediately
      useAppStore.getState().setSidebarTab('files')
      useAppStore.getState().setSidebarPanelOpen(true)
      toast.success(`${file.filename} added to session`)
    } catch (e: any) {
      toast.error(`Failed to save: ${e.message}`)
    } finally {
      setSaving(false)
    }
  }

  const handleDownload = () => {
    const blob = new Blob([file.content], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = file.filename.split('/').pop() || file.filename
    a.click()
    URL.revokeObjectURL(url)
  }

  const handleCopy = () => {
    navigator.clipboard.writeText(file.content)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div ref={cardRef} className="rounded-xl overflow-hidden border border-border/60 bg-base shadow-sm">

      {/* Header */}
      <div className="flex items-center justify-between px-3.5 py-2.5 bg-surface/80 border-b border-border/60">
        <div className="flex items-center gap-2.5 min-w-0">
          {/* Traffic lights */}
          <div className="flex gap-1.5 flex-shrink-0">
            <span className="w-3 h-3 rounded-full bg-red-500/60" />
            <span className="w-3 h-3 rounded-full bg-yellow-500/60" />
            <span className="w-3 h-3 rounded-full bg-green-500/60" />
          </div>
          <Description sx={{ fontSize: 13 }} className="text-muted flex-shrink-0" />
          <span className="text-[12px] font-mono text-ink truncate">{file.filename}</span>
          <span className={`flex-shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded border ${colorClass}`}>
            {label}
          </span>
          <span className="text-[11px] text-muted flex-shrink-0">{lines.length} lines</span>
        </div>

        <div className="flex items-center gap-1 flex-shrink-0">
          <button
            onClick={handleCopy}
            className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-muted hover:text-ink hover:bg-overlay/60 transition-colors"
          >
            {copied ? <><Check sx={{ fontSize: 12 }} className="text-success" /><span className="text-success">Copied</span></> : <><ContentCopy sx={{ fontSize: 12 }} /><span>Copy</span></>}
          </button>
          <button
            onClick={handleDownload}
            className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-muted hover:text-ink hover:bg-overlay/60 transition-colors"
          >
            <FileDownload sx={{ fontSize: 12 }} /><span>Download</span>
          </button>
          {isLong && (
            <button
              onClick={() => setCollapsed(c => !c)}
              className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-muted hover:text-ink hover:bg-overlay/60 transition-colors"
            >
              {collapsed ? <KeyboardArrowDown sx={{ fontSize: 12 }} /> : <KeyboardArrowUp sx={{ fontSize: 12 }} />}
              <span>{collapsed ? `Expand` : 'Collapse'}</span>
            </button>
          )}
        </div>
      </div>

      {/* File summary */}
      {file.summary && (
        <div className="px-4 py-2 bg-surface/40 border-b border-border/40 text-[12px] text-muted">
          {file.summary}
        </div>
      )}

      {/* Code */}
      <div
        className="relative"
        // When expanded, cap the height and scroll internally (ChatGPT/Claude-style)
        // so even a 1,000-line file never dominates the page. Collapsed view only
        // shows PREVIEW lines, so no cap is needed there.
        style={!collapsed && isLong ? { maxHeight: '60vh', overflowY: 'auto' } : undefined}
      >
        <SyntaxHighlighter
          language={lang}
          style={theme === 'dark' ? vscDarkPlus : oneLight}
          showLineNumbers
          wrapLines
          lineNumberStyle={{ minWidth: '2.5em', paddingRight: '1em', color: 'rgb(var(--c-faint))', fontSize: '11px', userSelect: 'none' }}
          customStyle={{ margin: 0, padding: '1rem', background: 'transparent', fontSize: '13px', lineHeight: '1.6', fontFamily: '"JetBrains Mono","Fira Code","Cascadia Code",Menlo,monospace' }}
        >
          {displayCode}
        </SyntaxHighlighter>

        {collapsed && isLong && (
          <div className="absolute bottom-0 left-0 right-0 h-14 bg-gradient-to-t from-base to-transparent pointer-events-none" />
        )}
      </div>

      {/* Footer expand/collapse — symmetric control so long files can be collapsed
          from the bottom without scrolling back up to the header (GitHub-style) */}
      {isLong && (
        <button
          onClick={() => {
            if (collapsed) {
              setCollapsed(false)
            } else {
              setCollapsed(true)
              // Bring the card header back into view so collapsing from the bottom
              // never strands the user in empty space.
              requestAnimationFrame(() => cardRef.current?.scrollIntoView({ block: 'nearest' }))
            }
          }}
          className="w-full py-2 text-[12px] text-muted hover:text-ink hover:bg-surface/60 transition-colors border-t border-border/60 flex items-center justify-center gap-1.5"
        >
          {collapsed ? (
            <><KeyboardArrowDown sx={{ fontSize: 13 }} /> Show {lines.length - PREVIEW} more lines</>
          ) : (
            <><KeyboardArrowUp sx={{ fontSize: 13 }} /> Collapse</>
          )}
        </button>
      )}

      {/* Action bar */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-surface/60 border-t border-border/60">
        <span className="text-[11px] text-muted">
          {saved ? '✅ Added to session — you can now edit this file' : 'Add to session to edit with AI'}
        </span>
        <button
          onClick={handleSave}
          disabled={saved || saving}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-semibold transition-all ${
            saved
              ? 'bg-success/20 text-success border border-success/40 cursor-default'
              : saving
              ? 'bg-accent/20 text-accent border border-accent/40 cursor-wait'
              : 'bg-accent text-base hover:bg-accent/90 active:scale-95'
          }`}
        >
          {saved ? (
            <><Check sx={{ fontSize: 13 }} /> Saved</>
          ) : saving ? (
            <>Saving...</>
          ) : (
            <><Add sx={{ fontSize: 13 }} /> Add to session</>
          )}
        </button>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────

interface NewFileCardProps {
  result: SmartResult
  sessionId: string
}

export function NewFileCard({ result, sessionId }: NewFileCardProps) {
  const [savedFiles, setSavedFiles] = useState<Set<string>>(new Set())
  const newFiles = result.new_files || []
  const editFiles = result.changes_by_file ? Object.entries(result.changes_by_file) : []
  const hasEdits = editFiles.length > 0

  if (!newFiles.length) return null

  return (
    <div className="flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-semibold text-muted/80 uppercase tracking-wider">
            {newFiles.length === 1 ? 'New file' : `${newFiles.length} new files`}
          </span>
          {savedFiles.size > 0 && (
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-success/20 text-success border border-success/30 font-medium">
              {savedFiles.size} saved
            </span>
          )}
        </div>
        {result.summary && (
          <span className="text-[12px] text-muted truncate max-w-xs">{result.summary}</span>
        )}
      </div>

      {/* File cards */}
      {newFiles.map((file, i) => (
        <SingleFileCard
          key={file.filename}
          file={file}
          sessionId={sessionId}
          index={i}
          onSaved={(fname) => {
        setSavedFiles(prev => new Set([...prev, fname]))
      }}
        />
      ))}

      {/* Risks */}
      {result.risks && result.risks.length > 0 && (
        <div className="rounded-lg border border-warning/30 bg-warning/10 px-4 py-2.5">
          <p className="text-[11px] font-semibold text-warning mb-1">Things to review</p>
          {result.risks.map((r, i) => (
            <p key={i} className="text-[12px] text-warning/80">• {r}</p>
          ))}
        </div>
      )}

      {/* Mixed create+edit: show edit diffs below the new file cards */}
      {hasEdits && (
        <div className="flex flex-col gap-2">
          <span className="text-[11px] font-semibold text-muted/80 uppercase tracking-wider">
            Existing file edits
          </span>
          <InlineDiffCard result={result} sessionId={sessionId} />
        </div>
      )}
    </div>
  )
}
