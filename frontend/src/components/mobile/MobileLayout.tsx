/**
 * MobileLayout — full-screen tab shell for SurgicalAI on mobile.
 * Three tabs: Chat, Sessions, Files.
 * Header shows current user + hamburger menu button.
 * Menu drawer: user info, Settings (opens shared SettingsModal), Logout.
 * Zero overlap with desktop components — this file tree is completely isolated.
 * Settings are opened via appStore.setSettingsOpen which triggers the shared
 * SettingsModal already rendered in App.tsx — no new modal needed.
 */
import React, { useState, useEffect, useRef } from 'react'
import { MobileChatPanel } from './MobileChatPanel'
import { MobileSessionsPanel } from './MobileSessionsPanel'
import { MobileFilesPanel } from './MobileFilesPanel'
import { useAuthStore } from '../../stores/authStore'
import { useAppStore } from '../../stores/appStore'
import { LoginPage } from '../../pages/LoginPage'

type Tab = 'chat' | 'sessions' | 'files'

// ── SVG Icons ────────────────────────────────────────────────────────────────
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

const SettingsIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
)

const LogoutIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <polyline points="16 17 21 12 16 7" />
    <line x1="21" y1="12" x2="9" y2="12" />
  </svg>
)

const UserIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
    <circle cx="12" cy="7" r="4" />
  </svg>
)

// ── Menu Drawer ───────────────────────────────────────────────────────────────
function MenuDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user, logout } = useAuthStore()
  const { setSettingsOpen } = useAppStore()
  const overlayRef = useRef<HTMLDivElement>(null)

  // Close on backdrop tap
  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === overlayRef.current) onClose()
  }

  // Close on back gesture / escape
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const handleSettings = () => {
    onClose()
    // Small delay so drawer closes cleanly before modal opens
    setTimeout(() => setSettingsOpen(true), 150)
  }

  const handleLogout = () => {
    onClose()
    logout()
  }

  const initials = (user?.username || 'U')
    .split(/[\s_-]/).map((w: string) => w[0]?.toUpperCase() || '').join('').slice(0, 2) || 'U'

  return (
    <>
      {/* Backdrop */}
      <div
        ref={overlayRef}
        onClick={handleOverlayClick}
        className={`fixed inset-0 z-40 transition-all duration-300 ${
          open ? 'bg-black/50 pointer-events-auto' : 'bg-transparent pointer-events-none'
        }`}
        aria-hidden={!open}
      />

      {/* Drawer — slides up from bottom */}
      <div
        className={`fixed bottom-0 left-0 right-0 z-50 bg-surface border-t border-border rounded-t-2xl
          transition-transform duration-300 ease-out shadow-2xl
          ${open ? 'translate-y-0' : 'translate-y-full'}`}
        role="dialog"
        aria-modal="true"
        aria-label="Menu"
      >
        {/* Drag handle */}
        <div className="flex justify-center pt-3 pb-1">
          <div className="w-10 h-1 rounded-full bg-border" />
        </div>

        {/* User info section */}
        <div className="flex items-center gap-3 px-5 py-4 border-b border-border/50">
          <div className="w-11 h-11 rounded-full bg-orange/20 border-2 border-orange/30
            flex items-center justify-center text-orange font-bold text-sm flex-shrink-0">
            {initials}
          </div>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-ink truncate">
              {user?.username || 'User'}
            </p>
            {user?.email && (
              <p className="text-[11px] text-muted/60 truncate">{user.email}</p>
            )}
            <p className="text-[10px] text-muted/40 mt-0.5">SurgicalAI</p>
          </div>
        </div>

        {/* Menu items */}
        <div className="py-2">
          <button
            onClick={handleSettings}
            className="w-full flex items-center gap-3.5 px-5 py-4 text-sm text-ink/80
              hover:bg-overlay/60 active:bg-overlay transition-colors text-left"
          >
            <span className="text-muted/60"><SettingsIcon /></span>
            <span className="font-medium">Settings</span>
            <span className="ml-auto text-[11px] text-muted/40">API keys, models</span>
          </button>

          <div className="mx-5 border-t border-border/50" />

          <button
            onClick={handleLogout}
            className="w-full flex items-center gap-3.5 px-5 py-4 text-sm
              text-red-400 hover:bg-red-400/5 active:bg-red-400/10 transition-colors text-left"
          >
            <LogoutIcon />
            <span className="font-medium">Log out</span>
            <span className="ml-auto text-[11px] text-red-400/40">{user?.username}</span>
          </button>
        </div>

        {/* Safe area spacer for phones with home indicator */}
        <div className="h-safe-area-bottom" style={{ height: 'env(safe-area-inset-bottom, 8px)' }} />
      </div>
    </>
  )
}

