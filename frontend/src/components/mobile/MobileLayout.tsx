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
import { MobileGitHubSheet } from './MobileGitHubSheet'
import { useAuthStore } from '../../stores/authStore'
import { useAppStore } from '../../stores/appStore'
import { useThemeStore } from '../../stores/themeStore'
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
  const { theme, toggleTheme } = useThemeStore()
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
          <div className="w-11 h-11 rounded-full bg-[rgba(74,222,128,0.12)] border-2 border-[rgba(74,222,128,0.25)]
            flex items-center justify-center text-[#4ade80] font-bold text-sm flex-shrink-0">
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

          <button
            onClick={toggleTheme}
            className="w-full flex items-center gap-3.5 px-5 py-4 text-sm text-ink/80
              hover:bg-overlay/60 active:bg-overlay transition-colors text-left"
          >
            <span className="text-muted/60">
              {theme === 'dark' ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/>
                  <line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
                  <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/>
                  <line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
                  <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
                </svg>
              ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
                </svg>
              )}
            </span>
            <span className="font-medium">{theme === 'dark' ? 'Light mode' : 'Dark mode'}</span>
            <span className="ml-auto text-[11px] text-muted/40">Currently {theme}</span>
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
  const [githubOpen, setGithubOpen] = useState(false)

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
    <div className="fixed inset-0 flex flex-col bg-base text-ink overflow-hidden">
      {/* Global header */}
      <header className="flex-shrink-0 flex items-center justify-between
        px-4 border-b border-border bg-surface/90 backdrop-blur-sm"
        style={{ paddingTop: 'max(env(safe-area-inset-top, 0px), 12px)', paddingBottom: '10px' }}
      >
        {/* Logo + current tab name */}
        <div className="flex items-center gap-2.5">
          <img src="/otter.png" alt="SurgicalAI" className="w-7 h-7 rounded-lg flex-shrink-0" />
          <div>
            <span className="text-[13px] font-semibold text-ink/80">SurgicalAI</span>
            <span className="text-muted/40 text-[12px] mx-1.5">·</span>
            <span className="text-[12px] text-muted/60">{tabLabel[activeTab]}</span>
          </div>
        </div>

        {/* Right: GitHub + user avatar */}
        <div className="flex items-center gap-2">
          {/* GitHub browser button */}
          <button
            onClick={() => setGithubOpen(true)}
            className="w-8 h-8 rounded-lg border border-border bg-surface/60
              flex items-center justify-center text-muted/60
              hover:text-ink hover:border-border/80 active:scale-95 transition-all"
            aria-label="Browse GitHub"
            title="GitHub files"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.54-1.38-1.33-1.75-1.33-1.75-1.09-.74.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49 1 .1-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.17 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 3-.4c1.02.005 2.04.138 3 .4 2.28-1.55 3.29-1.23 3.29-1.23.66 1.65.24 2.87.12 3.17.77.84 1.23 1.91 1.23 3.22 0 4.61-2.81 5.63-5.48 5.92.43.37.82 1.1.82 2.22v3.29c0 .32.21.7.83.58C20.57 21.8 24 17.3 24 12c0-6.63-5.37-12-12-12z"/>
            </svg>
          </button>

          {/* User avatar — opens menu */}
          <button
            onClick={() => setMenuOpen(true)}
            className="w-8 h-8 rounded-full bg-[rgba(74,222,128,0.12)] border border-[rgba(74,222,128,0.25)]
              flex items-center justify-center text-[#4ade80] text-[11px] font-bold
              hover:bg-[rgba(74,222,128,0.2)] active:scale-95 transition-all"
            aria-label="Open menu"
          >
            {initials}
          </button>
        </div>
      </header>

      {/* Content area */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {activeTab === 'chat'     && <MobileChatPanel />}
        {activeTab === 'sessions' && <MobileSessionsPanel onSelectSession={() => setActiveTab('chat')} />}
        {activeTab === 'files'    && <MobileFilesPanel />}
      </div>

      {/* Tab bar */}
      <nav className="flex-shrink-0 flex border-t border-border bg-surface">
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
              ${activeTab === id ? 'text-[#4ade80]' : 'text-muted/50 hover:text-muted'}`}
          >
            <Icon active={activeTab === id} />
            {label}
          </button>
        ))}
      </nav>

      {/* Slide-up menu drawer */}
      <MenuDrawer open={menuOpen} onClose={() => setMenuOpen(false)} />

      {/* GitHub bottom sheet */}
      <MobileGitHubSheet
        open={githubOpen}
        onClose={() => setGithubOpen(false)}
        onOpenSettings={() => { setMenuOpen(true) }}
      />
    </div>
  )
}
