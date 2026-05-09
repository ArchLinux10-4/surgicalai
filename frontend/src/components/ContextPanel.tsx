import React, { useState, useEffect } from 'react'
import { useAppStore } from '../stores/appStore'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import { Pin, Trash2, Save, Check, BookOpen } from 'lucide-react'
import type { PinnedContext } from '../types'

export function ContextPanel() {
  const { activeSessions, activeFile } = useAppStore()
  const [pins, setPins] = useState<PinnedContext[]>([])
  const [memory, setMemory] = useState('')
  const [savedMemory, setSavedMemory] = useState('')
  const [tab, setTab] = useState<'pins' | 'memory'>('pins')
  const [saving, setSaving] = useState(false)
  const [saveDone, setSaveDone] = useState(false)

  useEffect(() => {
    if (!activeSessions) return
    api.context.getPins(activeSessions).then(setPins).catch(() => {})
    api.context.getMemory(activeSessions).then((m) => {
      setMemory(m.content); setSavedMemory(m.content)
    }).catch(() => {})
  }, [activeSessions])

  const pinCurrentFile = async () => {
    if (!activeFile || !activeSessions) return
    try {
      await api.context.addPin({ workspace_path: activeSessions, file_path: activeFile.path })
      api.context.getPins(activeSessions).then(setPins)
      toast.success('File pinned to context')
    } catch (e: any) {
      toast.error('Pin failed', e.message)
    }
  }

  const removePin = async (id: string) => {
    await api.context.removePin(id)
    setPins((prev) => prev.filter((p) => p.id !== id))
  }

  const saveMemory = async () => {
    if (!activeSessions) return
    setSaving(true)
    try {
      await api.context.saveMemory({ workspace_path: activeSessions, content: memory })
      setSavedMemory(memory); setSaveDone(true)
      setTimeout(() => setSaveDone(false), 2500)
      toast.success('Project memory saved')
    } catch (e: any) {
      toast.error('Save failed', e.message)
    }
    setSaving(false)
  }

  return (
    <div className="flex flex-col h-full">
      {/* Tabs */}
      <div className="flex border-b border-border">
        {(['pins', 'memory'] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 py-2 text-[11px] font-semibold uppercase tracking-wide border-b-2 transition-colors ${
              tab === t ? 'border-accent text-ink' : 'border-transparent text-faint hover:text-muted'
            }`}
          >
            {t === 'pins' ? '📌 Pinned' : '🧠 Memory'}
          </button>
        ))}
      </div>

      {tab === 'pins' ? (
        <div className="flex-1 overflow-y-auto">
          {activeFile && (
            <div className="p-2 border-b border-border">
              <button
                onClick={pinCurrentFile}
                className="w-full flex items-center justify-center gap-2 py-1.5 rounded-lg bg-accent/10 border border-accent/30 text-accent text-xs font-semibold hover:bg-accent/20 transition-colors"
              >
                <Pin size={11} /> Pin current file
              </button>
            </div>
          )}
          {pins.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-32 gap-2 px-4 text-center">
              <Pin size={22} className="text-faint opacity-40" />
              <span className="text-xs text-faint leading-relaxed">
                No pinned files.<br />Pin files to keep them always in context.
              </span>
            </div>
          ) : (
            pins.map((pin) => (
              <div key={pin.id} className="flex items-center gap-2 px-3 py-2 border-b border-border/50 group">
                <Pin size={11} className="text-accent flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-ink truncate">
                    {pin.label || pin.file_path.split('/').pop()}
                  </div>
                  {pin.symbol_path && (
                    <div className="text-[10px] text-faint">→ {pin.symbol_path}</div>
                  )}
                </div>
                <button
                  onClick={() => removePin(pin.id)}
                  className="btn-icon w-5 h-5 opacity-0 group-hover:opacity-100 hover:text-danger"
                >
                  <Trash2 size={11} />
                </button>
              </div>
            ))
          )}
        </div>
      ) : (
        <div className="flex-1 flex flex-col p-3 gap-2">
          <div className="text-[11px] text-muted leading-relaxed">
            Project conventions injected into every prompt. Use markdown.
          </div>
          <textarea
            value={memory}
            onChange={(e) => setMemory(e.target.value)}
            placeholder={`# Project Conventions\n\n- Use snake_case for variables\n- All errors go through AppError\n- Prefer async/await over callbacks\n- Tests go in /tests/ directory`}
            className="flex-1 input resize-none text-xs font-mono leading-relaxed min-h-[180px]"
            style={{ fontFamily: 'JetBrains Mono, Fira Code, Consolas, monospace' }}
          />
          <button
            onClick={saveMemory}
            disabled={saving || memory === savedMemory}
            className={`flex items-center justify-center gap-1.5 py-2 rounded-lg text-xs font-bold transition-all ${
              saveDone
                ? 'bg-success/15 text-success border border-success/30'
                : memory === savedMemory
                  ? 'bg-border text-faint cursor-not-allowed'
                  : 'bg-accent text-base hover:bg-accent/90'
            }`}
          >
            {saveDone ? <><Check size={12} /> Saved!</> : <><Save size={12} /> Save Memory</>}
          </button>
        </div>
      )}
    </div>
  )
}
