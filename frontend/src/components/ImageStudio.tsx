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
  const fileRef = useRef<HTMLInputElement>(null)

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
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => !busy && setOpen(false)}>
          <div
            className="w-[560px] max-w-[92vw] max-h-[88vh] overflow-y-auto rounded-lg bg-surface border border-border p-5 flex flex-col gap-4"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold">🎨 Image Studio</h2>
              <button onClick={() => !busy && setOpen(false)} className="text-muted hover:text-fg" title="Close">✕</button>
            </div>

            <textarea
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              placeholder={inputImage
                ? 'Describe how to edit the uploaded image…'
                : 'Describe the image to generate — or upload one to edit…'}
              rows={3}
              className="w-full rounded border border-border bg-base p-2 text-sm resize-y focus:outline-none focus:border-accent/60"
            />

            <div className="flex items-center gap-3">
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
                className="px-3 py-1.5 rounded border border-border text-sm text-muted hover:text-fg disabled:opacity-50"
              >
                {inputImage ? 'Replace image' : 'Upload image to edit'}
              </button>
              {inputImage && (
                <span className="flex items-center gap-2 text-xs text-muted">
                  <img
                    src={`data:${inputImage.mime};base64,${inputImage.base64}`}
                    alt="input"
                    className="h-8 w-8 object-cover rounded border border-border"
                  />
                  {inputImage.name}
                  <button onClick={() => setInputImage(null)} disabled={busy} className="text-muted hover:text-fg" title="Remove">✕</button>
                </span>
              )}
            </div>

            <button
              onClick={run}
              disabled={busy || !prompt.trim()}
              className="px-4 py-2 rounded bg-accent text-white text-sm font-medium disabled:opacity-50"
            >
              {busy
                ? `Generating… ${elapsed}s (image models can take 1-2 minutes)`
                : inputImage ? 'Edit image' : 'Generate image'}
            </button>

            {error && (
              <div className="rounded border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300 whitespace-pre-wrap">
                {error}
              </div>
            )}

            {result && (
              <div className="flex flex-col gap-2">
                <img
                  src={`data:${result.mime};base64,${result.base64}`}
                  alt="result"
                  className="w-full rounded border border-border"
                />
                {result.text && <p className="text-xs text-muted whitespace-pre-wrap">{result.text}</p>}
                <button
                  onClick={download}
                  className="self-start px-3 py-1.5 rounded border border-border text-sm text-muted hover:text-fg"
                >
                  ⬇ Download
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  )
}
