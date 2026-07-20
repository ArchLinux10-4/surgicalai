import React, { useState, useCallback, useRef } from 'react'
import { Sidebar } from './Sidebar'
import { ChatPanel } from './ChatPanel'
import { CodePanel } from './CodePanel'
import { ElementPickerPanel } from './ElementPickerPanel'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { LoginPage } from '../pages/LoginPage'
import { ImageStudio } from './ImageStudio'

// Sidebar width constants
const SIDEBAR_MIN_PX = 264   // 44px rail + 220px panel — current default
const SIDEBAR_MAX_PX = Math.round(SIDEBAR_MIN_PX * 1.4)  // +40% = ~370px

// Chat panel width constants (used when a file or the Element Picker is
// docked beside it) — same min/max-clamped drag convention as the sidebar
// above, just on the opposite edge. Min keeps the composer/message bubbles
// usable; max leaves at least ~30% of a typical 1280px window for the
// docked pane so it's never squeezed to nothing.
const CHAT_MIN_PX = 340
const CHAT_MAX_PX = 900
const CHAT_DEFAULT_PX = 460
const CHAT_WIDTH_STORAGE_KEY = 'surgicalai:chatPanelWidth'

function loadChatWidth(): number {
  try {
    const saved = Number(localStorage.getItem(CHAT_WIDTH_STORAGE_KEY))
    if (Number.isFinite(saved) && saved >= CHAT_MIN_PX && saved <= CHAT_MAX_PX) return saved
  } catch { /* localStorage unavailable — fall back to default */ }
  return CHAT_DEFAULT_PX
}

export function Layout() {
  const activeFile = useAppStore(s => s.activeFile)
  const sidebarPinned = useAppStore(s => s.sidebarPinned)
  // Element Picker docks into the same third-pane slot CodePanel uses below
  // — one main view beside chat at a time, same convention Cursor's own
  // in-app Browser pane follows. It takes precedence over an open file
  // rather than stacking a fourth column, matching the "keep it simple,
  // one thing docked beside chat" decision from user feedback.
  const elementPickerOpen = useAppStore(s => s.elementPickerModalOpen)
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

  // Chat panel resize — same grab-and-drag convention as the sidebar handle
  // above, mirrored onto the chat/docked-pane boundary. One width value
  // drives both the CodePanel split and the Element Picker split since only
  // one of those two ever occupies the third-pane slot at a time.
  const [chatWidth, setChatWidth] = useState(loadChatWidth)
  const chatWidthRef    = useRef(chatWidth)
  const chatDragStartX  = useRef<number>(0)
  const chatDragStartW  = useRef<number>(chatWidth)
  const isChatDragging  = useRef(false)
  chatWidthRef.current = chatWidth

  const onChatDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    isChatDragging.current  = true
    chatDragStartX.current  = e.clientX
    chatDragStartW.current  = chatWidthRef.current

    const onMove = (ev: MouseEvent) => {
      if (!isChatDragging.current) return
      const delta = ev.clientX - chatDragStartX.current
      const next  = Math.min(CHAT_MAX_PX, Math.max(CHAT_MIN_PX, chatDragStartW.current + delta))
      setChatWidth(next)
    }
    const onUp = () => {
      isChatDragging.current = false
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      try { localStorage.setItem(CHAT_WIDTH_STORAGE_KEY, String(chatWidthRef.current)) } catch { /* best-effort */ }
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [])

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

      {/* Chat — expands to fill all space when no file/picker pane is docked.
          Width comes from the drag-resizable `chatWidth` state (not a fixed
          Tailwind width) whenever a third pane is docked, same idea as the
          sidebar's own resizable width above; the `transition-all` is
          suppressed mid-drag (via the dragging check below is unnecessary
          here since width is only set on mouseup-free `next` per move —
          keeping the transition off during active drags avoids the resize
          feeling laggy/rubber-banded behind the mouse). */}
      <section
        className={`flex flex-col bg-base overflow-hidden ${
          (activeFile || elementPickerOpen)
            ? 'flex-shrink-0 border-r border-border'
            : 'flex-1 transition-all duration-200'
        }`}
        style={(activeFile || elementPickerOpen) ? { width: chatWidth } : undefined}
      >
        <ChatPanel />
      </section>

      {/* Drag handle — boundary between chat and whichever third pane is
          docked (CodePanel or Element Picker). Same visual language as the
          sidebar's own handle for consistency. */}
      {(activeFile || elementPickerOpen) && (
        <div
          onMouseDown={onChatDragStart}
          className="w-1 flex-shrink-0 cursor-col-resize z-10 relative
            hover:bg-accent/40 active:bg-accent/60 transition-colors group"
          title="Drag to resize chat panel"
        >
          <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 flex flex-col gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            {[0, 1, 2].map(i => (
              <div key={i} className="w-0.5 h-1 bg-muted/60 rounded-full" />
            ))}
          </div>
        </div>
      )}

      {/* Third pane — Element Picker takes priority over an open code file
          (one main view beside chat at a time, not a stacked fourth column) */}
      {elementPickerOpen ? (
        <ElementPickerPanel />
      ) : activeFile ? (
        <section className="flex-1 flex flex-col bg-base min-w-0">
          <CodePanel />
        </section>
      ) : null}
    </div>
  )
}
