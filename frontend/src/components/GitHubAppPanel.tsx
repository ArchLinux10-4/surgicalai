import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { GitHub, CheckCircle, Lock, Edit, LockOpen, DeleteOutline } from '@mui/icons-material'

interface Installation {
  installation_id: string
  account_login: string
  permission_tier: 'read_only' | 'read_comment' | 'read_write'
  connected_at: string
}

const TIER_LABELS: Record<string, string> = {
  read_only: 'Read only',
  read_comment: 'Read + comment',
  read_write: 'Read + write (commits & PRs)',
}

const TIER_DESCRIPTIONS: Record<string, string> = {
  read_only: 'Claude can read your repos, PRs, and issues — no changes of any kind.',
  read_comment: 'Adds the ability to post PR/issue comments. No file commits.',
  read_write: 'Full access — Claude can commit files and open pull requests, same as the legacy PAT flow.',
}

/**
 * GitHubAppPanel — new, opt-in "recommended" GitHub connection method.
 * Lives alongside the existing GitHubPanel (legacy PAT flow), which is
 * completely untouched and remains available for anyone who prefers it.
 *
 * Connecting requires zero typing: click Connect, pick repos on GitHub's
 * own install screen, done. No token, key, or ID is ever entered here —
 * those live only as server-side environment variables.
 */
