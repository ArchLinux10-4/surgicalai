import React, { useState, useRef, useCallback } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { SurgicalPanel } from './SurgicalPanel'
import { DiffView } from './DiffView'
import Editor from '@monaco-editor/react'
import {
  Save, GitBranch, Scissors, RefreshCw, Upload, FolderOpen,
  ChevronDown, Check, History,
} from 'lucide-react'

// ── File Upload Drop Zone ───────────────────────────
function useFileDrop(onFile: (name: string, content: string, language: string) => void) {
  const [dragOver, setDragOver] = useState(false)

  const detectLang = (name: string) => {
    const ext = name.split('.').pop()?.toLowerCase() || ''
    const map: Record<string, string> = {
      py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript',
      jsx: 'javascript', go: 'go', rs: 'rust', java: 'java', cs: 'csharp',
      cpp: 'cpp', c: 'c', rb: 'ruby', php: 'php', swift: 'swift',
      kt: 'kotlin', sh: 'shell', bash: 'shell', html: 'html', css: 'css',
      scss: 'scss', json: 'json', yaml: 'yaml', yml: 'yaml', md: 'markdown',
      toml: 'toml', sql: 'sql', xml: 'xml', vue: 'html',
    }
    return map[ext] || 'plaintext'
  }

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = (ev) => {
      const content = ev.target?.result as string
      onFile(file.name, content, detectLang(file.name))
    }
    reader.readAsText(file)
  }, [onFile])

  return {
    dragOver,
    dropProps: {
      onDragOver: (e: React.DragEvent) => { e.preventDefault(); setDragOver(true) },
      onDragLeave: () => setDragOver(false),
      onDrop: handleDrop,
    },
  }
}

// ── Open local file via input ───────────────────────
function OpenFileButton({ onFile }: { onFile: (name: string, content: string, lang: string) => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const detectLang = (name: string) => {
    const ext = name.split('.').pop()?.toLowerCase() || ''
    const map: Record<string, string> = {
      py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript',
      jsx: 'javascript', go: 'go', rs: 'rust', java: 'java',
      cs: 'csharp', cpp: 'cpp', c: 'c', sh: 'shell', html: 'html',
      css: 'css', json: 'json', yaml: 'yaml', yml: 'yaml', md: 'markdown', sql: 'sql',
    }
    return map[ext] || 'plaintext'
  }

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = (ev) => {
      onFile(file.name, ev.target?.result as string, detectLang(file.name))
    }
    reader.readAsText(file)
    e.target.value = ''
  }

  return (
    <>
      <input ref={inputRef} type="file" className="hidden" onChange={handleChange} accept="*/*" />
      <button
        onClick={() => inputRef.current?.click()}
        className="btn-ghost gap-1.5 text-[12px]"
        title="Open local file"
      >
        <Upload size={13} /> Upload File
      </button>
    </>
  )
}

// ── No-file empty state ─────────────────────────────
function NoFileState({ onFile }: { onFile: (name: string, content: string, lang: string) => void }) {
  const { dragOver, dropProps } = useFileDrop(onFile)
  const inputRef = useRef<HTMLInputElement>(null)

  const detectLang = (name: string) => {
    const ext = name.split('.').pop()?.toLowerCase() || ''
    const map: Record<string, string> = {
      py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript',
      jsx: 'javascript', go: 'go', rs: 'rust', java: 'java', cs: 'csharp',
    }
    return map[ext] || 'plaintext'
  }

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = (ev) => onFile(file.name, ev.target?.result as string, detectLang(file.name))
    reader.readAsText(file)
    e.target.value = ''
  }

  return (
    <div
      {...dropProps}
      className={`flex flex-col items-center justify-center h-full gap-4 transition-all ${dragOver ? 'drag-active bg-accent/5' : ''}`}
    >
      <input ref={inputRef} type="file" className="hidden" onChange={handleInputChange} accept="*/*" />
      <div className={`w-20 h-20 rounded-2xl border-2 border-dashed flex items-center justify-center transition-colors ${
        dragOver ? 'border-accent bg-accent/10' : 'border-border'
      }`}>
        {dragOver
          ? <Upload size={32} className="text-accent" />
          : <FolderOpen size={32} className="text-faint" />
        }
      </div>
      <div className="text-center">
        <div className="text-base font-semibold text-ink mb-1">
          {dragOver ? 'Drop to open' : 'No file open'}
        </div>
        <div className="text-sm text-muted leading-relaxed">
          Open a file from the sidebar,<br />or drop any code file here.
        </div>
      </div>
      <button
        onClick={() => inputRef.current?.click()}
        className="btn-md bg-overlay border border-border text-ink hover:bg-surface gap-2"
      >
        <Upload size={15} /> Open Local File
      </button>
      <p className="text-[11px] text-faint">All edits stay local — nothing leaves your machine</p>
    </div>
  )
}

