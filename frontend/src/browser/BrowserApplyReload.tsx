import { useEffect } from 'react'
import { browserApi } from './browserClient'
import { useBrowserStore } from './browserStore'

/** Reloads the live page after an edit is applied, even if the tab is hidden. */
export function BrowserApplyReload() {
  useEffect(() => {
    const onApplied = () => {
      const state = useBrowserStore.getState()
      if (!state.sessionUsed || !state.reloadAfterApply) return
      browserApi.reload().catch(() => {})
    }
    window.addEventListener('sai-applied-refresh', onApplied)
    return () => window.removeEventListener('sai-applied-refresh', onApplied)
  }, [])
  return null
}
