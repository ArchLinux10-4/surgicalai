import React, { useEffect } from 'react'
import { Layout } from './components/Layout'
import { SettingsModal } from './components/SettingsModal'
import { Toaster } from './components/Toast'
import { useAppStore } from './stores/appStore'
import { api } from './api/client'

export default function App() {
  const { setSettings, settings, setSettingsOpen } = useAppStore()

  useEffect(() => {
    api.settings.get().then(setSettings).catch(console.error)
  }, [])

  return (
    <div className="h-screen flex flex-col bg-base text-ink overflow-hidden">
      {!settings?.openai_api_key_set && (
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
