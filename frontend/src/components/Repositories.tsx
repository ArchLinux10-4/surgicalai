import React, { useEffect, useState } from 'react'
import { Close, FolderOpen, GitHub, Refresh } from '@mui/icons-material'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { useAppStore } from '../stores/appStore'
import type { RepoPreview, Repository } from '../types'

export function OpenFolderModal() {
  const open = useAppStore(s => s.openFolderOpen)
  const setOpen = useAppStore(s => s.setOpenFolderOpen)
  const repositories = useAppStore(s => s.repositories)
  const setRepositories = useAppStore(s => s.setRepositories)
  const settings = useAppStore(s => s.settings)
  const [path, setPath] = useState('')
  const [preview, setPreview] = useState<RepoPreview | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [busy, setBusy] = useState(false)
  const [browsing, setBrowsing] = useState(false)
  const [fsPath, setFsPath] = useState('')
  const [entries, setEntries] = useState<{ name: string; path: string }[]>([])
  const [parent, setParent] = useState('')

  const localInstall = !!settings && !settings.is_hosted

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, setOpen])

  useEffect(() => {
    if (!open || !localInstall) return
    api.repositories.list().then(setRepositories).catch(() => {})
    loadFs('')
  }, [open, localInstall])

  useEffect(() => {
    if (!open) return
    const trimmed = path.trim()
    if (!trimmed) { setPreview(null); setPreviewError(''); return }
    const handle = window.setTimeout(() => {
      api.repositories.preview(trimmed)
        .then((res) => { setPreview(res); setPreviewError('') })
        .catch((e: any) => { setPreview(null); setPreviewError(e.message || 'Folder not found') })
    }, 250)
    return () => window.clearTimeout(handle)
  }, [path, open])

  if (!open || !localInstall) return null

  const loadFs = async (next: string) => {
    try {
      const res = await api.repositories.browseFs(next)
      setFsPath(res.path)
      setParent(res.parent || '')
      setEntries(res.entries || [])
    } catch (e: any) {
      toast.error('Could not list that folder', e.message)
    }
  }

  const choose = (next: string) => {
    setPath(next)
    setPreview(null)
  }

  const browseNative = async () => {
    setBrowsing(true)
    try {
      const res = await api.repositories.browseNative()
      if (res.path) choose(res.path)
    } catch (e: any) {
      toast.error('Folder picker unavailable', e.message)
    } finally {
      setBrowsing(false)
    }
  }

  const openPath = async (next: string) => {
    const target = next.trim()
    if (!target) return
    setBusy(true)
    try {
      await api.repositories.open(target)
      const list = await api.repositories.list()
      setRepositories(list)
      toast.success('Folder opened', target)
      setOpen(false)
      setPath('')
      setPreview(null)
    } catch (e: any) {
      toast.error('Could not open folder', e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4" onClick={() => setOpen(false)}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="open-folder-title"
        className="flex max-h-[min(640px,90vh)] w-full max-w-lg flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-modal outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <h2 id="open-folder-title" className="text-sm font-semibold text-ink">Open Folder</h2>
            <p className="mt-0.5 text-[12px] text-muted">Chats in this folder can read the project on this computer.</p>
          </div>
          <button type="button" className="btn-icon" onClick={() => setOpen(false)} aria-label="Close">
            <Close sx={{ fontSize: 16 }} />
          </button>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-3">
          <div>
            <label className="text-[11px] font-semibold text-muted" htmlFor="open-folder-path">Folder path</label>
            <div className="mt-1 flex gap-2">
              <input
                id="open-folder-path"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="/Users/you/projects/app"
                className="input flex-1 font-mono text-[12px]"
              />
              <button type="button" className="btn-ghost shrink-0 border border-border px-3 text-[12px]" onClick={browseNative} disabled={browsing}>
                {browsing ? 'Opening…' : 'Browse'}
              </button>
            </div>
            {previewError && <p className="mt-1 text-[11px] text-danger">{previewError}</p>}
            {preview && (
              <div className="mt-2 rounded-lg border border-border bg-base/40 px-3 py-2 text-[12px]">
                <div className="font-medium text-ink">{preview.name}</div>
                <div className="mt-0.5 truncate font-mono text-[11px] text-faint">{preview.path}</div>
                <div className="mt-1 text-muted">
                  {preview.is_git
                    ? <>Git{preview.branch ? ` · ${preview.branch}` : ''}{preview.dirty ? ' · uncommitted changes' : ''}</>
                    : 'Not a git repository'}
                </div>
                {preview.suggested_github && (
                  <div className="mt-1 flex items-center gap-1.5 text-muted">
                    <GitHub sx={{ fontSize: 13 }} />
                    <span>{preview.suggested_github.owner}/{preview.suggested_github.repo}</span>
                    {preview.github_match && <span className="text-[10px] text-accent">Connected</span>}
                  </div>
                )}
              </div>
            )}
          </div>

          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[11px] font-semibold text-muted">On this computer</span>
              {parent && (
                <button type="button" className="text-[11px] text-accent" onClick={() => loadFs(parent)}>Up</button>
              )}
            </div>
            <div className="max-h-40 overflow-y-auto rounded-lg border border-border">
              <button type="button" className="block w-full truncate px-3 py-1.5 text-left font-mono text-[11px] text-faint hover:bg-overlay" onClick={() => choose(fsPath)}>
                {fsPath || 'Home'}
              </button>
              {entries.map((entry) => (
                <div key={entry.path} className="flex items-center border-t border-border/60">
                  <button type="button" className="flex-1 truncate px-3 py-1.5 text-left text-[12px] text-ink hover:bg-overlay" onClick={() => loadFs(entry.path)}>
                    {entry.name}
                  </button>
                  <button type="button" className="px-2 text-[11px] text-accent" onClick={() => choose(entry.path)}>Select</button>
                </div>
              ))}
              {entries.length === 0 && <p className="px-3 py-2 text-[12px] text-faint">No folders here</p>}
            </div>
          </div>

          {repositories.length > 0 && (
            <div>
              <div className="mb-1 text-[11px] font-semibold text-muted">Recent</div>
              <div className="space-y-1">
                {repositories.filter(repo => !repo.removed).slice(0, 6).map((repo) => (
                  <button
                    key={repo.id}
                    type="button"
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-overlay"
                    onClick={() => openPath(repo.root_path)}
                  >
                    <FolderOpen sx={{ fontSize: 14 }} className="text-muted" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12px] font-medium text-ink">{repo.name}</span>
                      <span className="block truncate font-mono text-[10px] text-faint">{repo.root_path}</span>
                    </span>
                    {repo.missing && <span className="text-[10px] text-danger">Missing</span>}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-border px-4 py-3">
          <button type="button" className="btn-ghost px-3 text-[12px]" onClick={() => setOpen(false)}>Cancel</button>
          <button type="button" className="btn-primary px-3 text-[12px]" disabled={busy || !path.trim()} onClick={() => openPath(path)}>
            {busy ? 'Opening…' : 'Open'}
          </button>
        </div>
      </div>
    </div>
  )
}

export function RepoChip({ sessionId, variant }: { sessionId: string | null; variant: 'desktop' | 'mobile' }) {
  const repositories = useAppStore(s => s.repositories)
  const setRepositories = useAppStore(s => s.setRepositories)
  const sessions = useAppStore(s => s.sessions)
  const settings = useAppStore(s => s.settings)
  const setOpen = useAppStore(s => s.setOpenFolderOpen)
  const localInstall = !!settings && !settings.is_hosted
  useEffect(() => {
    if (!localInstall) return
    api.repositories.list(true).then(setRepositories).catch(() => {})
  }, [localInstall, setRepositories])
  if (!localInstall || !sessionId) return null
  const session = sessions.find(s => s.id === sessionId)
  if (!session?.repository_id) return null
  const repo = repositories.find(r => r.id === session.repository_id)
  const desktop = variant === 'desktop'
  const label = repo?.removed ? 'Removed from sidebar' : repo?.missing ? 'Folder missing' : (repo?.name || 'Folder removed')
  return (
    <button
      type="button"
      onClick={() => setOpen(true)}
      title={repo?.root_path || 'This chat was opened in a folder that is no longer in the sidebar'}
      className={desktop
        ? 'h-8 max-w-[180px] px-2.5 rounded-lg text-xs font-medium flex items-center gap-1.5 text-muted hover:text-ink hover:bg-overlay/60 transition-colors'
        : 'h-9 max-w-[148px] px-2.5 rounded-xl text-[11px] font-semibold flex items-center gap-1.5 text-muted border border-transparent hover:bg-overlay/60 transition-colors'}
    >
      <FolderOpen sx={{ fontSize: desktop ? 14 : 15 }} />
      <span className="truncate">{label}</span>
    </button>
  )
}

export function AddFromRepo({ sessionId, repoId, onAdded }: { sessionId: string; repoId: string; onAdded: () => void }) {
  const [open, setOpen] = useState(false)
  const [tree, setTree] = useState<any>(null)
  const [busy, setBusy] = useState(false)

  const load = async () => {
    setOpen(true)
    try {
      setTree(await api.repositories.tree(repoId))
    } catch (e: any) {
      toast.error('Could not read the folder', e.message)
    }
  }

  const stage = async (path: string) => {
    setBusy(true)
    try {
      const res = await api.repositories.stage(repoId, sessionId, path)
      toast.success(res.kept_edits ? 'Kept your edits' : 'Added to this chat', res.filename)
      onAdded()
    } catch (e: any) {
      toast.error('Could not add file', e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={load}
        className="flex items-center gap-1 px-2 py-1 rounded-lg bg-overlay text-muted text-[11px] font-semibold hover:bg-accent/10 hover:text-accent transition-colors"
        title="Add a file from the open folder"
      >
        <FolderOpen sx={{ fontSize: 12 }} /> Add from repo
      </button>
      {open && (
        <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4" onClick={() => setOpen(false)}>
          <div role="dialog" aria-modal="true" aria-labelledby="add-repo-file-title" className="flex max-h-[70vh] w-full max-w-md flex-col rounded-xl border border-border bg-surface shadow-modal" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
              <h2 id="add-repo-file-title" className="text-sm font-semibold text-ink">Add from repository</h2>
              <button
                type="button"
                className="text-[11px] font-semibold text-accent disabled:opacity-50"
                disabled={busy}
                onClick={async () => {
                  setBusy(true)
                  try {
                    const result = await api.sessionFiles.importFolder(sessionId)
                    toast.success(`Imported ${result.imported_count} file${result.imported_count === 1 ? '' : 's'}`)
                    onAdded()
                    setOpen(false)
                  } catch (e: any) {
                    toast.error('Import failed', e.message)
                  } finally {
                    setBusy(false)
                  }
                }}
              >
                Import all
              </button>
              <button type="button" className="btn-icon" aria-label="Close" onClick={() => setOpen(false)}><Close sx={{ fontSize: 16 }} /></button>
            </div>
            <div className="flex-1 overflow-y-auto px-2 py-2">
              {tree ? <RepoTree node={tree} depth={0} disabled={busy} onPick={stage} /> : <p className="px-2 py-3 text-[12px] text-muted">Loading…</p>}
            </div>
          </div>
        </div>
      )}
    </>
  )
}

function RepoTree({ node, depth, disabled, onPick }: { node: any; depth: number; disabled: boolean; onPick: (path: string) => void }) {
  const [expanded, setExpanded] = useState(depth < 1)
  if (!node) return null
  if (node.type === 'dir') {
    return (
      <div>
        <button type="button" className="flex w-full items-center gap-1 rounded px-2 py-1 text-left text-[12px] text-ink hover:bg-overlay" style={{ paddingLeft: 8 + depth * 12 }} onClick={() => setExpanded(v => !v)}>
          <span className="text-faint">{expanded ? '▾' : '▸'}</span> {node.name}
        </button>
        {expanded && (node.children || []).map((child: any) => (
          <RepoTree key={child.path} node={child} depth={depth + 1} disabled={disabled} onPick={onPick} />
        ))}
      </div>
    )
  }
  return (
    <button type="button" disabled={disabled} className="block w-full truncate rounded px-2 py-1 text-left text-[12px] text-muted hover:bg-overlay hover:text-ink disabled:opacity-50" style={{ paddingLeft: 8 + depth * 12 }} onClick={() => onPick(node.path)}>
      {node.name}
    </button>
  )
}

export function RepositoryManager() {
  const repositories = useAppStore(s => s.repositories)
  const setRepositories = useAppStore(s => s.setRepositories)
  const setOpen = useAppStore(s => s.setOpenFolderOpen)
  const [link, setLink] = useState<Repository | null>(null)
  const [owner, setOwner] = useState('')
  const [repo, setRepo] = useState('')

  const refresh = () => api.repositories.list().then(setRepositories).catch(() => {})
  useEffect(() => { refresh() }, [])

  const saveLink = async () => {
    if (!link) return
    try {
      await api.repositories.update(link.id, { github_owner: owner.trim(), github_repo: repo.trim() })
      setLink(null)
      await refresh()
    } catch (e: any) {
      toast.error('Could not link GitHub', e.message)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[12px] text-muted">Folders on this computer. Chats inside a folder can read it, and you apply edits to disk yourself.</p>
        <button type="button" className="btn-primary shrink-0 px-3 py-1.5 text-[12px]" onClick={() => setOpen(true)}>Open Folder</button>
      </div>
      <button type="button" className="flex items-center gap-1 text-[11px] text-muted hover:text-ink" onClick={refresh}>
        <Refresh sx={{ fontSize: 12 }} /> Refresh
      </button>
      {repositories.filter(item => !item.removed).length === 0 && <p className="text-[12px] text-faint">No folders open yet.</p>}
      <div className="space-y-2">
        {repositories.filter(item => !item.removed).map((item) => (
          <div key={item.id} className="rounded-lg border border-border px-3 py-2">
            <div className="flex items-center gap-2">
              <FolderOpen sx={{ fontSize: 15 }} className="text-muted" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-medium text-ink">{item.name}</div>
                <div className="truncate font-mono text-[10px] text-faint">{item.root_path}</div>
              </div>
              {item.missing && <span className="text-[10px] text-danger">Missing</span>}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
              {item.is_git && <span className="text-muted">{item.default_branch || 'git'}</span>}
              {item.github_linked && <span className="text-muted">{item.github_owner}/{item.github_repo}</span>}
              <label className="ml-auto flex items-center gap-1 text-muted">
                <input
                  type="checkbox"
                  checked={item.auto_apply}
                  onChange={async (e) => {
                    await api.repositories.update(item.id, { auto_apply: e.target.checked }).catch((err: any) => toast.error('Could not update', err.message))
                    refresh()
                  }}
                />
                Auto-apply
              </label>
              <button type="button" className="text-accent" onClick={() => { setLink(item); setOwner(item.suggested_github?.owner || item.github_owner || ''); setRepo(item.suggested_github?.repo || item.github_repo || '') }}>
                {item.github_linked ? 'Change GitHub' : 'Link GitHub'}
              </button>
              {item.github_linked && (
                <button type="button" className="text-muted" onClick={async () => { await api.repositories.update(item.id, { unlink_github: true }); refresh() }}>Unlink</button>
              )}
              <button type="button" className="text-danger" onClick={async () => { await api.repositories.remove(item.id); refresh() }}>Remove</button>
            </div>
          </div>
        ))}
      </div>
      {link && (
        <div className="rounded-lg border border-border p-3">
          <div className="text-[12px] font-medium text-ink">Link {link.name} to GitHub</div>
          <div className="mt-2 flex gap-2">
            <input className="input flex-1 text-[12px]" value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="owner" />
            <input className="input flex-1 text-[12px]" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="repo" />
          </div>
          <div className="mt-2 flex justify-end gap-2">
            <button type="button" className="btn-ghost px-2 text-[12px]" onClick={() => setLink(null)}>Cancel</button>
            <button type="button" className="btn-primary px-2 text-[12px]" onClick={saveLink}>Save</button>
          </div>
        </div>
      )}
    </div>
  )
}