// ── Code Panel ──────────────────────────────────────
export function CodePanel() {
  const {
    activeFile, setActiveFile, rightTab, setRightTab,
    surgicalPanelOpen, surgicalAnalysis, settings,
  } = useAppStore()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [content, setContent] = useState<string>('')

  React.useEffect(() => {
    if (activeFile) setContent(activeFile.content)
  }, [activeFile?.path])

  // Handle file opened via drag-drop or upload button
  const handleLocalFile = useCallback((name: string, fileContent: string, language: string) => {
    const lines = fileContent.split('\n').length
    setActiveFile({
      path: name, // display name only (not a server path)
      name,
      content: fileContent,
      language,
      lines,
      size: fileContent.length,
      extension: '.' + (name.split('.').pop() || ''),
    } as any)
    setContent(fileContent)
    setRightTab('editor')
    toast.success(`Opened ${name}`, `${lines} lines · ${language}`)
  }, [])

  const { dragOver, dropProps } = useFileDrop(handleLocalFile)

  const handleSave = async () => {
    if (!activeFile) return
    setSaving(true)
    try {
      await api.files.save(activeFile.path, content)
      setActiveFile({ ...activeFile, content })
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
      toast.success('File saved')
    } catch (e: any) {
      toast.error('Save failed', e.message)
    }
    setSaving(false)
  }

  const handleReload = async () => {
    if (!activeFile) return
    try {
      const f = await api.files.read(activeFile.path)
      setActiveFile(f)
      setContent(f.content)
      toast.info('File reloaded')
    } catch (e: any) {
      toast.error('Reload failed', e.message)
    }
  }

  if (!activeFile) {
    return <NoFileState onFile={handleLocalFile} />
  }

  const tabs = [
    { id: 'editor' as const, label: '📄 Editor' },
    { id: 'diff' as const, label: `✂ Changes${surgicalAnalysis ? ` (${surgicalAnalysis.changes.length})` : ''}` },
    { id: 'git' as const, label: '🌿 Git' },
  ]

  const isServerFile = activeFile.path.startsWith('/')
  const fileName = activeFile.path.split('/').pop() || activeFile.path
  const dirPath = isServerFile ? activeFile.path.split('/').slice(0, -1).join('/') : null

  return (
    <div
      {...dropProps}
      className={`flex flex-col h-full transition-all ${dragOver ? 'drag-active' : ''}`}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface/50 flex-shrink-0">
        <div className="flex-1 min-w-0 flex items-center gap-1.5 text-sm">
          {dirPath && <span className="text-faint truncate text-[12px] hidden sm:block">{dirPath}/</span>}
          <span className="font-semibold text-ink flex-shrink-0">{fileName}</span>
          <span className="text-[11px] text-faint flex-shrink-0">{activeFile.lines}L · {activeFile.language}</span>
        </div>

        <div className="flex items-center gap-1 flex-shrink-0">
          <OpenFileButton onFile={handleLocalFile} />
          {isServerFile && (
            <button onClick={handleReload} className="btn-icon" title="Reload from disk">
              <RefreshCw size={13} />
            </button>
          )}
          <button
            onClick={handleSave}
            className={`btn-sm gap-1.5 transition-all ${
              saved ? 'bg-success/10 text-success border border-success/30' : 'bg-accent text-base'
            }`}
          >
            {saved ? <><Check size={12} /> Saved</> : <><Save size={12} /> {saving ? '…' : 'Save'}</>}
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="tab-bar flex-shrink-0 bg-surface/30">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setRightTab(tab.id)}
            className={rightTab === tab.id ? 'tab-item-active' : 'tab-item'}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden relative min-h-0">
        {rightTab === 'editor' && (
          <Editor
            height="100%"
            language={activeFile.language}
            value={content}
            onChange={(v) => setContent(v || '')}
            theme="vs-dark"
            options={{
              fontSize: settings?.font_size || 14,
              minimap: { enabled: true },
              scrollBeyondLastLine: false,
              wordWrap: 'off',
              renderLineHighlight: 'all',
              smoothScrolling: true,
              cursorBlinking: 'smooth',
              bracketPairColorization: { enabled: true },
              formatOnPaste: false,
              tabSize: 2,
              lineNumbersMinChars: 3,
              padding: { top: 8, bottom: 8 },
            }}
          />
        )}
        {rightTab === 'diff' && <SurgicalPanel />}
        {rightTab === 'git' && <GitPanel />}
      </div>

      {/* Drop overlay */}
      {dragOver && (
        <div className="absolute inset-0 bg-accent/10 flex items-center justify-center pointer-events-none z-10">
          <div className="bg-surface border-2 border-accent border-dashed rounded-2xl px-12 py-8 text-center">
            <Upload size={36} className="text-accent mx-auto mb-3" />
            <div className="text-lg font-bold text-ink">Drop to open file</div>
            <div className="text-sm text-muted mt-1">Opens in editor (read-only from disk)</div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Git Panel ───────────────────────────────────────
function GitPanel() {
  const { activeFile } = useAppStore()
  const [status, setStatus] = useState<any>(null)
  const [log, setLog] = useState<any[]>([])
  const [commitMsg, setCommitMsg] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const repoPath = activeFile?.path.split('/').slice(0, -1).join('/') || ''

  const loadGit = async () => {
    if (!repoPath) return
    setLoading(true)
    try {
      const [s, l] = await Promise.all([api.git.status(repoPath), api.git.log(repoPath)])
      setStatus(s); setLog(l); setError(null)
    } catch (e: any) { setError(e.message) }
    setLoading(false)
  }

  React.useEffect(() => { loadGit() }, [activeFile?.path])

  const handleCommit = async () => {
    if (!commitMsg.trim() || !repoPath) return
    try {
      await api.git.commit({ repo_path: repoPath, message: commitMsg })
      setCommitMsg(''); loadGit()
      toast.success('Committed', commitMsg)
    } catch (e: any) { toast.error('Commit failed', e.message) }
  }

  if (loading) return <div className="flex items-center justify-center h-32 text-faint text-sm">Loading git status…</div>
  if (!status?.is_repo) return (
    <div className="flex flex-col items-center justify-center h-32 text-faint text-sm gap-2">
      <GitBranch size={24} className="opacity-40" />
      {error ? `Error: ${error}` : 'Not a git repository'}
    </div>
  )

  return (
    <div className="p-4 h-full overflow-y-auto text-sm text-ink">
      <div className="flex items-center gap-2 mb-4">
        <GitBranch size={14} className="text-success" />
        <strong className="text-ink">{status.branch}</strong>
        <button onClick={loadGit} className="btn-icon ml-auto"><RefreshCw size={13} /></button>
      </div>

      {(status.staged.length > 0 || status.unstaged.length > 0 || status.untracked.length > 0) && (
        <div className="mb-4 space-y-3">
          {status.staged.length > 0 && (
            <div>
              <div className="text-success text-xs font-semibold mb-1">Staged ({status.staged.length})</div>
              {status.staged.map((f: string) => <div key={f} className="text-muted text-xs pl-3">{f}</div>)}
            </div>
          )}
          {status.unstaged.length > 0 && (
            <div>
              <div className="text-danger text-xs font-semibold mb-1">Modified ({status.unstaged.length})</div>
              {status.unstaged.map((f: string) => <div key={f} className="text-muted text-xs pl-3">{f}</div>)}
            </div>
          )}
          {status.untracked.length > 0 && (
            <div>
              <div className="text-faint text-xs font-semibold mb-1">Untracked ({status.untracked.length})</div>
              {status.untracked.map((f: string) => <div key={f} className="text-muted text-xs pl-3">{f}</div>)}
            </div>
          )}
          <div className="flex gap-2 pt-1">
            <input
              value={commitMsg}
              onChange={(e) => setCommitMsg(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleCommit()}
              placeholder="Commit message…"
              className="input text-sm flex-1"
            />
            <button onClick={handleCommit} className="btn-success">Commit</button>
          </div>
        </div>
      )}

      {log.length > 0 && (
        <div>
          <div className="section-label px-0 mb-2">Recent Commits</div>
          {log.slice(0, 10).map((c, i) => (
            <div key={i} className="py-2 border-b border-border/50">
              <div className="text-ink text-sm">{c.message}</div>
              <div className="text-faint text-[11px] mt-0.5 font-mono">{c.hash?.slice(0,7)} · {c.author} · {c.when}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
