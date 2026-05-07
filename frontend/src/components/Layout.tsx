import React, { useState } from 'react'
import { Sidebar } from './Sidebar'
import { ChatPanel } from './ChatPanel'
import { CodePanel } from './CodePanel'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { LoginPage } from '../pages/LoginPage'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'

export function Layout() {
  const activeFile = useAppStore(s => s.activeFile)
  const { isAuthenticated } = useAuthStore()
  const [sidebarOpen, setSidebarOpen] = useState(true)

  // Show login/setup screen until authenticated
  if (!isAuthenticated) {
    return <LoginPage />
  }

  return (
    <div className="flex flex-1 overflow-hidden relative">
      {/* Sidebar — collapsible */}
      <aside
        className={`flex-shrink-0 border-r border-border flex flex-col bg-surface overflow-hidden transition-all duration-200 ${
          sidebarOpen ? 'w-64' : 'w-0 border-r-0'
        }`}
      >
        <Sidebar />
      </aside>

      {/* Sidebar toggle button — floats at the left edge of the chat area */}
      <button
        onClick={() => setSidebarOpen(v => !v)}
        title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
        className={`absolute top-3 z-20 flex items-center justify-center w-6 h-6 rounded-md
          bg-surface border border-border text-text-muted hover:text-text hover:bg-surface-hover
          transition-all duration-200 shadow-sm`}
        style={{ left: sidebarOpen ? '248px' : '4px' }}
      >
        {sidebarOpen
          ? <PanelLeftClose size={14} />
          : <PanelLeftOpen size={14} />
        }
      </button>

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
