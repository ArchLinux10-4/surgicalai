import { api } from '../api/client'
import { parseServerUtc } from './relativeTime'
import type { GitHubConnections, GitHubTargetState, SessionFile } from '../types'
import { useAppStore } from '../stores/appStore'

let githubTargetEpoch = 0

/** Bumped on every local write so an older GET cannot overwrite the chip. */
export function noteGithubTargetWrite() {
  githubTargetEpoch += 1
}

export function githubTargetEpochNow() {
  return githubTargetEpoch
}

/** Same rule as the "Modified · not pushed" badge in SessionFilesTray. */
export function githubFileModifiedSincePush(file: SessionFile): boolean {
  if (!file.github_meta || !file.github_pushed_at) return false
  const pushed = parseServerUtc(file.github_pushed_at)?.getTime() ?? 0
  const updated = parseServerUtc(file.updated_at)?.getTime() ?? 0
  return updated > pushed + 2000
}

export function githubLoadedKey(owner: string, repo: string, branch: string, path: string): string {
  return `${owner}/${repo}@${branch}:${path}`
}

/** Ask before a load would replace a file that shows as modified. */
export function confirmReplaceEditedCopy(
  files: SessionFile[],
  owner: string,
  repo: string,
  paths: string[],
  branch: string,
): boolean {
  const prefixes = paths.map(p => p.replace(/\/$/, ''))
  const hit = files.some(file => {
    if (!githubFileModifiedSincePush(file) || !file.github_meta) return false
    let meta: { owner?: string; repo?: string; path?: string }
    try { meta = JSON.parse(file.github_meta) } catch { return false }
    if (meta.owner !== owner || meta.repo !== repo || !meta.path) return false
    return prefixes.some(p => meta.path === p || meta.path!.startsWith(`${p}/`))
  })
  if (!hit) return true
  return window.confirm(`Replace your edited copy with \`${branch}\`?`)
}

export async function fetchGithubBranches(
  owner: string,
  repo: string,
  connections: GitHubConnections,
): Promise<{ branches: string[]; default?: string }> {
  if (connections.pat) {
    try {
      return await api.github.branches(owner, repo)
    } catch (err) {
      if (!connections.app) throw err
    }
  }
  if (connections.app) return await api.githubApp.branches(owner, repo)
  throw new Error('GitHub is not connected')
}

/** Optimistic store write, then PATCH. Reverts the chip if the save fails. */
export async function saveGithubTarget(sessionId: string, owner: string, repo: string, branch: string) {
  noteGithubTargetWrite()
  const store = useAppStore.getState()
  const prev = store.githubTargets[sessionId]
  const connections = prev?.connections ?? { pat: false, app: false, app_configured: false }
  const optimistic: GitHubTargetState = {
    target: { owner, repo, branch, explicit: true },
    connections,
  }
  store.setGithubTarget(sessionId, optimistic)
  try {
    await api.chat.setGithubTarget(sessionId, { owner, repo, branch })
  } catch (err) {
    useAppStore.getState().setGithubTarget(sessionId, prev)
    throw err
  }
  try {
    const fresh = await api.chat.getGithubTarget(sessionId)
    useAppStore.getState().setGithubTarget(sessionId, fresh)
  } catch {
    // The branch was saved. Keep the optimistic chip if the refresh fails.
  }
}
