import React, { useState, useCallback, useRef } from 'react'
import { Sidebar } from './Sidebar'
import { ChatPanel } from './ChatPanel'
import { CodePanel } from './CodePanel'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { LoginPage } from '../pages/LoginPage'
import { ImageStudio } from './ImageStudio'

// Sidebar width constants
const SIDEBAR_MIN_PX = 264   // 44px rail + 220px panel — current default
const SIDEBAR_MAX_PX = Math.round(SIDEBAR_MIN_PX * 1.4)  // +40% = ~370px

export function Layout() {
  const activeFile = useAppStore(s => s.activeFile)
  const sidebarPinned = useAppStore(s => s.sidebarPinned)
  const { isAuthenticated } = useAuthStore()
  // Default to max width — the panel was too cramped at the minimum.
  // Users can still drag it narrower via the resize handle.
  const [sidebarWidth, setSidebarWidth] = useState(SIDEBAR_MAX_PX)
  const dragStartX  = useRef<number>(0)
  const dragStartW  = useRef<number>(SIDEBAR_MAX_PX)
  const isDragging  = useRef(false)

  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    isDragging.current  = true
    dragStartX.current  = e.clientX
    dragStartW.current  = sidebarWidth

    const onMove = (ev: MouseEvent) => {
      if (!isDragging.current) return
      const delta  = ev.clientX - dragStartX.current
      const next   = Math.min(SIDEBAR_MAX_PX, Math.max(SIDEBAR_MIN_PX, dragStartW.current + delta))
      setSidebarWidth(next)
    }
    const onUp = () => {
      isDragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [sidebarWidth])

  if (!isAuthenticated) {
    return <LoginPage />
  }

  // When the panel isn't pinned open, the <aside> shrinks down to just the
  // always-visible 44px icon rail — Sidebar.tsx's transient hover-preview
  // flyout still renders on top (via its own absolute positioning), so it
  // never forces this container — and therefore the main chat/code area — to
  // stay reflowed at full width while collapsed.
  const SIDEBAR_RAIL_PX = 44
  const asideWidth = sidebarPinned ? sidebarWidth : SIDEBAR_RAIL_PX

  return (
    <div className="flex flex-1 overflow-hidden">
      <ImageStudio />
      {/* Sidebar — resizable between SIDEBAR_MIN_PX and SIDEBAR_MAX_PX when pinned open, collapses to the icon rail otherwise */}
      <aside
        className="flex-shrink-0 flex flex-col bg-surface overflow-visible relative z-20 transition-[width] duration-200 ease-out"
        style={{ width: asideWidth }}
      >
        <Sidebar />

        {/* Drag handle — right edge of sidebar, only usable while the panel is pinned open */}
        {sidebarPinned && (
          <div
            onMouseDown={onDragStart}
            className="absolute top-0 right-0 bottom-0 w-1 cursor-col-resize z-10
              hover:bg-accent/40 active:bg-accent/60 transition-colors group"
            title="Drag to resize sidebar"
          >
            {/* Visual indicator — subtle dots */}
            <div className="absolute top-1/2 left-0 -translate-y-1/2 flex flex-col gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
              {[0,1,2].map(i => (
                <div key={i} className="w-0.5 h-1 bg-muted/60 rounded-full" />
              ))}
            </div>
          </div>
        )}
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
