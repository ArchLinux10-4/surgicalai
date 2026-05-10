import React, { useEffect, useRef } from 'react'
import { X, FileCode, Trash2, Upload } from 'lucide-react'
import { useAppStore } from '../../stores/appStore'
import { api } from '../../api/client'
import { toast } from '../../lib/toast'

const FILE_ICONS: Record<string, string> = {
  '.tsx': '⚛️', '.ts': '📘', '.jsx': '⚛️', '.js': '📜',
  '.py': '🐍', '.go': '🦫', '.rs': '🦀', '.html': '🌐',
  '.css': '🎨', '.json': '📋', '.md': '📝', '.sh': '⚙️',
  '.sql': '🗃', '.yaml': '📄', '.yml': '📄', '.txt': '📄',
}

function fileIcon(name: string): string {
  const ext = '.' + (name.split('.').pop()?.toLowerCase() || '')
  return FILE_ICONS[ext] || '📄'
}

interface Props {
  open: boolean
  onClose: () => void
}

export function MobileFilesSheet({ open, onClose }: Props) {
  const { sessionFiles, removeSessionFile, addSessionFile, activeSessions } = useAppStore()
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden'
      return () => { document.body.style.overflow = '' }
    }
  }, [open])

  const handleUpload = async (files: FileList | null) => {
    if (!files || !activeSessions) return
    for (const file of Array.from(files)) {
      try {
        const content = await file.text()
        const lineCount = content.split('\n').length
        const uploaded = await api.sessionFiles.upload(activeSessions, {
          filename: file.name,
          content,
          language: file.name.split('.').pop() || 'text',
        })
        addSessionFile({
          id: uploaded.id,
          session_id: activeSessions,
          filename: file.name,
          language: file.name.split('.').pop() || 'text',
          lines: lineCount,
          symbol_count: 0,
          created_at: new Date().toISOString(),
          content,
        })
      } catch (err: any) {
        toast.error(`Failed to upload ${file.name}`)
      }
    }
  }

  const removeFile = async (id: string) => {
    if (!activeSessions) return
    try {
      await api.sessionFiles.delete(activeSessions, id)
    } catch {}
    removeSessionFile(id)
  }

  return (
    <>
      <div
        className={`fixed inset-0 z-40 bg-black/50 transition-opacity duration-200 ${
          open ? 'opacity-100' : 'opacity-0 pointer-events-none'
        }`}
        onClick={onClose}
        aria-hidden={!open}
      />
      <div
        className={`fixed left-0 right-0 bottom-0 z-50 bg-base border-t border-border rounded-t-2xl shadow-2xl transition-transform duration-250 ease-out flex flex-col max-h-[85vh] ${
          open ? 'translate-y-0' : 'translate-y-full'
        }`}
        style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
        aria-hidden={!open}
      >
        {/* Drag handle */}
        <div className="flex justify-center pt-2 pb-1 flex-shrink-0">
          <div className="w-10 h-1 rounded-full bg-border" />
        </div>

        {/* Header */}
        <div className="flex items-center justify-between px-4 pt-2 pb-3 border-b border-border flex-shrink-0">
          <div>
            <h2 className="text-[15px] font-semibold text-ink">Session Files</h2>
            <p className="text-[11px] text-muted">
              {sessionFiles.length === 0
                ? 'No files in this chat'
                : `${sessionFiles.length} file${sessionFiles.length !== 1 ? 's' : ''}`}
            </p>
          </div>
          <button
            onClick={onClose}
            className="w-9 h-9 flex items-center justify-center rounded-lg text-muted hover:text-ink hover:bg-overlay"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* Add files button */}
        <div className="px-3 pt-3 pb-2 flex-shrink-0">
          <button
            onClick={() => fileInputRef.current?.click()}
            className="w-full flex items-center justify-center gap-2 h-11 rounded-xl bg-accent/10 text-accent border border-accent/20 font-semibold text-[14px] active:bg-accent/20 transition-colors"
          >
            <Upload size={16} />
            Upload files
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".py,.js,.ts,.tsx,.jsx,.go,.rs,.java,.cs,.cpp,.c,.h,.html,.css,.scss,.json,.md,.sh,.sql,.yaml,.yml,.toml,.txt,.env,.rb,.php,.swift,.kt"
            className="hidden"
            onChange={(e) => handleUpload(e.target.files)}
          />
        </div>

        {/* File list */}
        <div className="flex-1 overflow-y-auto overscroll-contain px-2 pb-2">
          {sessionFiles.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-center text-muted gap-3">
              <div className="w-14 h-14 rounded-2xl bg-surface flex items-center justify-center">
                <FileCode size={26} className="text-muted/60" />
              </div>
              <div>
                <p className="text-[14px] font-semibold">Drop files into chat</p>
                <p className="text-[12px] text-faint mt-1">Or tap Upload above</p>
              </div>
            </div>
          ) : (
            <ul className="py-1 space-y-1">
              {sessionFiles.map(f => (
                <li
                  key={f.id}
                  className="flex items-center gap-3 px-3 py-2.5 rounded-xl bg-surface/60 active:bg-surface"
                >
                  <span className="text-[20px] flex-shrink-0">{fileIcon(f.filename)}</span>
                  <div className="flex-1 min-w-0">
                    <p className="text-[14px] font-medium text-ink truncate">{f.filename}</p>
                    {f.lines > 0 && (
                      <p className="text-[11px] text-muted">
                        {f.lines} lines{f.symbol_count > 0 ? ` · ${f.symbol_count} symbols` : ''}
                      </p>
                    )}
                  </div>
                  <button
                    onClick={() => removeFile(f.id)}
                    className="w-9 h-9 flex items-center justify-center rounded-lg text-muted hover:text-danger hover:bg-overlay -mr-1"
                    aria-label={`Remove ${f.filename}`}
                  >
                    <Trash2 size={15} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </>
  )
}