export function GitHubAppPanel() {
  const [enabled, setEnabled] = useState(false)
  const [loadingConfig, setLoadingConfig] = useState(true)
  const [installations, setInstallations] = useState<Installation[]>([])
  const [loadingStatus, setLoadingStatus] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [savingTierFor, setSavingTierFor] = useState<string | null>(null)

  const refreshStatus = useCallback(() => {
    setLoadingStatus(true)
    ;(api as any).githubApp.status()
      .then((d: { installations: Installation[] }) => setInstallations(d.installations || []))
      .catch(() => setInstallations([]))
      .finally(() => setLoadingStatus(false))
  }, [])

  useEffect(() => {
    setLoadingConfig(true)
    ;(api as any).githubApp.config()
      .then((d: { enabled: boolean }) => setEnabled(!!d.enabled))
      .catch(() => setEnabled(false))
      .finally(() => setLoadingConfig(false))
  }, [])

  useEffect(() => {
    if (!enabled) return
    refreshStatus()
  }, [enabled, refreshStatus])

  // Pick up ?github_app=connected|error from the post-install redirect.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const result = params.get('github_app')
    if (!result) return
    if (result === 'connected') {
      toast.success('GitHub connected!')
      refreshStatus()
    } else if (result === 'error') {
      toast.error(`GitHub connection failed (${params.get('reason') || 'unknown reason'})`)
    }
    params.delete('github_app')
    params.delete('reason')
    const clean = window.location.pathname + (params.toString() ? `?${params}` : '')
    window.history.replaceState({}, '', clean)
  }, [refreshStatus])

  const handleConnect = useCallback(async () => {
    setConnecting(true)
    try {
      const d: any = await (api as any).githubApp.installUrl()
      window.location.href = d.url
    } catch (e: any) {
      toast.error(e.message || 'Could not start GitHub connection')
      setConnecting(false)
    }
  }, [])

  const handleTierChange = useCallback(async (installationId: string, tier: string) => {
    setSavingTierFor(installationId)
    try {
      await (api as any).githubApp.setTier(installationId, tier)
      setInstallations(prev => prev.map(i =>
        i.installation_id === installationId ? { ...i, permission_tier: tier as any } : i
      ))
      toast.success('Permission level updated')
    } catch (e: any) {
      toast.error(e.message || 'Could not update permission level')
    } finally {
      setSavingTierFor(null)
    }
  }, [])

  const handleDisconnect = useCallback(async (installationId: string, accountLogin: string) => {
    if (!window.confirm(`Disconnect ${accountLogin}? You can also fully revoke access from your GitHub account settings.`)) return
    try {
      await (api as any).githubApp.disconnect(installationId)
      setInstallations(prev => prev.filter(i => i.installation_id !== installationId))
      toast.success('Disconnected')
    } catch (e: any) {
      toast.error(e.message || 'Could not disconnect')
    }
  }, [])

  if (loadingConfig) {
    return <div className="text-sm text-gray-400 p-4">Checking GitHub App availability…</div>
  }

  if (!enabled) {
    // Server hasn't configured the GitHub App yet — hide the option
    // entirely rather than showing a broken button. Legacy PAT flow below
    // (rendered by GitHubPanel) is unaffected either way.
    return null
  }

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800/50 p-4 space-y-4">
      <div className="flex items-center gap-2">
        <GitHub className="text-gray-300" fontSize="small" />
        <h3 className="text-sm font-semibold text-gray-200">GitHub App (recommended)</h3>
        <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-900/40 text-emerald-300 border border-emerald-700/50">
          More secure
        </span>
      </div>
      <p className="text-xs text-gray-400">
        No tokens or keys to copy or paste. Click Connect, choose which repos to share on
        GitHub's own screen, and you're done — GitHub handles the login for you.
      </p>

      {loadingStatus ? (
        <div className="text-xs text-gray-500">Loading connections…</div>
      ) : installations.length === 0 ? (
        <div className="space-y-3">
          <ol className="text-xs text-gray-400 space-y-1 list-decimal list-inside">
            <li>Click "Connect GitHub" below</li>
            <li>Sign in to GitHub if asked, then pick the repos to share</li>
            <li>You'll land back here, fully connected — nothing else to configure</li>
          </ol>
          <button
            onClick={handleConnect}
            disabled={connecting}
            className="flex items-center gap-2 px-4 py-2 rounded-md bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-sm font-medium text-white transition-colors"
          >
            <GitHub fontSize="small" />
            {connecting ? 'Redirecting to GitHub…' : 'Connect GitHub'}
          </button>
          <p className="text-xs text-gray-500">
            Starts out read-only — Claude can look at your code but can't change anything
            until you turn on write access below.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {installations.map(inst => (
            <div key={inst.installation_id} className="flex items-center justify-between gap-3 rounded-md border border-gray-700 bg-gray-900/40 p-3">
              <div className="flex items-center gap-2 min-w-0">
                <CheckCircle className="text-emerald-400 shrink-0" fontSize="small" />
                <div className="min-w-0">
                  <div className="text-sm text-gray-200 truncate">{inst.account_login}</div>
                  <div className="text-xs text-gray-500">Connected {new Date(inst.connected_at).toLocaleDateString()}</div>
                </div>
              </div>
              <div className="flex flex-col items-end gap-1 shrink-0">
                <div className="flex items-center gap-2">
                  <select
                    value={inst.permission_tier}
                    disabled={savingTierFor === inst.installation_id}
                    onChange={e => handleTierChange(inst.installation_id, e.target.value)}
                    title={TIER_DESCRIPTIONS[inst.permission_tier]}
                    className="text-xs bg-gray-800 border border-gray-600 rounded px-2 py-1 text-gray-200"
                  >
                    <option value="read_only">{TIER_LABELS.read_only}</option>
                    <option value="read_comment">{TIER_LABELS.read_comment}</option>
                    <option value="read_write">{TIER_LABELS.read_write}</option>
                  </select>
                  <button
                    onClick={() => handleDisconnect(inst.installation_id, inst.account_login)}
                    title="Disconnect"
                    className="p-1.5 rounded text-gray-400 hover:text-red-400 hover:bg-red-900/20 transition-colors"
                  >
                    <DeleteOutline fontSize="small" />
                  </button>
                </div>
                <div className="text-[11px] text-gray-500 max-w-[220px] text-right">
                  {TIER_DESCRIPTIONS[inst.permission_tier]}
                </div>
              </div>
            </div>
          ))}
          <button
            onClick={handleConnect}
            disabled={connecting}
            className="text-xs text-emerald-400 hover:text-emerald-300"
          >
            + Connect another account/org
          </button>
        </div>
      )}
    </div>
  )
}
