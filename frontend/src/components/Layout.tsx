import React from 'react'
import { Sidebar } from './Sidebar'
import { ChatPanel } from './ChatPanel'
import { CodePanel } from './CodePanel'
import { useAppStore } from '../stores/appStore'

export function Layout() {
  const activeFile = useAppStore(s => s.activeFile)

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Sidebar — fixed 256px */}
      <aside className="w-64 flex-shrink-0 border-r border-border flex flex-col bg-surface">
        <Sidebar />
      </aside>

      {/* Chat — expands to fill all space when no file is open, collapses to 460px when code panel is active */}
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
