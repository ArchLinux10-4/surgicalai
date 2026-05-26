/**
 * MobileGitHubSheet — GitHub file browser as a full-screen bottom sheet.
 *
 * Best-practice mobile pattern (iOS Files, GitHub mobile, Linear):
 * - Triggered by a GitHub icon in the mobile header
 * - Slides up from bottom, covers full screen
 * - Drag handle at top, X to close
 * - Same 3-stage flow as desktop GitHubPanel: repos → branch/tree → load file
 * - Same API calls as GitHubPanel — zero backend changes
 *
 * Only renders the sheet overlay — the trigger button lives in MobileLayout.
 */
import React, { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../../api/client'
import { useAppStore } from '../../stores/appStore'
import { toast } from '../../lib/toast'

interface GHRepo {
  id: number; name: string; full_name: string; owner: string
  private: boolean; description: string; default_branch: string
  updated_at: string | null; language: string; stars: number
}
interface TreeItem {
  name: string; path: string; type: 'file' | 'dir'; size: number; sha: string
}
interface GitHubStatus {
  connected: boolean; username?: string; avatar_url?: string; public_repos?: number
}

const LANG_COLORS: Record<string, string> = {
  TypeScript: '#3178c6', JavaScript: '#f7df1e', Python: '#3572a5',
  Go: '#00add8', Rust: '#ce422b', HTML: '#e34c26', CSS: '#563d7c',
  Vue: '#41b883', Ruby: '#cc342d', Swift: '#fa7343',
}
const FILE_EXTS: Record<string, string> = {
  '.ts':'🔷','.tsx':'⚛️','.js':'🟨','.jsx':'⚛️','.py':'🐍','.go':'🐹',
  '.rs':'🦀','.html':'🌐','.css':'🎨','.json':'📋','.md':'📝',
  '.sh':'⚡','.sql':'🗄️','.yml':'⚙️','.yaml':'⚙️',
}
const fileIcon = (n: string) => FILE_EXTS['.'+n.split('.').pop()?.toLowerCase()] || '📄'

// ── Spinner ────────────────────────────────────────────────────────────────
function Spin() {
  return (
    <svg className="animate-spin" width="16" height="16" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3"/>
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
    </svg>
  )
}

// ── Main sheet ─────────────────────────────────────────────────────────────
interface Props {
  open: boolean
  onClose: () => void
  onOpenSettings: () => void
}

export function MobileGitHubSheet({ open, onClose, onOpenSettings }: Props) {
  const { activeSessions, sessionFiles, setSessionFiles } = useAppStore()

  const [status, setStatus]       = useState<GitHubStatus | null>(null)
  const [loadingStatus, setLdSt]  = useState(true)
  const [repos, setRepos]         = useState<GHRepo[]>([])
  const [loadingRepos, setLdRp]   = useState(false)
  const [repoSearch, setSearch]   = useState('')
  const [selectedRepo, setRepo]   = useState<GHRepo | null>(null)
  const [branches, setBranches]   = useState<string[]>([])
  const [branch, setBranch]       = useState('')
  const [loadingBranches, setLdBr]= useState(false)
  const [currentPath, setPath]    = useState('')
  const [tree, setTree]           = useState<TreeItem[]>([])
  const [loadingTree, setLdTr]    = useState(false)
  const [pathHistory, setPathHist]= useState<string[]>([])
  const [loadingFiles, setLdF]    = useState<Set<string>>(new Set())
  const [loadedPaths, setLdPaths] = useState<Set<string>>(new Set())

  // Track which files already in session
  useEffect(() => {
    const paths = new Set<string>()
    sessionFiles.forEach(f => {
      if (f.github_meta) {
        try { const m = JSON.parse(f.github_meta); if (m.path) paths.add(m.path) } catch {}
      }
    })
    setLdPaths(paths)
  }, [sessionFiles])

  // Load status on open
  useEffect(() => {
    if (!open) return
    setLdSt(true)
    ;(api as any).github.status()
      .then((s: GitHubStatus) => { setStatus(s) })
      .catch(() => setStatus({ connected: false }))
      .finally(() => setLdSt(false))
  }, [open])

  // Load repos when connected
  useEffect(() => {
    if (!status?.connected) return
    setLdRp(true)
    ;(api as any).github.repos()
      .then((d: any) => setRepos(d.repos || []))
      .catch(() => {})
      .finally(() => setLdRp(false))
  }, [status?.connected])

  const selectRepo = useCallback(async (repo: GHRepo) => {
    setRepo(repo); setBranch(''); setBranches([]); setTree([]); setPath(''); setPathHist([])
    setLdBr(true)
    try {
      const d: any = await (api as any).github.branches(repo.owner, repo.name)
      setBranches(d.branches || [])
      setBranch(d.default || repo.default_branch || 'main')
    } catch (e: any) { toast.error('Could not load branches') }
    finally { setLdBr(false) }
  }, [])

  const loadTree = useCallback(async (path: string, br: string, repo: GHRepo) => {
    setLdTr(true)
    try {
      const d: any = await (api as any).github.tree(repo.owner, repo.name, br, path)
      setTree(d.items || []); setPath(path)
    } catch { toast.error('Could not load directory') }
    finally { setLdTr(false) }
  }, [])

  useEffect(() => {
    if (!selectedRepo || !branch) return
    loadTree('', branch, selectedRepo); setPathHist([])
  }, [branch, selectedRepo])

  const navigateInto = (item: TreeItem) => {
    if (!selectedRepo || !branch) return
    setPathHist(h => [...h, currentPath])
    loadTree(item.path, branch, selectedRepo)
  }
  const navigateBack = () => {
    if (!selectedRepo || !branch) return
    const prev = pathHistory[pathHistory.length - 1] ?? ''
    setPathHist(h => h.slice(0, -1))
    loadTree(prev, branch, selectedRepo)
  }

  const loadFile = useCallback(async (item: TreeItem) => {
    if (!activeSessions || !selectedRepo || !branch) {
      toast.error('Open a chat session first'); return
    }
    setLdF(f => new Set(f).add(item.path))
    try {
      const d: any = await (api as any).github.load({
        session_id: activeSessions,
        owner: selectedRepo.owner,
        repo: selectedRepo.name,
        branch,
        paths: [item.path],
      })
      if (d.loaded?.length > 0) {
        const updated: any[] = await (api as any).sessionFiles.list(activeSessions)
        setSessionFiles(updated)
        setLdPaths(p => new Set(p).add(item.path))
        toast.success(`${item.name} loaded into chat`)
      }
      if (d.errors?.length) toast.error(d.errors[0].error)
    } catch (e: any) { toast.error('Failed to load file') }
    finally { setLdF(f => { const s = new Set(f); s.delete(item.path); return s }) }
  }, [activeSessions, selectedRepo, branch, setSessionFiles])

  const breadcrumbs = currentPath ? currentPath.split('/') : []

  if (!open) return null

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 z-40 bg-black/50" onClick={onClose} />

      {/* Sheet */}
      <div className="fixed inset-x-0 bottom-0 top-12 z-50 flex flex-col bg-surface rounded-t-2xl shadow-2xl overflow-hidden">

        {/* Drag handle */}
        <div className="flex-shrink-0 flex justify-center pt-3 pb-1">
          <div className="w-10 h-1 rounded-full bg-border" />
        </div>

        {/* Header */}
        <div className="flex-shrink-0 flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="flex items-center gap-2.5">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" className="text-ink/70">
              <path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.54-1.38-1.33-1.75-1.33-1.75-1.09-.74.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.17 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 3-.4c1.02.005 2.04.138 3 .4 2.28-1.55 3.29-1.23 3.29-1.23.66 1.65.24 2.87.12 3.17.77.84 1.23 1.91 1.23 3.22 0 4.61-2.81 5.63-5.48 5.92.43.37.82 1.1.82 2.22v3.29c0 .32.21.7.83.58C20.57 21.8 24 17.3 24 12c0-6.63-5.37-12-12-12z"/>
            </svg>
            <span className="text-sm font-semibold text-ink">GitHub</span>
            {status?.connected && status.username && (
              <span className="text-[11px] text-muted/60">@{status.username}</span>
            )}
          </div>
          <button onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-lg text-muted/60 hover:text-ink hover:bg-overlay transition-colors">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-hidden flex flex-col">

          {/* Loading status */}
          {loadingStatus && (
            <div className="flex items-center justify-center flex-1 gap-2 text-muted/60 text-sm">
              <Spin /> Connecting...
            </div>
          )}

          {/* Not connected */}
          {!loadingStatus && !status?.connected && (
            <div className="flex flex-col items-center justify-center flex-1 gap-5 px-8 text-center">
              <div className="w-16 h-16 rounded-2xl bg-surface border border-border flex items-center justify-center">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="currentColor" className="text-muted/50">
                  <path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.54-1.38-1.33-1.75-1.33-1.75-1.09-.74.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.17 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 3-.4c1.02.005 2.04.138 3 .4 2.28-1.55 3.29-1.23 3.29-1.23.66 1.65.24 2.87.12 3.17.77.84 1.23 1.91 1.23 3.22 0 4.61-2.81 5.63-5.48 5.92.43.37.82 1.1.82 2.22v3.29c0 .32.21.7.83.58C20.57 21.8 24 17.3 24 12c0-6.63-5.37-12-12-12z"/>
                </svg>
              </div>
              <div>
                <p className="text-base font-semibold text-ink mb-1">Connect GitHub</p>
                <p className="text-sm text-muted/60 leading-relaxed">
                  Browse repos, load files into chat, and push changes back with one tap.
                </p>
              </div>
              <button
                onClick={() => { onClose(); setTimeout(onOpenSettings, 200) }}
                className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-[rgba(74,222,128,0.12)] border border-[rgba(74,222,128,0.25)] text-[#4ade80] text-sm font-semibold hover:bg-[rgba(74,222,128,0.2)] active:scale-95 transition-all"
              >
                Add Token in Settings →
              </button>
              <p className="text-xs text-muted/40">Uses a Personal Access Token · 30 seconds to set up</p>
            </div>
          )}

          {/* Connected — repo list */}
          {!loadingStatus && status?.connected && !selectedRepo && (
            <div className="flex flex-col flex-1 overflow-hidden">
              {/* Search */}
              <div className="flex-shrink-0 px-4 py-2.5 border-b border-border/50">
                <div className="flex items-center gap-2 bg-overlay/60 border border-border rounded-xl px-3 py-2">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-muted/50 flex-shrink-0">
                    <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
                  </svg>
                  <input
                    value={repoSearch}
                    onChange={e => setSearch(e.target.value)}
                    placeholder="Search repositories..."
                    className="flex-1 bg-transparent text-sm text-ink placeholder:text-muted/40 focus:outline-none"
                  />
                </div>
              </div>

              <div className="flex-1 overflow-y-auto">
                {loadingRepos ? (
                  <div className="flex justify-center items-center h-24 gap-2 text-muted/60 text-sm"><Spin /> Loading repos...</div>
                ) : (
                  <div className="py-2">
                    {repos
                      .filter(r => r.full_name.toLowerCase().includes(repoSearch.toLowerCase()) || (r.description||'').toLowerCase().includes(repoSearch.toLowerCase()))
                      .map(repo => (
                        <button key={repo.id} onClick={() => selectRepo(repo)}
                          className="w-full flex items-start gap-3 px-4 py-3 border-b border-border/30 hover:bg-overlay/60 active:bg-overlay transition-colors text-left">
                          <div className="w-8 h-8 rounded-lg bg-surface border border-border flex items-center justify-center flex-shrink-0 mt-0.5">
                            {repo.private ? (
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-muted/60"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                            ) : (
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" className="text-muted/60"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.54-1.38-1.33-1.75-1.33-1.75-1.09-.74.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.17 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 3-.4c1.02.005 2.04.138 3 .4 2.28-1.55 3.29-1.23 3.29-1.23.66 1.65.24 2.87.12 3.17.77.84 1.23 1.91 1.23 3.22 0 4.61-2.81 5.63-5.48 5.92.43.37.82 1.1.82 2.22v3.29c0 .32.21.7.83.58C20.57 21.8 24 17.3 24 12c0-6.63-5.37-12-12-12z"/></svg>
                            )}
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-sm font-semibold text-ink">{repo.name}</span>
                              {repo.private && <span className="text-[9px] px-1.5 py-0.5 rounded bg-surface border border-border text-muted/60">Private</span>}
                              {repo.stars > 0 && <span className="text-[10px] text-muted/50">★ {repo.stars}</span>}
                            </div>
                            {repo.description && <p className="text-xs text-muted/60 mt-0.5 truncate">{repo.description}</p>}
                            {repo.language && (
                              <div className="flex items-center gap-1.5 mt-1">
                                <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: LANG_COLORS[repo.language] || '#94a3b8' }} />
                                <span className="text-[10px] text-muted/60">{repo.language}</span>
                              </div>
                            )}
                          </div>
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-muted/40 flex-shrink-0 mt-2">
                            <polyline points="9 18 15 12 9 6"/>
                          </svg>
                        </button>
                      ))
                    }
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Repo selected — branch + file tree */}
          {!loadingStatus && status?.connected && selectedRepo && (
            <div className="flex flex-col flex-1 overflow-hidden">

              {/* Repo nav bar */}
              <div className="flex-shrink-0 flex items-center gap-2 px-4 py-2.5 border-b border-border bg-surface/80">
                <button onClick={() => { setRepo(null); setTree([]); setBranches([]) }}
                  className="w-8 h-8 flex items-center justify-center rounded-lg text-muted/60 hover:text-ink hover:bg-overlay transition-colors">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><polyline points="15 18 9 12 15 6"/></svg>
                </button>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-semibold text-ink truncate">{selectedRepo.name}</p>
                  {currentPath && <p className="text-[10px] text-muted/60 truncate">{currentPath}</p>}
                </div>
                {/* Branch selector */}
                {branches.length > 0 && (
                  <div className="flex items-center gap-1 px-2 py-1 bg-overlay/60 border border-border rounded-lg">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-muted/60">
                      <line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/>
                      <path d="M18 9a9 9 0 0 1-9 9"/>
                    </svg>
                    <select value={branch} onChange={e => setBranch(e.target.value)}
                      className="text-[11px] text-ink bg-transparent outline-none max-w-[90px]">
                      {branches.map(b => <option key={b} value={b}>{b}</option>)}
                    </select>
                  </div>
                )}
                {loadingBranches && <Spin />}
              </div>

              {/* Breadcrumb */}
              {currentPath && (
                <div className="flex-shrink-0 flex items-center gap-1 px-4 py-2 border-b border-border/50 overflow-x-auto bg-surface/40">
                  <button onClick={() => { loadTree('', branch, selectedRepo); setPathHist([]) }}
                    className="text-[11px] text-[#4ade80] flex-shrink-0">root</button>
                  {breadcrumbs.map((part, i) => (
                    <React.Fragment key={i}>
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-muted/40 flex-shrink-0">
                        <polyline points="9 18 15 12 9 6"/>
                      </svg>
                      <button
                        onClick={() => { const p = breadcrumbs.slice(0,i+1).join('/'); loadTree(p, branch, selectedRepo) }}
                        className="text-[11px] text-[#4ade80] flex-shrink-0">{part}</button>
                    </React.Fragment>
                  ))}
                </div>
              )}

              {/* Back button */}
              {pathHistory.length > 0 && (
                <button onClick={navigateBack}
                  className="flex-shrink-0 flex items-center gap-2 px-4 py-2 text-sm text-muted/70 hover:text-ink hover:bg-overlay/60 transition-colors border-b border-border/30">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><polyline points="15 18 9 12 15 6"/></svg>
                  ..
                </button>
              )}

              {/* Tree */}
              <div className="flex-1 overflow-y-auto">
                {loadingTree ? (
                  <div className="flex justify-center items-center h-24 gap-2 text-muted/60 text-sm"><Spin /> Loading...</div>
                ) : tree.length === 0 ? (
                  <div className="flex items-center justify-center h-24 text-muted/50 text-sm">Empty directory</div>
                ) : (
                  <div className="py-1">
                    {tree.map(item => {
                      const isDir     = item.type === 'dir'
                      const isLoading = loadingFiles.has(item.path)
                      const isLoaded  = loadedPaths.has(item.path)
                      return (
                        <div key={item.path}
                          className="flex items-center gap-3 px-4 py-2.5 border-b border-border/20 active:bg-overlay/60 transition-colors">
                          <span className="text-base flex-shrink-0 w-6 text-center">
                            {isDir ? '📁' : fileIcon(item.name)}
                          </span>
                          <span
                            className={`flex-1 text-sm min-w-0 truncate ${isDir ? 'text-[#4ade80] font-medium' : 'text-ink'}`}
                            onClick={() => isDir && navigateInto(item)}
                          >
                            {item.name}
                          </span>
                          {/* File size */}
                          {!isDir && item.size > 0 && (
                            <span className="text-[10px] text-muted/40 flex-shrink-0">
                              {item.size > 1024 ? `${Math.round(item.size/1024)}k` : `${item.size}b`}
                            </span>
                          )}
                          {/* Actions */}
                          <div className="flex-shrink-0">
                            {isDir ? (
                              <button onClick={() => navigateInto(item)}
                                className="w-8 h-8 flex items-center justify-center rounded-lg text-muted/50 hover:text-ink hover:bg-overlay transition-colors">
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="9 18 15 12 9 6"/></svg>
                              </button>
                            ) : isLoaded ? (
                              <span className="flex items-center gap-1 text-[10px] text-emerald-400 font-medium px-2 py-1 bg-emerald-500/10 rounded-lg border border-emerald-500/20">
                                ✓ In chat
                              </span>
                            ) : isLoading ? (
                              <div className="w-8 h-8 flex items-center justify-center text-[#4ade80]"><Spin /></div>
                            ) : (
                              <button onClick={() => loadFile(item)}
                                className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-[11px] font-semibold
                                  bg-[rgba(74,222,128,0.1)] border border-[rgba(74,222,128,0.25)] text-[#4ade80]
                                  hover:bg-[rgba(74,222,128,0.18)] active:scale-95 transition-all">
                                Load
                              </button>
                            )}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>

              {/* No session warning */}
              {!activeSessions && (
                <div className="flex-shrink-0 flex items-center gap-2 px-4 py-3 border-t border-border bg-amber-500/5">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-amber-400 flex-shrink-0"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                  <span className="text-[11px] text-amber-400">Open or create a chat to load files</span>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  )
}
