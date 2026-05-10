import React from 'react'
import { Sidebar } from './Sidebar'
import { ChatPanel } from './ChatPanel'
import { CodePanel } from './CodePanel'
import { MobileLayout } from './mobile/MobileLayout'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { useIsMobile } from '../hooks/useIsMobile'
import { LoginPage } from '../pages/LoginPage'

export function Layout() {
  const activeFile = useAppStore(s => s.activeFile)
  const { isAuthenticated } = useAuthStore()
  const isMobile = useIsMobile()

  if (!isAuthenticated) {
    return <LoginPage />
  }

  // Mobile: render dedicated mobile experience
  if (isMobile) {
    return <MobileLayout />
  }

  // Desktop: untouched
  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Sidebar — self-sizing: rail (44px always) + panel (220px when open) */}
      <aside className="flex-shrink-0 flex flex-col bg-surface overflow-hidden">
        <Sidebar />
      </aside>

      {/* Chat — expands to fill all space when no file is open */}
      <section className={`flex flex-col bg-base overflow-hidden transition-all duration-200 ${
        activeFile
          ? 'w-[460px] flex-shrink-0 border-r border-border'
          : 'flex-1'
      }`}>
        <ChatPanel />
      </section>

      {/* Code editor — only rendered when a file is actively open */}
      {activeFile && (
        <section className="flex-1 flex flex-col bg-base min-w-0">
          <CodePanel />
        </section>
      )}
    </div>
  )
}
