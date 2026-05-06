import React from 'react'
import { Sidebar } from './Sidebar'
import { ChatPanel } from './ChatPanel'
import { CodePanel } from './CodePanel'

export function Layout() {
  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Sidebar — fixed 256px */}
      <aside className="w-64 flex-shrink-0 border-r border-border flex flex-col bg-surface">
        <Sidebar />
      </aside>

      {/* Chat — fixed 400px */}
      <section className="w-[400px] flex-shrink-0 border-r border-border flex flex-col bg-base">
        <ChatPanel />
      </section>

      {/* Editor — fills remaining */}
      <section className="flex-1 flex flex-col bg-base min-w-0">
        <CodePanel />
      </section>
    </div>
  )
}
