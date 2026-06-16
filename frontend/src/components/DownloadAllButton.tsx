import React, { useState, useCallback } from 'react'
import JSZip from 'jszip'
import { FolderZip } from '@mui/icons-material'

/* ─── types ─────────────────────────────────────────────────────────────── */

interface DownloadFile {
  filename: string
  content: string
}

interface DownloadAllButtonProps {
  /** Files to include in the zip — filename paths are preserved as folder structure */
  files: DownloadFile[]
  /** Custom zip filename (default: auto-detected from common folder prefix) */
  zipName?: string
}

type ZipStage = 'idle' | 'compressing' | 'downloading'

/* ─── helpers ───────────────────────────────────────────────────────────── */

/** Detect a common folder prefix to name the zip nicely.
 *  e.g. all files start with "calorie_tracker/" → "calorie_tracker.zip"
 *  Otherwise falls back to "session-files.zip" */
function deriveZipName(files: DownloadFile[]): string {
  if (files.length === 0) return 'session-files.zip'
  const parts = files[0].filename.split('/')
  if (parts.length < 2) return 'session-files.zip'
  const prefix = parts[0]
  const allMatch = files.every(f => f.filename.startsWith(prefix + '/'))
  return allMatch ? `${prefix}.zip` : 'session-files.zip'
}

/* ─── component ─────────────────────────────────────────────────────────── */

export function DownloadAllButton({ files, zipName }: DownloadAllButtonProps) {
  const [stage, setStage] = useState<ZipStage>('idle')
  const [progress, setProgress] = useState(0)

  const handleDownloadAll = useCallback(async () => {
    if (stage !== 'idle' || files.length === 0) return

    setStage('compressing')
    setProgress(0)

    try {
      const zip = new JSZip()

      // Add each file — JSZip automatically creates nested folders from paths
      // e.g. "src/models/db.py" → src/ → models/ → db.py
      for (const file of files) {
        zip.file(file.filename, file.content)
      }

      // Generate with progress callback — JSZip reports 0–100 percent
      const blob = await zip.generateAsync(
        { type: 'blob' },
        (meta) => {
          // meta.percent is 0–100, meta.currentFile is the file being processed
          setProgress(Math.round(meta.percent))
        }
      )

      // Compression done — now trigger browser download
      setStage('downloading')
      setProgress(100)

      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = zipName || deriveZipName(files)
      a.click()
      URL.revokeObjectURL(url)

      // Brief pause so user sees "Downloading…" before reset
      await new Promise(r => setTimeout(r, 800))
    } catch (e) {
      console.error('[DownloadAll] Failed to create zip:', e)
    } finally {
      setStage('idle')
      setProgress(0)
    }
  }, [files, zipName, stage])

  // Only show when there are 2+ files
  if (files.length < 2) return null

  /* ─── label ─────────────────────────────────────────────────────────── */
  let label = `Download All (${files.length})`
  if (stage === 'compressing') label = `Compressing… ${progress}%`
  if (stage === 'downloading') label = 'Downloading…'

  const busy = stage !== 'idle'

  return (
    <button
      onClick={handleDownloadAll}
      disabled={busy}
      title={
        busy
          ? label
          : `Download all ${files.length} files as a zip (folder structure preserved)`
      }
      className={`
        flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium
        border border-border/60 transition-all min-w-[140px] justify-center
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
