import React, { useEffect } from 'react'
import { Layout } from './components/Layout'
import { SettingsModal } from './components/SettingsModal'
import { Toaster } from './components/Toast'
import { useAppStore } from './stores/appStore'
import { useAuthStore } from './stores/authStore'
import { api } from './api/client'

export default function App() {
  const { setSettings, settings, setSettingsOpen } = useAppStore()
  const { isAuthenticated } = useAuthStore()
  const [settingsLoaded, setSettingsLoaded] = React.useState(false)

  // Only fetch settings when actually authenticated.
  // This prevents the banner from flashing on the login screen when a stale
  // token lingers in localStorage before the auto-logout 401 fires.
  useEffect(() => {
    if (!isAuthenticated) {
      // Not logged in — reset "loaded" flag so if user logs back in we re-fetch
      // fresh settings. Banner is also gated on isAuthenticated so it won't show.
      setSettingsLoaded(false)
      return
    }
    setSettingsLoaded(false)
    api.settings.get()
      .then((s) => { setSettings(s); setSettingsLoaded(true) })
      .catch(() => setSettingsLoaded(true))
  }, [isAuthenticated])

  return (
    <div className="h-screen flex flex-col bg-base text-ink overflow-hidden">
      {/* Only show banner when:
           1. User is actually logged in
           2. Settings have finished loading (no flash while fetching)
           3. Key genuinely missing */}
      {isAuthenticated && settingsLoaded && !settings?.openai_api_key_set && (
        <div className="flex items-center gap-3 px-4 py-2 bg-surface border-b border-orange/30 text-orange text-xs">
          <span className="text-base">⚠️</span>
          <span className="font-medium">OpenAI API key not configured — AI features are disabled.</span>
          <button
            onClick={() => setSettingsOpen(true)}
            className="ml-auto px-3 py-1 rounded-md bg-orange text-base text-xs font-bold hover:bg-orange/90 transition-colors"
          >
            Add Key →
          </button>
        </div>
      )}
      <Layout />
      <SettingsModal />
      <Toaster />
    </div>
  )
}
