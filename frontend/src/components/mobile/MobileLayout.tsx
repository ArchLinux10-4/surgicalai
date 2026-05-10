import React, { useState } from 'react'
import { MobileTopBar } from './MobileTopBar'
import { MobileDrawer } from './MobileDrawer'
import { MobileChat } from './MobileChat'
import { MobileFilesSheet } from './MobileFilesSheet'
import { useAppStore } from '../../stores/appStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'

/**
 * Root mobile container. Owns drawer + files sheet state, hosts the chat screen.
 * Renders nothing for desktop — Layout.tsx routes based on viewport.
 */
export function MobileLayout() {
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [filesSheetOpen, setFilesSheetOpen] = useState(false)
  const { setActiveSession, setMessages, setSessions, setSessionFiles } = useAppStore()

  const handleNewChat = async () => {
    try {
      const s = await api.chat.createSession({ title: 'New Chat' })
      const updated = await api.chat.getSessions()
      setSessions(updated)
      setActiveSession(s.id)
      setMessages([])
      setSessionFiles([])
    } catch {
      toast.error('Failed to create chat')
    }
  }

  return (
    <div
      className="flex flex-col h-full bg-base text-ink relative overflow-hidden"
      style={{
        // Use dynamic viewport height for iOS — accounts for collapsing browser chrome
        height: '100dvh',
        paddingTop: 'env(safe-area-inset-top)',
      }}
    >
      <MobileTopBar
        onMenuClick={() => setDrawerOpen(true)}
        onNewChat={handleNewChat}
      />

      <main className="flex-1 min-h-0 overflow-hidden">
        <MobileChat onOpenFiles={() => setFilesSheetOpen(true)} />
      </main>

      <MobileDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        onOpenFiles={() => setFilesSheetOpen(true)}
      />

      <MobileFilesSheet
        open={filesSheetOpen}
        onClose={() => setFilesSheetOpen(false)}
      />
    </div>
  )
}
