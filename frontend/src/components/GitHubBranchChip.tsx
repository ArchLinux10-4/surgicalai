import React, { useEffect, useMemo, useRef, useState } from 'react'
import { AccountTree, Check } from '@mui/icons-material'
import { api } from '../api/client'
import { fetchGithubBranches, noteGithubTargetWrite } from '../lib/githubBranch'
import { toast } from '../lib/toast'
import { useAppStore } from '../stores/appStore'
import type { GitHubConnections, GitHubTarget } from '../types'
import { useGithubTarget } from './useGithubTarget'

interface Props {
  sessionId: string | null
  disabled?: boolean
  variant: 'desktop' | 'mobile'
}

export function GitHubBranchChip({ sessionId, disabled = false, variant }: Props) {
  const state = useGithubTarget(sessionId, disabled)
  const setGithubTarget = useAppStore(s => s.setGithubTarget)
  const setSettingsOpen = useAppStore(s => s.setSettingsOpen)
  const setSettingsInitialTab = useAppStore(s => s.setSettingsInitialTab)
  const [open, setOpen] = useState(false)
  const connected = !!(state?.connections.pat || state?.connections.app)
  useEffect(() => {
    if (!connected) setOpen(false)
  }, [connected])

  if (!state?.target) return null
  if (!connected) {
    return (
      <DisconnectedChip
        target={state.target}
        variant={variant}
        onOpenSettings={() => {
          setSettingsInitialTab('github')
          setSettingsOpen(true)
        }}
      />
    )
  }

  return (
    <ConnectedChip
      sessionId={sessionId}
      target={state.target}
      connections={state.connections}
      disabled={disabled}
      variant={variant}
      open={open}
      setOpen={setOpen}
      onRevert={(prev) => sessionId && setGithubTarget(sessionId, prev)}
    />
  )
}

function DisconnectedChip({
  target, variant, onOpenSettings,
}: {
  target: GitHubTarget
  variant: 'desktop' | 'mobile'
  onOpenSettings: () => void
}) {
  const desktop = variant === 'desktop'
  return (
    <button
      type="button"
      onClick={onOpenSettings}
      title={`${target.owner}/${target.repo}@${target.branch}`}
      className={desktop
        ? 'h-8 max-w-[168px] px-2.5 rounded-lg text-xs font-medium flex items-center gap-1.5 text-muted/50 hover:text-ink/70 hover:bg-overlay/60 transition-colors'
        : 'h-9 max-w-[148px] px-2.5 rounded-xl text-[11px] font-semibold flex items-center gap-1.5 text-muted/50 border border-transparent hover:bg-overlay/60 transition-colors'}
    >
      <AccountTree sx={{ fontSize: desktop ? 14 : 15 }} />
      <span className="truncate">GitHub disconnected</span>
    </button>
  )
}

function ConnectedChip({
  sessionId, target, connections, disabled, variant, open, setOpen, onRevert,
}: {
  sessionId: string | null
  target: GitHubTarget
  connections: GitHubConnections
  disabled: boolean
  variant: 'desktop' | 'mobile'
  open: boolean
  setOpen: (v: boolean) => void
  onRevert: (prev: { target: GitHubTarget | null; connections: GitHubConnections } | undefined) => void
}) {
  const desktop = variant === 'desktop'
  const [query, setQuery] = useState('')
  const [branches, setBranches] = useState<string[]>([])
  const [defaultBranch, setDefaultBranch] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [hi, setHi] = useState(0)
  const [attempt, setAttempt] = useState(0)
  const searchRef = useRef<HTMLInputElement>(null)
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([])
  const showAppHint = connections.pat && !connections.app && connections.app_configured

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return branches
    return branches.filter(name => name.toLowerCase().includes(q))
  }, [branches, query])

  useEffect(() => {
    if (disabled) setOpen(false)
  }, [disabled, setOpen])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError('')
    setQuery('')
    setHi(0)
    fetchGithubBranches(target.owner, target.repo, connections)
      .then(data => {
        if (cancelled) return
        setBranches(data.branches || [])
        setDefaultBranch(data.default || '')
      })
      .catch((err: any) => {
        if (cancelled) return
        setBranches([])
        setError(err?.message || 'Could not load branches')
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    const focus = window.setTimeout(() => searchRef.current?.focus(), 30)
    return () => { cancelled = true; window.clearTimeout(focus) }
  }, [open, attempt, target.owner, target.repo, connections.pat, connections.app])

  useEffect(() => {
    if (!open) return
    itemRefs.current[hi]?.scrollIntoView({ block: 'nearest' })
  }, [hi, open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, setOpen])

  const choose = async (branch: string) => {
    if (!sessionId) return
    setOpen(false)
    if (branch === target.branch) return
    noteGithubTargetWrite()
    const prev = useAppStore.getState().githubTargets[sessionId]
    useAppStore.getState().setGithubTarget(sessionId, {
      target: { ...target, branch, explicit: true },
      connections,
    })
    try {
      await api.chat.setGithubTarget(sessionId, { owner: target.owner, repo: target.repo, branch })
    } catch (err: any) {
      onRevert(prev)
      toast.error('Could not switch branch', err?.message || 'Try again')
    }
  }

  const onSearchKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHi(i => Math.min(shown.length - 1, i + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHi(i => Math.max(0, i - 1))
    } else if (e.key === 'Enter' && shown[hi]) {
      e.preventDefault()
      void choose(shown[hi])
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
    }
  }

  const buttonClass = desktop
    ? 'h-8 max-w-[168px] px-2.5 rounded-lg text-xs font-medium flex items-center gap-1.5 transition-colors disabled:opacity-40 text-muted/80 hover:text-ink hover:bg-overlay/60'
    : 'h-9 max-w-[148px] px-2.5 rounded-xl text-[11px] font-semibold flex items-center gap-1.5 border border-transparent transition-colors disabled:opacity-40 text-muted/80 hover:text-ink hover:bg-overlay/60'

  const list = (
    <BranchList
      query={query}
      setQuery={setQuery}
      searchRef={searchRef}
      onSearchKey={onSearchKey}
      loading={loading}
      error={error}
      onRetry={() => setAttempt(n => n + 1)}
      shown={shown}
      hi={hi}
      setHi={setHi}
      itemRefs={itemRefs}
      current={target.branch}
      defaultBranch={defaultBranch}
      onChoose={choose}
      showAppHint={showAppHint}
      onOpenSettings={() => {
        setOpen(false)
        useAppStore.getState().setSettingsInitialTab('github')
        useAppStore.getState().setSettingsOpen(true)
      }}
      roomy={!desktop}
    />
  )

  return (
    <div className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(!open)}
        title={`${target.owner}/${target.repo}@${target.branch}`}
        className={buttonClass}
      >
        <AccountTree sx={{ fontSize: desktop ? 14 : 15 }} className="shrink-0" />
        <span className="truncate">{target.repo}</span>
        <span className="text-muted/40 shrink-0">·</span>
        <span className="truncate">{target.branch}</span>
      </button>
      {open && desktop && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full left-0 z-50 mb-1 w-64 rounded-xl border border-border bg-surface shadow-lg shadow-black/20 overflow-hidden">
            {list}
          </div>
        </>
      )}
      {open && !desktop && (
        <>
          <div className="fixed inset-0 z-40 bg-black/40" onClick={() => setOpen(false)} />
          <div
            className="fixed inset-x-0 bottom-0 z-50 rounded-t-2xl border-t border-border bg-surface shadow-2xl"
            style={{ paddingBottom: 'max(env(safe-area-inset-bottom, 0px), 12px)' }}
          >
            <div className="flex justify-center pt-2.5 pb-1">
              <div className="w-10 h-1 rounded-full bg-border" />
            </div>
            <div className="px-4 pb-2 text-[13px] font-medium text-ink truncate">
              {target.owner}/{target.repo}
            </div>
            {list}
          </div>
        </>
      )}
    </div>
  )
}

