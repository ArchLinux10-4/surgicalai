import { useEffect, useRef } from 'react'
import { api } from '../api/client'
import { githubTargetEpochNow } from '../lib/githubBranch'
import { useAppStore } from '../stores/appStore'
import type { GitHubTargetState } from '../types'

const EMPTY: GitHubTargetState = {
  target: null,
  connections: { pat: false, app: false, app_configured: false },
}

/**
 * Loads the chat's GitHub target. Undefined until the first response, so the
 * chip can render nothing and avoid a layout jump. A later response for an
 * older chat is ignored.
 *
 * `pulse` is the composer's disabled flag. When a turn finishes it goes from
 * true to false and the target is read again, which is how a file the agent
 * just registered shows up on the chip.
 */
export function useGithubTarget(sessionId: string | null, pulse = false): GitHubTargetState | undefined {
  const entry = useAppStore(s => (sessionId ? s.githubTargets[sessionId] : undefined))
  const setGithubTarget = useAppStore(s => s.setGithubTarget)
  const seq = useRef(0)
  const prevPulse = useRef<boolean | null>(null)

  useEffect(() => {
    if (!sessionId) return
    const requestId = ++seq.current
    const epoch = githubTargetEpochNow()
    let cancelled = false
    api.chat.getGithubTarget(sessionId)
      .then(data => {
        if (cancelled || seq.current !== requestId || githubTargetEpochNow() !== epoch) return
        setGithubTarget(sessionId, data)
      })
      .catch(() => {
        if (cancelled || seq.current !== requestId || githubTargetEpochNow() !== epoch) return
        if (!useAppStore.getState().githubTargets[sessionId]) {
          setGithubTarget(sessionId, EMPTY)
        }
      })
    return () => { cancelled = true }
  }, [sessionId, setGithubTarget])

  useEffect(() => {
    const wasBusy = prevPulse.current === true
    prevPulse.current = pulse
    if (!sessionId || !wasBusy || pulse) return
    const requestId = ++seq.current
    const epoch = githubTargetEpochNow()
    let cancelled = false
    api.chat.getGithubTarget(sessionId)
      .then(data => {
        if (cancelled || seq.current !== requestId || githubTargetEpochNow() !== epoch) return
        setGithubTarget(sessionId, data)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [pulse, sessionId, setGithubTarget])

  return sessionId ? entry : undefined
}
