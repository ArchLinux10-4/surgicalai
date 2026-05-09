import React, { useState, useEffect, useCallback } from 'react'
import {
  Github, RefreshCw, ChevronRight, ChevronDown, GitBranch,
  FolderOpen, Folder, FileCode, Download, CheckCircle2, Loader2,
  ChevronLeft, Search, Lock, Star, Settings, AlertCircle,
} from 'lucide-react'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import { toast } from '../lib/toast'

interface GHRepo {
  id: number
  name: string
  full_name: string
  owner: string
  private: boolean
  description: string
  default_branch: string
  updated_at: string | null
  language: string
  stars: number
}

interface TreeItem {
  name: string
  path: string
  type: 'file' | 'dir'
  size: number
  sha: string
}

interface GitHubStatus {
  connected: boolean
  username?: string
  name?: string
  avatar_url?: string
  public_repos?: number
}

// Language color dots
const LANG_COLORS: Record<string, string> = {
  TypeScript: 'bg-blue-400', JavaScript: 'bg-yellow-400', Python: 'bg-green-400',
  Go: 'bg-cyan-400', Rust: 'bg-orange-400', Java: 'bg-red-400',
  'C++': 'bg-pink-400', CSS: 'bg-purple-400', HTML: 'bg-red-300',
  Vue: 'bg-green-500', Ruby: 'bg-red-500', PHP: 'bg-indigo-400',
  Swift: 'bg-orange-500', Kotlin: 'bg-purple-500',
}

const FILE_ICONS: Record<string, string> = {
  '.py': '🐍', '.js': '🟨', '.ts': '🔷', '.tsx': '⚛️', '.jsx': '⚛️',
  '.go': '🐹', '.rs': '🦀', '.java': '☕', '.html': '🌐', '.css': '🎨',
  '.json': '📋', '.md': '📝', '.sh': '⚡', '.sql': '🗄️', '.yml': '⚙️',
  '.yaml': '⚙️', '.toml': '⚙️', '.rb': '💎', '.php': '🐘',
}

function fileIcon(name: string) {
  const ext = '.' + name.split('.').pop()?.toLowerCase()
  return FILE_ICONS[ext] || '📄'
}

