import React, { useEffect, useState } from 'react'
import { MobileTopBar } from './MobileTopBar'
import { MobileDrawer } from './MobileDrawer'
import { MobileChat } from './MobileChat'
import { MobileFilesSheet } from './MobileFilesSheet'
import { useAppStore } from '../../stores/appStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'

/**
 * Root mobile container — position:fixed + inset:0 so the layout is always
 * anchored to the visual viewport. This prevents the topbar from scrolling
 * off-screen when the iOS/Android virtual keyboard opens or closes.
 *
 * Safe-area bottom inset is applied ONLY by the input component (MobileChat).
 * Never add paddingBottom here — that would double-stack with the child's inset.
 */
export function MobileLayout() {
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [filesSheetOpen, setFilesSheetOpen] = useState(false)
  const {
    setActiveSession, setMessages, setSessions, setSessionFiles, activeSessions,
  } = useAppStore()

  // ── Prevent body scroll — stops iOS/Android from scrolling the layout when
  //    the keyboard opens, which would push the topbar off screen.
  useEffect(() => {
    const prev = { overflow: document.body.style.overflow, position: document.body.style.position, width: document.body.style.width }
    document.body.style.overflow = 'hidden'
    document.body.style.position = 'fixed'
    document.body.style.width = '100%'
    return () => {
      document.body.style.overflow = prev.overflow
      document.body.style.position = prev.position
      document.body.style.width = prev.width
    }
  }, [])

  // ── Load session list so the drawer shows chat history immediately ──────────
  useEffect(() => {
    api.chat.getSessions().then(setSessions).catch(() => {})
  }, [])

  // ── Restore messages for previously active session on page reload ───────────
  useEffect(() => {
    if (activeSessions) {
      api.chat.getMessages(activeSessions).then(setMessages).catch(() => {})
    }
    // intentionally only on mount — session switches handled by MobileDrawer
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
      className="flex flex-col bg-base text-ink"
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        overflow: 'hidden',
        // Only top safe-area here — bottom is handled exclusively by MobileChat input
        paddingTop: 'env(safe-area-inset-top)',
        paddingLeft: 'env(safe-area-inset-left)',
        paddingRight: 'env(safe-area-inset-right)',
      }}
    >
      {/* TopBar — flex-shrink-0 so it never gets pushed off by message content */}
      <MobileTopBar
        onMenuClick={() => setDrawerOpen(true)}
        onNewChat={handleNewChat}
      />

      {/* Chat area — flex-1 flex-col so MobileChat can fill remaining height */}
      <main className="flex flex-col flex-1 min-h-0">
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
