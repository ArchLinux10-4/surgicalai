import React, { useState, useCallback } from 'react'
import JSZip from 'jszip'
import { FolderZip } from '@mui/icons-material'
import { api } from '../api/client'
import type { SessionFile } from '../types'

/* ─── types ─────────────────────────────────────────────────────────────── */

interface Props {
  sessionId: string
  sessionFiles: SessionFile[]
}

type ZipStage = 'idle' | 'fetching' | 'compressing' | 'downloading'

/* ─── helpers ───────────────────────────────────────────────────────────── */

/** Detect a common folder prefix to name the zip nicely.
 *  e.g. all files start with "calorie_tracker/" → "calorie_tracker.zip"
 *  Otherwise falls back to "session-files.zip" */
function deriveZipName(files: { filename: string }[]): string {
  if (files.length === 0) return 'session-files.zip'
  const parts = files[0].filename.split('/')
  if (parts.length < 2) return 'session-files.zip'
  const prefix = parts[0]
  const allMatch = files.every(f => f.filename.startsWith(prefix + '/'))
  return allMatch ? `${prefix}.zip` : 'session-files.zip'
}

/* ─── component ─────────────────────────────────────────────────────────── */

export function DownloadSessionButton({ sessionId, sessionFiles }: Props) {
  const [stage, setStage] = useState<ZipStage>('idle')
  const [progress, setProgress] = useState('')

  const handleDownload = useCallback(async () => {
    if (stage !== 'idle' || sessionFiles.length < 2) return

    setStage('fetching')

    try {
      // Phase 1: Fetch content for each file from the session API
      const fileContents: { filename: string; content: string }[] = []

      for (let i = 0; i < sessionFiles.length; i++) {
        const f = sessionFiles[i]
        setProgress(`Fetching ${i + 1}/${sessionFiles.length}…`)
        try {
          const data = await api.sessionFiles.get(sessionId, f.id)
          if (data?.content) {
            fileContents.push({ filename: f.filename, content: data.content })
          }
        } catch {
          // Skip files we can't fetch — don't block the whole zip
          console.warn(`[DownloadSession] Skipped ${f.filename} — fetch failed`)
        }
      }

      if (fileContents.length === 0) {
        console.warn('[DownloadSession] No file contents fetched — aborting')
        return
      }

      // Phase 2: Compress with JSZip — preserves folder structure automatically
      setStage('compressing')
      setProgress('Compressing… 0%')

      const zip = new JSZip()
      for (const file of fileContents) {
        // JSZip auto-creates nested folders from paths
        // e.g. "src/models/db.py" → src/ → models/ → db.py
        zip.file(file.filename, file.content)
      }

      const blob = await zip.generateAsync(
        { type: 'blob' },
        (meta) => setProgress(`Compressing… ${Math.round(meta.percent)}%`)
      )

      // Phase 3: Trigger browser download
      setStage('downloading')
      setProgress('Downloading…')

      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = deriveZipName(fileContents)
      a.click()
      URL.revokeObjectURL(url)

      // Brief pause so user sees "Downloading…" before reset
      await new Promise(r => setTimeout(r, 800))
    } catch (e) {
      console.error('[DownloadSession] Failed to create zip:', e)
    } finally {
      setStage('idle')
      setProgress('')
    }
  }, [sessionId, sessionFiles, stage])

  // Only show when there are 2+ session files
  if (sessionFiles.length < 2) return null

  /* ─── label ─────────────────────────────────────────────────────────── */
  const busy = stage !== 'idle'
  const label = busy ? progress : `Download All (${sessionFiles.length})`

  return (
    <button
      onClick={handleDownload}
      disabled={busy}
      title={
        busy
          ? label
          : `Download all ${sessionFiles.length} session files as a zip (folder structure preserved)`
      }
      className={`
        flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium
        border border-border/60 transition-all
        ${busy
          ? 'text-muted cursor-wait bg-surface/40'
          : 'text-muted hover:text-ink hover:bg-surface/80 active:scale-[0.97]'
        }
      `}
    >
      <FolderZip sx={{ fontSize: 14 }} />
      <span>{label}</span>
    </button>
  )
}
