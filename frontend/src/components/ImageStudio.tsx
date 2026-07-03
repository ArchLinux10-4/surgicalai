import React, { useEffect, useRef, useState } from 'react'
import { generateImage, type ImageStudioResult } from '../api/client'

/**
 * Image Studio — GPT-powered image generation & editing.
 *
 * Fully self-contained: renders its own floating trigger button and modal.
 * Mounted with a single <ImageStudio /> line in Layout.tsx.
 *
 * Flow: prompt (+ optional uploaded image) -> POST /api/images/generate
 * -> backend calls OpenAI Responses API image_generation tool -> base64 back.
 */

const MAX_UPLOAD_BYTES = 20 * 1024 * 1024 // must match backend cap
const ALLOWED_MIMES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']

export function ImageStudio() {
  const [open, setOpen] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [inputImage, setInputImage] = useState<{ base64: string; mime: string; name: string } | null>(null)
  const [result, setResult] = useState<{ base64: string; mime: string; text: string } | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [quality, setQuality] = useState('auto') // auto | low | medium | high
  const fileRef = useRef<HTMLInputElement>(null)

  // Esc closes the modal (never mid-generation).
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, busy])

  // Elapsed-seconds ticker while generating (image calls can take 30-120s).
  useEffect(() => {
    if (!busy) return
    setElapsed(0)
    const t = setInterval(() => setElapsed(e => e + 1), 1000)
    return () => clearInterval(t)
  }, [busy])

  const onFile = (file: File | undefined) => {
    setError('')
    if (!file) return
    if (!ALLOWED_MIMES.includes(file.type)) {
      setError(`Unsupported file type "${file.type || 'unknown'}" — use PNG, JPEG, WebP, or GIF.`)
      return
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(`Image is ${Math.round(file.size / 1024 / 1024)} MB — max is 20 MB.`)
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = String(reader.result || '')
      const base64 = dataUrl.split(',')[1] || ''
      if (!base64) {
        setError('Could not read the image file.')
        return
      }
      setInputImage({ base64, mime: file.type, name: file.name })
    }
    reader.onerror = () => setError('Could not read the image file.')
    reader.readAsDataURL(file)
  }

  const run = async () => {
    if (!prompt.trim() || busy) return
    setBusy(true)
    setError('')
    setResult(null)
    try {
      const res: ImageStudioResult = await generateImage({
        prompt: prompt.trim(),
        image_base64: inputImage?.base64,
        image_mime: inputImage?.mime,
        // Omit "auto" so the default request stays byte-identical to before.
        quality: quality !== 'auto' ? quality : undefined,
      })
      if (res.ok && res.image_base64) {
        setResult({ base64: res.image_base64, mime: res.image_mime || 'image/png', text: res.text || '' })
      } else {
        setError(res.detail || 'Generation failed for an unknown reason.')
      }
    } catch (e: any) {
      setError(e?.message || 'Request failed.')
    } finally {
      setBusy(false)
    }
  }

  // Reset everything back to a blank studio (safe: blocked while a generation is running).
  const reset = () => {
    if (busy) return
    setPrompt('')
    setInputImage(null)
    setResult(null)
    setError('')
    setQuality('auto')
    // File inputs keep their last value even after state clears — wipe it so
    // re-uploading the same file still fires onChange.
    if (fileRef.current) fileRef.current.value = ''
  }

  const download = () => {
    if (!result) return
    const ext = (result.mime.split('/')[1] || 'png').replace('jpeg', 'jpg')
    const a = document.createElement('a')
    a.href = `data:${result.mime};base64,${result.base64}`
    a.download = `image-studio-${Date.now()}.${ext}`
    a.click()
  }

  return (
    <>
      {/* Floating trigger button — bottom-right, out of the way of chat */}
      <button
        onClick={() => setOpen(true)}
        title="Image Studio (GPT image generation & editing)"
        className="fixed bottom-4 right-4 z-40 flex items-center gap-2 px-3 py-2 rounded-full
          bg-surface border border-border text-sm text-muted hover:text-fg hover:border-accent/60
          shadow-lg transition-colors"
      >
        🎨 <span className="hidden sm:inline">Image Studio</span>
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => !busy && setOpen(false)}>
          <div
            className="w-[1100px] max-w-[95vw] h-[85vh] max-h-[900px] rounded-xl bg-surface border border-border shadow-2xl flex flex-col overflow-hidden"
            onClick={e => e.stopPropagation()}
          >
            {/* Header bar */}
            <div className="flex items-center justify-between px-5 py-3 border-b border-border shrink-0">
              <h2 className="text-base font-semibold">🎨 Image Studio</h2>
              <div className="flex items-center gap-4">
                <button
                  onClick={reset}
                  disabled={busy}
                  className="text-xs text-muted hover:text-fg disabled:opacity-50"
                  title="Clear everything and start a new image"
                >
                  ↺ Reset
                </button>
                <button onClick={() => !busy && setOpen(false)} className="text-muted hover:text-fg text-lg leading-none" title="Close (Esc)">✕</button>
              </div>
            </div>

            {/* Body: controls sidebar + preview canvas. Stacks on small screens. */}
            <div className="flex-1 flex flex-col md:flex-row min-h-0">

              {/* Left: controls */}
              <div className="w-full md:w-[360px] md:border-r border-b md:border-b-0 border-border p-5 flex flex-col gap-4 overflow-y-auto shrink-0">
                <div className="flex flex-col gap-1.5 flex-1 min-h-[140px]">
                  <label className="text-xs font-medium text-muted uppercase tracking-wide">Prompt</label>
                  <textarea
                    value={prompt}
                    onChange={e => setPrompt(e.target.value)}
                    placeholder={inputImage
                      ? 'Describe how to edit the uploaded image…'
                      : 'Describe the image to generate — or upload one to edit…'}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                        e.preventDefault()
                        run()
                      }
                    }}
                    className="w-full flex-1 min-h-[120px] rounded-lg border border-border bg-base p-3 text-sm resize-none focus:outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30"
                  />
                  <span className="text-[11px] text-muted">Ctrl+Enter to generate · Esc to close</span>
                </div>

                <div className="flex flex-col gap-1.5">
                  <label className="text-xs font-medium text-muted uppercase tracking-wide">Quality</label>
                  <select
                    value={quality}
                    onChange={e => setQuality(e.target.value)}
                    disabled={busy}
                    className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm disabled:opacity-50 focus:outline-none focus:border-accent/60"
                  >
                    <option value="auto">Auto (model decides)</option>
                    <option value="low">Low — fastest, cheapest</option>
                    <option value="medium">Medium</option>
                    <option value="high">High — best, priciest</option>
                  </select>
                </div>

                <div className="flex flex-col gap-1.5">
                  <label className="text-xs font-medium text-muted uppercase tracking-wide">Source image (optional)</label>
                  <input
                    ref={fileRef}
                    type="file"
                    accept={ALLOWED_MIMES.join(',')}
                    className="hidden"
                    onChange={e => onFile(e.target.files?.[0])}
                  />
                  <button
                    onClick={() => fileRef.current?.click()}
                    disabled={busy}
                    className="w-full px-3 py-2 rounded-lg border border-dashed border-border text-sm text-muted hover:text-fg hover:border-accent/60 disabled:opacity-50 transition-colors"
                  >
                    {inputImage ? 'Replace image' : '＋ Upload image to edit'}
                  </button>
                  {inputImage && (
                    <div className="flex items-center gap-2 text-xs text-muted rounded-lg border border-border p-2">
                      <img
                        src={`data:${inputImage.mime};base64,${inputImage.base64}`}
                        alt="input"
                        className="h-10 w-10 object-cover rounded border border-border"
                      />
                      <span className="flex-1 truncate">{inputImage.name}</span>
                      <button onClick={() => setInputImage(null)} disabled={busy} className="text-muted hover:text-fg" title="Remove">✕</button>
                    </div>
                  )}
                </div>

                <button
                  onClick={run}
                  disabled={busy || !prompt.trim()}
                  className="w-full px-4 py-2.5 rounded-lg bg-accent text-white text-sm font-medium disabled:opacity-50 hover:opacity-90 transition-opacity"
                >
                  {busy
                    ? `Generating… ${elapsed}s`
                    : inputImage ? 'Edit image' : 'Generate image'}
                </button>
                {busy && (
                  <p className="text-[11px] text-muted text-center -mt-2">Image models can take 1–2 minutes</p>
                )}

                {error && (
                  <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300 whitespace-pre-wrap break-words">
                    {error}
                  </div>
                )}
              </div>

              {/* Right: preview canvas */}
              <div className="flex-1 min-h-[280px] min-w-0 p-5 overflow-y-auto flex flex-col gap-3 bg-base/40">
                {result ? (
                  <>
                    <div className="flex-1 min-h-0 flex items-center justify-center">
                      <img
                        src={`data:${result.mime};base64,${result.base64}`}
                        alt="result"
                        className="max-w-full max-h-full object-contain rounded-lg border border-border shadow-lg"
                      />
                    </div>
                    {result.text && (
                      <p className="text-sm text-muted whitespace-pre-wrap break-words leading-relaxed shrink-0 max-h-[30%] overflow-y-auto">
                        {result.text}
                      </p>
                    )}
                    <button
                      onClick={download}
                      className="self-start px-4 py-2 rounded-lg border border-border text-sm text-muted hover:text-fg hover:border-accent/60 shrink-0 transition-colors"
                    >
                      ⬇ Download
                    </button>
                  </>
                ) : (
                  <div className="flex-1 flex flex-col items-center justify-center text-muted gap-2 select-none">
                    <span className="text-5xl opacity-40">🖼️</span>
                    <p className="text-sm">
                      {busy ? `Generating… ${elapsed}s` : 'Your image will appear here'}
                    </p>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