// ── Main Layout ───────────────────────────────────────────────────────────────
export function MobileLayout() {
  const { isAuthenticated, user } = useAuthStore()
  const [activeTab, setActiveTab] = useState<Tab>('chat')
  const [menuOpen, setMenuOpen]   = useState(false)

  if (!isAuthenticated) {
    return <LoginPage />
  }

  const initials = (user?.username || 'U')
    .split(/[\s_-]/).map((w: string) => w[0]?.toUpperCase() || '').join('').slice(0, 2) || 'U'

  const tabLabel: Record<Tab, string> = {
    chat: 'Chat',
    sessions: 'Sessions',
    files: 'Files',
  }

  return (
    <div className="flex flex-col h-screen bg-base text-ink overflow-hidden">
      {/* Global header — always visible, shows current tab + user avatar */}
      <header className="flex-shrink-0 flex items-center justify-between
        px-4 border-b border-border bg-surface/90 backdrop-blur-sm"
        style={{ paddingTop: 'env(safe-area-inset-top, 12px)', paddingBottom: '10px' }}
      >
        {/* Logo + current tab name */}
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg bg-orange/20 border border-orange/30
            flex items-center justify-center flex-shrink-0">
            <span className="text-orange text-[11px] font-bold">S</span>
          </div>
          <div>
            <span className="text-[13px] font-semibold text-ink/80">SurgicalAI</span>
            <span className="text-muted/40 text-[12px] mx-1.5">·</span>
            <span className="text-[12px] text-muted/60">{tabLabel[activeTab]}</span>
          </div>
        </div>

        {/* User avatar — opens menu */}
        <button
          onClick={() => setMenuOpen(true)}
          className="w-8 h-8 rounded-full bg-orange/20 border border-orange/30
            flex items-center justify-center text-orange text-[11px] font-bold
            hover:bg-orange/30 active:scale-95 transition-all"
          aria-label="Open menu"
        >
          {initials}
        </button>
      </header>

      {/* Content area */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === 'chat'     && <MobileChatPanel />}
        {activeTab === 'sessions' && <MobileSessionsPanel onSelectSession={() => setActiveTab('chat')} />}
        {activeTab === 'files'    && <MobileFilesPanel />}
      </div>

      {/* Tab bar */}
      <nav className="flex-shrink-0 flex border-t border-border bg-surface"
        style={{ paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}
      >
        {([
          { id: 'chat',     label: 'Chat',     Icon: ChatIcon     },
          { id: 'sessions', label: 'Sessions', Icon: SessionsIcon },
          { id: 'files',    label: 'Files',    Icon: FilesIcon    },
        ] as { id: Tab; label: string; Icon: any }[]).map(({ id, label, Icon }) => (
          <button
            key={id}
            onClick={() => setActiveTab(id as Tab)}
            className={`flex-1 flex flex-col items-center justify-center gap-1 py-3
              text-[10px] font-medium transition-colors
              ${activeTab === id ? 'text-orange' : 'text-muted/50 hover:text-muted'}`}
          >
            <Icon active={activeTab === id} />
            {label}
          </button>
        ))}
      </nav>

      {/* Slide-up menu drawer */}
      <MenuDrawer open={menuOpen} onClose={() => setMenuOpen(false)} />
    </div>
  )
}
