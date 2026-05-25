/**
 * MobileLayout — full-screen tab shell for SurgicalAI on mobile.
 * Three tabs: Chat, Sessions, Files.
 * Zero overlap with desktop components — this file tree is completely isolated.
 */
import React, { useState } from 'react'
import { MobileChatPanel } from './MobileChatPanel'
import { MobileSessionsPanel } from './MobileSessionsPanel'
import { MobileFilesPanel } from './MobileFilesPanel'
import { useAuthStore } from '../../stores/authStore'
import { LoginPage } from '../../pages/LoginPage'

type Tab = 'chat' | 'sessions' | 'files'

// ── SVG Icons (no MUI dependency on mobile — keeps bundle lighter) ──────────
const ChatIcon = ({ active }: { active: boolean }) => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
    stroke={active ? '#f97316' : 'rgba(148,163,184,0.5)'}
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
  </svg>
)

const SessionsIcon = ({ active }: { active: boolean }) => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
    stroke={active ? '#f97316' : 'rgba(148,163,184,0.5)'}
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="8" y1="6" x2="21" y2="6" />
    <line x1="8" y1="12" x2="21" y2="12" />
    <line x1="8" y1="18" x2="21" y2="18" />
    <line x1="3" y1="6" x2="3.01" y2="6" />
    <line x1="3" y1="12" x2="3.01" y2="12" />
    <line x1="3" y1="18" x2="3.01" y2="18" />
  </svg>
)

const FilesIcon = ({ active }: { active: boolean }) => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
    stroke={active ? '#f97316' : 'rgba(148,163,184,0.5)'}
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
    <polyline points="13 2 13 9 20 9" />
  </svg>
)

export function MobileLayout() {
  const { isAuthenticated } = useAuthStore()
  const [activeTab, setActiveTab] = useState<Tab>('chat')

  if (!isAuthenticated) {
    return <LoginPage />
  }

  return (
    <div className="flex flex-col h-screen bg-base text-ink overflow-hidden">
      {/* Content area — fills all space above tab bar */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === 'chat'     && <MobileChatPanel />}
        {activeTab === 'sessions' && <MobileSessionsPanel onSelectSession={() => setActiveTab('chat')} />}
        {activeTab === 'files'    && <MobileFilesPanel />}
      </div>

      {/* Tab bar — fixed at bottom */}
      <nav className="flex-shrink-0 flex border-t border-border bg-surface">
        {([
          { id: 'chat',     label: 'Chat',     Icon: ChatIcon     },
          { id: 'sessions', label: 'Sessions', Icon: SessionsIcon },
          { id: 'files',    label: 'Files',    Icon: FilesIcon    },
        ] as { id: Tab; label: string; Icon: any }[]).map(({ id, label, Icon }) => (
          <button
            key={id}
            onClick={() => setActiveTab(id)}
            className={`flex-1 flex flex-col items-center justify-center gap-1 py-3 text-[10px] font-medium transition-colors
              ${activeTab === id ? 'text-orange' : 'text-muted/50 hover:text-muted'}`}
          >
            <Icon active={activeTab === id} />
            {label}
          </button>
        ))}
      </nav>
    </div>
  )
}
