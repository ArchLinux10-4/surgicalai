import React, { useState, useEffect } from 'react'
import { api } from '../api/client'
import { toast } from '../lib/toast'
import type { MemoryPreset } from '../types'
import { Check, Close, Save } from '@mui/icons-material';

export function ContextPanel() {
  const [memory, setMemory] = useState('')
  const [savedMemory, setSavedMemory] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveDone, setSaveDone] = useState(false)
  const [presets, setPresets] = useState<MemoryPreset[]>([])

  // Global memory + the shared preset library load once — they are team-wide,
  // not tied to any single chat session.
  useEffect(() => {
    api.context.getGlobalMemory().then((m) => {
      setMemory(m.content); setSavedMemory(m.content)
    }).catch(() => {})
    api.context.getMemoryPresets().then(setPresets).catch(() => {})
  }, [])

  const saveMemory = async () => {
    setSaving(true)
    try {
      await api.context.saveGlobalMemory(memory)
      setSavedMemory(memory); setSaveDone(true)
      setTimeout(() => setSaveDone(false), 2500)
      toast.success('Global memory saved')
    } catch (e: any) {
      toast.error('Save failed', e.message)
    }
    setSaving(false)
  }

  // A preset counts as "added" when its (unedited) block is present in memory.
  // Deriving from the text — rather than a separate flag — keeps the chips correct
  // after a page reload and lets the same chip remove what it added.
  const isPresetAdded = (preset: MemoryPreset) => memory.includes(preset.content.trim())

  const togglePreset = (preset: MemoryPreset) => {
    const block = preset.content.trim()
    setMemory((prev) => {
      if (prev.includes(block)) {
        // Remove the preset block and collapse any orphaned blank lines.
        const next = prev.replace(block, '').replace(/\n{3,}/g, '\n\n').trim()
        return next ? `${next}\n` : ''
      }
      const trimmed = prev.trimEnd()
      return trimmed ? `${trimmed}\n\n${block}\n` : `${block}\n`
    })
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 flex flex-col p-3 gap-2">
        <div className="text-[11px] text-muted leading-relaxed">
          Global conventions — injected into <span className="text-ink font-semibold">every prompt, in every chat</span>, shared by all developers. Use markdown.
        </div>
        {presets.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <div className="text-[10px] uppercase tracking-wide text-faint font-semibold">Presets — click to add, click again to remove</div>
            <div className="flex flex-wrap gap-1.5">
              {presets.map((p) => {
                const added = isPresetAdded(p)
                return (
                  <button
                    key={p.id}
                    onClick={() => togglePreset(p)}
                    title={added ? `Remove "${p.title}" from memory` : p.description}
                    className={`group/chip flex items-center gap-1 px-2 py-1 rounded-md border text-[11px] font-medium transition-colors ${
                      added
                        ? 'bg-success/15 border-success/30 text-success hover:bg-danger/15 hover:border-danger/30 hover:text-danger'
                        : 'bg-overlay border-border text-muted hover:text-ink hover:border-accent/40'
                    }`}
                  >
                    <span>{p.icon}</span>
                    <span>{p.title}</span>
                    {added && (
                      <>
                        <Check sx={{ fontSize: 11 }} className="group-hover/chip:hidden" />
                        <Close sx={{ fontSize: 11 }} className="hidden group-hover/chip:inline-flex" />
                      </>
                    )}
                  </button>
                )
              })}
            </div>
          </div>
        )}
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
          {saveDone ? <><Check sx={{ fontSize: 12 }} /> Saved!</> : <><Save sx={{ fontSize: 12 }} /> Save Memory</>}
        </button>
      </div>
    </div>
  )
}
