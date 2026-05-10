import React from 'react'
import { Menu, Edit3, Zap } from 'lucide-react'
import { useAppStore } from '../../stores/appStore'

interface Props {
  onMenuClick: () => void
  onNewChat: () => void
}

export function MobileTopBar({ onMenuClick, onNewChat }: Props) {
  const { sessions, activeSessions, sessionFiles, settings } = useAppStore()
  const activeSession = sessions.find(s => s.id === activeSessions)

  const title = activeSession?.title || 'SurgicalAI'
  const fileCount = sessionFiles.length

  // Compact model label
  const modelLabel = (() => {
    const m = settings?.architect_model || ''
    if (m.includes('claude-opus')) return 'Opus'
    if (m.includes('claude-sonnet')) return 'Sonnet'
    if (m.includes('gpt-4.1')) return 'GPT-4.1'
    return ''
  })()

  return (
    <header className="flex items-center gap-2 h-14 px-3 border-b border-border bg-base flex-shrink-0 relative z-10">
      <button
        onClick={onMenuClick}
        className="flex items-center justify-center w-10 h-10 -ml-1 rounded-lg text-ink hover:bg-overlay active:bg-overlay/80 transition-colors"
        aria-label="Open menu"
      >
        <Menu size={22} strokeWidth={2} />
      </button>

      <div className="flex-1 min-w-0 flex items-center gap-2">
        <Zap size={14} className="text-accent flex-shrink-0" />
        <div className="min-w-0 flex-1">
          <h1 className="text-[15px] font-semibold text-ink truncate leading-tight">{title}</h1>
          {(fileCount > 0 || modelLabel) && (
            <p className="text-[11px] text-muted truncate leading-tight">
              {modelLabel}
              {fileCount > 0 && (
                <>
                  {modelLabel && <span className="mx-1.5 opacity-50">·</span>}
                  {fileCount} file{fileCount !== 1 ? 's' : ''}
                </>
              )}
            </p>
          )}
        </div>
      </div>

      <button
        onClick={onNewChat}
        className="flex items-center justify-center w-10 h-10 -mr-1 rounded-lg text-ink hover:bg-overlay active:bg-overlay/80 transition-colors"
        aria-label="New chat"
      >
        <Edit3 size={18} strokeWidth={2} />
      </button>
    </header>
  )
}