export function GitHubPanel({ onOpenSettings }: { onOpenSettings?: () => void }) {
  const { activeSessions, sessionFiles, setSessionFiles } = useAppStore()

  const [status, setStatus] = useState<GitHubStatus | null>(null)
  const [loadingStatus, setLoadingStatus] = useState(true)

  // Repo browser state
  const [repos, setRepos] = useState<GHRepo[]>([])
  const [loadingRepos, setLoadingRepos] = useState(false)
  const [repoSearch, setRepoSearch] = useState('')
  const [selectedRepo, setSelectedRepo] = useState<GHRepo | null>(null)

  // Branch state
  const [branches, setBranches] = useState<string[]>([])
  const [selectedBranch, setSelectedBranch] = useState('')
  const [loadingBranches, setLoadingBranches] = useState(false)

  // Tree state
  const [currentPath, setCurrentPath] = useState('')
  const [tree, setTree] = useState<TreeItem[]>([])
  const [loadingTree, setLoadingTree] = useState(false)
  const [pathHistory, setPathHistory] = useState<string[]>([])

  // File loading state
  const [loadingFiles, setLoadingFiles] = useState<Set<string>>(new Set())
  const [loadedPaths, setLoadedPaths] = useState<Set<string>>(new Set())

  // Check which files are already in this session
  useEffect(() => {
    const paths = new Set<string>()
    sessionFiles.forEach(f => {
      if (f.github_meta) {
        try {
          const meta = JSON.parse(f.github_meta)
          if (meta.path) paths.add(meta.path)
        } catch {}
      }
    })
    setLoadedPaths(paths)
  }, [sessionFiles])

  // Load GitHub status on mount
  useEffect(() => {
    setLoadingStatus(true)
    ;(api as any).github.status()
      .then((s: GitHubStatus) => setStatus(s))
      .catch(() => setStatus({ connected: false }))
      .finally(() => setLoadingStatus(false))
  }, [])

  // Load repos when connected
  useEffect(() => {
    if (!status?.connected) return
    setLoadingRepos(true)
    ;(api as any).github.repos()
      .then((d: { repos: GHRepo[] }) => setRepos(d.repos || []))
      .catch(() => {})
      .finally(() => setLoadingRepos(false))
  }, [status?.connected])

  const selectRepo = useCallback(async (repo: GHRepo) => {
    setSelectedRepo(repo)
    setSelectedBranch('')
    setBranches([])
    setTree([])
    setCurrentPath('')
    setPathHistory([])
    setLoadingBranches(true)
    try {
      const d: any = await (api as any).github.branches(repo.owner, repo.name)
      setBranches(d.branches || [])
      const def = d.default || repo.default_branch || 'main'
      setSelectedBranch(def)
    } catch (e: any) {
      toast.error('Could not load branches', e.message)
    } finally {
      setLoadingBranches(false)
    }
  }, [])

  const loadTree = useCallback(async (path: string, branch: string, repo: GHRepo) => {
    setLoadingTree(true)
    try {
      const d: any = await (api as any).github.tree(repo.owner, repo.name, branch, path)
      setTree(d.items || [])
      setCurrentPath(path)
    } catch (e: any) {
      toast.error('Could not load directory', e.message)
    } finally {
      setLoadingTree(false)
    }
  }, [])

  // Load tree when branch selected
  useEffect(() => {
    if (!selectedRepo || !selectedBranch) return
    loadTree('', selectedBranch, selectedRepo)
    setPathHistory([])
  }, [selectedBranch, selectedRepo])

  const navigateInto = (item: TreeItem) => {
    if (!selectedRepo || !selectedBranch) return
    setPathHistory(h => [...h, currentPath])
    loadTree(item.path, selectedBranch, selectedRepo)
  }

  const navigateBack = () => {
    if (!selectedRepo || !selectedBranch) return
    const prev = pathHistory[pathHistory.length - 1] ?? ''
    setPathHistory(h => h.slice(0, -1))
    loadTree(prev, selectedBranch, selectedRepo)
  }

  const loadFile = useCallback(async (item: TreeItem) => {
    if (!activeSessions || !selectedRepo || !selectedBranch) {
      toast.error('No active chat', 'Open or create a chat first')
      return
    }
    setLoadingFiles(f => new Set(f).add(item.path))
    try {
      const d: any = await (api as any).github.load({
        session_id: activeSessions,
        owner: selectedRepo.owner,
        repo: selectedRepo.name,
        branch: selectedBranch,
        paths: [item.path],
      })
      const loaded = d.loaded || []
      if (loaded.length > 0) {
        // Refresh session files from backend
        const updated: any[] = await (api as any).sessionFiles.list(activeSessions)
        setSessionFiles(updated)
        setLoadedPaths(p => new Set(p).add(item.path))
        toast.success(`${item.name} loaded into chat`)
      }
      if (d.errors?.length) {
        toast.error('Load error', d.errors[0].error)
      }
    } catch (e: any) {
      toast.error('Failed to load file', e.message)
    } finally {
      setLoadingFiles(f => { const s = new Set(f); s.delete(item.path); return s })
    }
  }, [activeSessions, selectedRepo, selectedBranch, setSessionFiles])

  const loadFolder = useCallback(async (folderPath: string) => {
    if (!activeSessions || !selectedRepo || !selectedBranch) {
      toast.error('No active chat', 'Open or create a chat first')
      return
    }
    // Get all files in the folder
    setLoadingTree(true)
    try {
      const d: any = await (api as any).github.tree(selectedRepo.owner, selectedRepo.name, selectedBranch, folderPath)
      const filePaths = (d.items || []).filter((i: TreeItem) => i.type === 'file').map((i: TreeItem) => i.path)
      if (filePaths.length === 0) { toast.error('No files in folder'); return }
      filePaths.forEach((p: string) => setLoadingFiles(f => new Set(f).add(p)))
      const result: any = await (api as any).github.load({
        session_id: activeSessions,
        owner: selectedRepo.owner,
        repo: selectedRepo.name,
        branch: selectedBranch,
        paths: filePaths,
      })
      const updated: any[] = await (api as any).sessionFiles.list(activeSessions)
      setSessionFiles(updated)
      result.loaded?.forEach((f: any) => {
        const meta = f.github_meta ? JSON.parse(f.github_meta) : null
        if (meta?.path) setLoadedPaths(p => new Set(p).add(meta.path))
      })
      toast.success(`${result.loaded?.length || 0} files loaded into chat`)
    } catch (e: any) {
      toast.error('Failed to load folder', e.message)
    } finally {
      setLoadingTree(false)
      setLoadingFiles(new Set())
    }
  }, [activeSessions, selectedRepo, selectedBranch, setSessionFiles])

  // ── Render ────────────────────────────────────────────────────────────────

  if (loadingStatus) {
    return (
      <div className="flex items-center justify-center h-20">
        <Loader2 size={16} className="animate-spin text-faint" />
      </div>
    )
  }

  // ── Not connected ─────────────────────────────────────────────────────────
  if (!status?.connected) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4 px-5 py-8 text-center">
        <div className="w-14 h-14 rounded-2xl bg-surface-alt border border-border flex items-center justify-center">
          <Github size={26} className="text-muted/70" />
        </div>
        <div>
          <p className="text-[14px] font-semibold text-ink mb-1">Connect GitHub</p>
          <p className="text-[12px] text-faint leading-relaxed">
            Browse your repos, load files directly into a chat, and push changes back with one click.
          </p>
        </div>
        <button
          onClick={onOpenSettings}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-accent text-white text-[13px] font-semibold hover:bg-accent/90 transition-colors"
        >
          <Settings size={13} />
          Add Token in Settings
        </button>
        <p className="text-[11px] text-faint">
          Uses a Personal Access Token — takes 30 seconds to set up
        </p>
      </div>
    )
  }

  // ── Connected ─────────────────────────────────────────────────────────────
  const filteredRepos = repos.filter(r =>
    r.full_name.toLowerCase().includes(repoSearch.toLowerCase()) ||
    r.description.toLowerCase().includes(repoSearch.toLowerCase())
  )

  // Breadcrumb
  const breadcrumbParts = currentPath ? currentPath.split('/') : []

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Connected user header */}
      <div className="flex items-center gap-2.5 px-3 py-2.5 border-b border-border bg-surface-alt/40">
        {status.avatar_url ? (
          <img src={status.avatar_url} alt="" className="w-6 h-6 rounded-full" />
        ) : (
          <div className="w-6 h-6 rounded-full bg-accent/20 flex items-center justify-center">
            <Github size={12} className="text-accent" />
          </div>
        )}
        <div className="flex-1 min-w-0">
          <p className="text-[12px] font-semibold text-ink truncate">@{status.username}</p>
          <p className="text-[10px] text-faint">{status.public_repos} public repos</p>
        </div>
        <span className="flex items-center gap-1 text-[10px] text-emerald-400 font-medium">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 inline-block" />
          Connected
        </span>
      </div>

      {/* Repo not selected: show repo list */}
      {!selectedRepo && (
        <div className="flex flex-col flex-1 overflow-hidden">
          <div className="px-2 pt-2 pb-1">
            <div className="relative">
              <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
              <input
                type="text"
                placeholder="Search repositories…"
                value={repoSearch}
                onChange={e => setRepoSearch(e.target.value)}
                className="w-full pl-7 pr-2 py-1.5 text-[12px] rounded-lg bg-surface border border-border text-ink placeholder:text-faint focus:outline-none focus:border-accent/60"
              />
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {loadingRepos ? (
              <div className="flex items-center justify-center h-20">
                <Loader2 size={14} className="animate-spin text-faint" />
              </div>
            ) : filteredRepos.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-24 text-faint">
                <p className="text-[12px]">No repositories found</p>
              </div>
            ) : (
              <div className="px-2 py-1 space-y-0.5">
                {filteredRepos.map(repo => (
                  <button
                    key={repo.id}
                    onClick={() => selectRepo(repo)}
                    className="w-full text-left px-2.5 py-2 rounded-lg hover:bg-overlay transition-colors group"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-[12px] font-semibold text-ink truncate flex-1">{repo.name}</span>
                      {repo.private && <Lock size={10} className="text-faint flex-shrink-0" />}
                      {repo.stars > 0 && (
                        <span className="flex items-center gap-0.5 text-[10px] text-faint flex-shrink-0">
                          <Star size={9} />
                          {repo.stars}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2 mt-0.5">
                      {repo.language && (
                        <span className="flex items-center gap-1 text-[10px] text-faint">
                          <span className={`w-2 h-2 rounded-full ${LANG_COLORS[repo.language] || 'bg-gray-400'}`} />
                          {repo.language}
                        </span>
                      )}
                      {repo.description && (
                        <span className="text-[10px] text-faint truncate">{repo.description}</span>
                      )}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Repo selected: show branch + tree */}
      {selectedRepo && (
        <div className="flex flex-col flex-1 overflow-hidden">
          {/* Repo header + back */}
          <div className="flex items-center gap-1.5 px-2 py-2 border-b border-border bg-surface-alt/20">
            <button
              onClick={() => { setSelectedRepo(null); setTree([]); setBranches([]) }}
              className="p-1 rounded hover:bg-overlay transition-colors"
              title="Back to repos"
            >
              <ChevronLeft size={13} className="text-muted" />
            </button>
            <span className="text-[12px] font-semibold text-ink truncate flex-1">{selectedRepo.name}</span>
            {/* Branch dropdown */}
            {branches.length > 0 && (
              <div className="relative">
                <div className="flex items-center gap-1 px-2 py-1 rounded-md bg-surface border border-border cursor-pointer hover:border-accent/50 transition-colors">
                  <GitBranch size={11} className="text-faint" />
                  <select
                    value={selectedBranch}
                    onChange={e => setSelectedBranch(e.target.value)}
                    className="text-[11px] text-ink bg-transparent outline-none cursor-pointer max-w-[80px]"
                  >
                    {branches.map(b => (
                      <option key={b} value={b}>{b}</option>
                    ))}
                  </select>
                </div>
              </div>
            )}
            {loadingBranches && <Loader2 size={11} className="animate-spin text-faint" />}
          </div>

          {/* Breadcrumb */}
          {currentPath && (
            <div className="flex items-center gap-1 px-2 py-1.5 border-b border-border/50 overflow-x-auto">
              <button
                onClick={() => { setCurrentPath(''); loadTree('', selectedBranch, selectedRepo); setPathHistory([]) }}
                className="text-[11px] text-accent hover:underline flex-shrink-0"
              >
                root
              </button>
              {breadcrumbParts.map((part, i) => (
                <React.Fragment key={i}>
                  <ChevronRight size={10} className="text-faint flex-shrink-0" />
                  <button
                    onClick={() => {
                      const path = breadcrumbParts.slice(0, i + 1).join('/')
                      setPathHistory(h => h.slice(0, h.indexOf(path) + 1))
                      loadTree(path, selectedBranch, selectedRepo)
                    }}
                    className="text-[11px] text-accent hover:underline flex-shrink-0"
                  >
                    {part}
                  </button>
                </React.Fragment>
              ))}
            </div>
          )}

          {/* Back button in tree */}
          {pathHistory.length > 0 && (
            <button
              onClick={navigateBack}
              className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] text-muted hover:text-ink hover:bg-overlay transition-colors"
            >
              <ChevronLeft size={12} /> ..
            </button>
          )}

          {/* Tree content */}
          <div className="flex-1 overflow-y-auto">
            {loadingTree ? (
              <div className="flex items-center justify-center h-20">
                <Loader2 size={14} className="animate-spin text-faint" />
              </div>
            ) : (
              <div className="py-1">
                {tree.map(item => {
                  const isDir = item.type === 'dir'
                  const isLoading = loadingFiles.has(item.path)
                  const isLoaded = loadedPaths.has(item.path)

                  return (
                    <div
                      key={item.path}
                      className="group flex items-center gap-2 px-3 py-1.5 hover:bg-overlay transition-colors"
                    >
                      {/* Icon */}
                      <span className="text-[13px] flex-shrink-0">
                        {isDir ? '📁' : fileIcon(item.name)}
                      </span>

                      {/* Name */}
                      <span
                        className={`flex-1 text-[12px] truncate min-w-0 ${isDir ? 'text-accent font-medium cursor-pointer' : 'text-ink'}`}
                        onClick={() => isDir && navigateInto(item)}
                      >
                        {item.name}
                      </span>

                      {/* Size badge for files */}
                      {!isDir && item.size > 0 && (
                        <span className="text-[10px] text-faint flex-shrink-0 opacity-0 group-hover:opacity-100">
                          {item.size > 1024 ? `${Math.round(item.size / 1024)}k` : `${item.size}b`}
                        </span>
                      )}

                      {/* Actions */}
                      {isDir ? (
                        <div className="flex items-center gap-1 flex-shrink-0 opacity-0 group-hover:opacity-100">
                          <button
                            onClick={() => navigateInto(item)}
                            className="p-1 rounded text-faint hover:text-ink transition-colors"
                            title="Open folder"
                          >
                            <ChevronRight size={11} />
                          </button>
                          <button
                            onClick={() => loadFolder(item.path)}
                            className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-accent/10 text-accent hover:bg-accent/20 transition-colors"
                            title="Load all files in folder"
                          >
                            <Download size={9} />
                            All
                          </button>
                        </div>
                      ) : (
                        <div className="flex-shrink-0">
                          {isLoaded ? (
                            <span className="flex items-center gap-1 text-[10px] text-emerald-400 font-medium">
                              <CheckCircle2 size={11} />
                              In chat
                            </span>
                          ) : isLoading ? (
                            <Loader2 size={12} className="animate-spin text-accent" />
                          ) : (
                            <button
                              onClick={() => loadFile(item)}
                              className="opacity-0 group-hover:opacity-100 flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold bg-accent/10 text-accent hover:bg-accent/20 border border-accent/20 transition-all"
                            >
                              <Download size={9} />
                              Load
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}

                {tree.length === 0 && !loadingTree && (
                  <div className="flex items-center justify-center h-16 text-faint">
                    <p className="text-[12px]">Empty directory</p>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* No session warning */}
          {!activeSessions && (
            <div className="px-3 py-2 border-t border-border bg-amber-500/5">
              <div className="flex items-center gap-1.5 text-[11px] text-amber-400">
                <AlertCircle size={11} />
                Open a chat to load files
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