function BranchList({
  query, setQuery, searchRef, onSearchKey, loading, error, onRetry, shown, hi, setHi,
  itemRefs, current, defaultBranch, onChoose, showAppHint, onOpenSettings, roomy,
}: {
  query: string
  setQuery: (v: string) => void
  searchRef: React.RefObject<HTMLInputElement>
  onSearchKey: (e: React.KeyboardEvent) => void
  loading: boolean
  error: string
  onRetry: () => void
  shown: string[]
  hi: number
  setHi: (n: number) => void
  itemRefs: React.MutableRefObject<(HTMLButtonElement | null)[]>
  current: string
  defaultBranch: string
  onChoose: (branch: string) => void
  showAppHint: boolean
  onOpenSettings: () => void
  roomy: boolean
}) {
  return (
    <div>
      <div className="p-2 border-b border-border/60">
        <input
          ref={searchRef}
          value={query}
          onChange={e => { setQuery(e.target.value); setHi(0) }}
          onKeyDown={onSearchKey}
          placeholder="Find a branch"
          className="w-full h-8 px-2.5 rounded-lg bg-base/70 border border-border/70 text-xs text-ink placeholder:text-muted/50 outline-none focus:border-accent/50"
        />
      </div>
      <div
        role="listbox"
        aria-label="Branches"
        className={`overflow-y-auto ${roomy ? 'max-h-72' : 'max-h-56'}`}
      >
        {loading && (
          <div className="p-2 space-y-1.5">
            {[0, 1, 2, 3].map(i => (
              <div key={i} className="h-7 rounded-md bg-overlay/70 animate-pulse" />
            ))}
          </div>
        )}
        {!loading && error && (
          <div className="px-3 py-3 text-xs text-muted">
            <p>{error}</p>
            <button
              type="button"
              onClick={onRetry}
              className="mt-2 text-accent hover:underline"
            >
              Retry
            </button>
          </div>
        )}
        {!loading && !error && shown.length === 0 && (
          <p className="px-3 py-3 text-xs text-muted/70">No branches found</p>
        )}
        {!loading && !error && shown.map((name, i) => (
          <button
            key={name}
            type="button"
            role="option"
            aria-selected={name === current}
            ref={el => { itemRefs.current[i] = el }}
            onMouseEnter={() => setHi(i)}
            onClick={() => onChoose(name)}
            className={`w-full flex items-center gap-2 px-3 text-left text-xs ${roomy ? 'py-2.5' : 'py-1.5'} ${
              i === hi ? 'bg-overlay text-ink' : 'text-ink/80'
            }`}
          >
            <Check sx={{ fontSize: 14 }} className={name === current ? 'text-accent' : 'opacity-0'} />
            <span className="truncate flex-1">{name}</span>
            {name === defaultBranch && (
              <span className="text-[10px] text-muted/50 shrink-0">default</span>
            )}
          </button>
        ))}
      </div>
      {showAppHint && (
        <button
          type="button"
          onClick={onOpenSettings}
          className="w-full text-left px-3 py-2 border-t border-border/60 text-[11px] text-muted hover:text-ink hover:bg-overlay/50"
        >
          Connect the GitHub App so the agent can read this branch
        </button>
      )}
    </div>
  )
}
