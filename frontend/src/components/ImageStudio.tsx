import React, { useEffect, useRef, useState } from 'react'
import { generateImage, type ImageStudioResult } from '../api/client'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { AddPhotoAlternateOutlined, AutoAwesome, Close, FileDownload, ImageOutlined, RestartAlt, WarningAmber } from '@mui/icons-material'

/**
 * Image Studio — GPT-powered image generation & editing with multi-turn sessions.
 *
 * Mounted with a single <ImageStudio /> line in Layout.tsx (stays mounted so
 * the session survives close/reopen). Opened via the Image Studio icon in the
 * sidebar rail — visibility lives in the app store (imageStudioOpen).
 *
 * Flow: prompt (+ optional uploaded image) -> POST /api/images/generate
 * -> backend calls OpenAI Responses API image_generation tool -> base64 back.
 *
 * Multi-turn editing (docs: Responses API "Multi-turn editing"):
 * every result carries a response_id; follow-up prompts send it back as
 * previous_response_id so the model edits its own previous output. Each
 * turn is kept as a version — click any version to view it or branch a
 * new edit from that point.
 */

const MAX_UPLOAD_BYTES = 20 * 1024 * 1024 // must match backend cap
const MAX_INPUT_IMAGES = 5 // up to 5 reference images per generation
const ALLOWED_MIMES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']

const SIZE_OPTIONS = [
  { value: 'auto', label: 'Auto' },
  { value: '1024x1024', label: 'Square (1024×1024)' },
  { value: '1536x1024', label: 'Landscape (1536×1024)' },
  { value: '1024x1536', label: 'Portrait (1024×1536)' },
  { value: '2048x2048', label: 'Large Square (2048×2048)' },
] as const

const FORMAT_OPTIONS = [
  { value: 'png', label: 'PNG (lossless)' },
  { value: 'jpeg', label: 'JPEG (faster)' },
  { value: 'webp', label: 'WebP (smaller)' },
] as const

/** One completed generation/edit turn in the session. */
interface Turn {
  prompt: string      // what the user asked for on this turn
  base64: string      // resulting image
  mime: string        // resulting image mime
  text: string        // any accompanying model text
  responseId: string  // OpenAI response id — chain target for follow-ups
}

export function ImageStudio() {
  const open = useAppStore(s => s.imageStudioOpen)
  const setOpen = useAppStore(s => s.setImageStudioOpen)
  const [prompt, setPrompt] = useState('')
  const [inputImages, setInputImages] = useState<{ base64: string; mime: string; name: string }[]>([])
  const [turns, setTurns] = useState<Turn[]>([])
  const [activeIdx, setActiveIdx] = useState(0) // version shown on canvas + chain target
  const [error, setError] = useState('')
  const [isRefusal, setIsRefusal] = useState(false) // true => render "no image" warning style
  const [busy, setBusy] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [quality, setQuality] = useState('auto') // auto | low | medium | high
  const [size, setSize] = useState('auto') // auto | 1024x1024 | 1536x1024 | 1024x1536
  const [outputFormat, setOutputFormat] = useState('png') // png | jpeg | webp
  const [model, setModel] = useState('gpt-5.5') // gpt-5.5 | gpt-5.6-sol | gpt-5.6-terra | gpt-5.6-luna
  const isAdmin = useAuthStore(s => s.user?.is_admin ?? false)
  const [dragging, setDragging] = useState(false) // drag-over visual feedback
  const [versionTipDismissed, setVersionTipDismissed] = useState(false) // one-time branching tip
  const fileRef = useRef<HTMLInputElement>(null)
  const threadRef = useRef<HTMLDivElement>(null)

  const active: Turn | null = turns[activeIdx] ?? null
  const hasSession = turns.length > 0

  // Esc closes the modal (never mid-generation; confirm if a session is active).
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) tryClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, busy, turns.length])

  // Elapsed-seconds ticker while generating (image calls can take 30-120s).
  useEffect(() => {
    if (!busy) return
    setElapsed(0)
    const t = setInterval(() => setElapsed(e => e + 1), 1000)
    return () => clearInterval(t)
  }, [busy])

  // Keep the edit thread scrolled to the latest turn.
  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight })
  }, [turns.length, busy])

  // Closing only hides the window — the session (versions, thread, edit chain)
  // stays in memory and is restored on reopen. "New session" is the only
  // destructive action.
  const tryClose = () => {
    if (busy) return
    setOpen(false)
  }

  const onFiles = (files: FileList | null) => {
    setError('')
    if (!files || files.length === 0) return
    const remaining = MAX_INPUT_IMAGES - inputImages.length
    if (remaining <= 0) {
      setError(`Maximum ${MAX_INPUT_IMAGES} images allowed.`)
      return
    }
    const toAdd = Array.from(files).slice(0, remaining)
    if (files.length > remaining) {
      setError(`Only ${remaining} more image${remaining === 1 ? '' : 's'} allowed — extra files skipped.`)
    }
    for (const file of toAdd) {
      if (!ALLOWED_MIMES.includes(file.type)) {
        setError(`"${file.name}" — unsupported type. Use PNG, JPEG, WebP, or GIF.`)
        return
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        setError(`"${file.name}" is ${Math.round(file.size / 1024 / 1024)} MB — max is 20 MB.`)
        return
      }
    }
    // Read all valid files
    for (const file of toAdd) {
      const reader = new FileReader()
      reader.onload = () => {
        const dataUrl = String(reader.result || '')
        const b64 = dataUrl.split(',')[1] || ''
        if (!b64) return
        setInputImages(prev => {
          if (prev.length >= MAX_INPUT_IMAGES) return prev
          return [...prev, { base64: b64, mime: file.type, name: file.name }]
        })
      }
      reader.onerror = () => setError(`Could not read "${file.name}".`)
      reader.readAsDataURL(file)
    }
  }

  const removeImage = (idx: number) => {
    setInputImages(prev => prev.filter((_, i) => i !== idx))
  }

  /** Runs one turn: fresh generation when no session, chained edit otherwise. */
  const run = async () => {
    if (!prompt.trim() || busy) return
    // Chained edits need the active version's response id. It should always
    // exist; if it somehow doesn't, fail loud rather than generating an
    // unrelated image.
    if (hasSession && !active?.responseId) {
      setIsRefusal(false)
      setError('This version cannot be edited further — start a new session.')
      return
    }
    setBusy(true)
    setError('')
    setIsRefusal(false)
    try {
      const res: ImageStudioResult = await generateImage({
        prompt: prompt.trim(),
        model,
        // First turn only: optional uploaded source images. Follow-up turns
        // inherit image context through previous_response_id chaining.
        images: !hasSession && inputImages.length > 0
          ? inputImages.map(img => ({ base64: img.base64, mime: img.mime }))
          : undefined,
        // Omit defaults so the request stays byte-identical to before when unchanged.
        quality: quality !== 'auto' ? quality : undefined,
        size: size !== 'auto' ? size : undefined,
        output_format: outputFormat !== 'png' ? outputFormat : undefined,
        previous_response_id: hasSession ? active!.responseId : undefined,
      })
      if (res.ok && res.image_base64) {
        const turn: Turn = {
          prompt: prompt.trim(),
          base64: res.image_base64,
          mime: res.image_mime || 'image/png',
          text: res.text || '',
          responseId: res.response_id || '',
        }
        // Branching: editing from an older version discards nothing — the new
        // turn is simply appended and becomes the active version. Both states
        // derive from the same snapshot so they can never disagree ("busy"
        // already blocks concurrent mutations; this makes it airtight anyway).
        const next = [...turns, turn]
        setTurns(next)
        setActiveIdx(next.length - 1)
        setPrompt('')
      } else {
        // tool_choice forces the image tool server-side, so a text-only reply
        // means the model refused (e.g. content policy). Label it clearly —
        // the session (if any) survives so the user can just rephrase.
        setIsRefusal(res.error_code === 'no_image_text_response')
        setError(res.detail || 'Generation failed for an unknown reason.')
      }
    } catch (e: any) {
      setIsRefusal(false)
      setError(e?.message || 'Request failed.')
    } finally {
      setBusy(false)
    }
  }

  // Reset everything back to a blank studio (safe: blocked while a generation
  // is running; confirms first when an edit session would be lost).
  const reset = () => {
    if (busy) return
    if (turns.length > 0 && !window.confirm('Start a new session? Your current versions will be lost.')) return
    setPrompt('')
    setInputImages([])
    setTurns([])
    setActiveIdx(0)
    setError('')
    setIsRefusal(false)
    setQuality('auto')
    setSize('auto')
    setOutputFormat('png')
    setModel('gpt-5.5')
    // File inputs keep their last value even after state clears — wipe it so
    // re-uploading the same file still fires onChange.
    if (fileRef.current) fileRef.current.value = ''
  }

  const download = () => {
    if (!active) return
    const ext = (active.mime.split('/')[1] || 'png').replace('jpeg', 'jpg')
    const a = document.createElement('a')
    a.href = `data:${active.mime};base64,${active.base64}`
    a.download = `image-studio-v${activeIdx + 1}-${Date.now()}.${ext}`
    a.click()
  }

  const onPromptKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      run()
    }
  }

  const errorBanner = error && (
    <div
      className={`rounded-lg border p-3 text-sm whitespace-pre-wrap break-words ${
        isRefusal
          ? 'border-amber-500/40 bg-amber-500/10 text-amber-300'
          : 'border-red-500/40 bg-red-500/10 text-red-300'
      }`}
    >
      {isRefusal && (
        <p className="font-medium mb-1 flex items-center gap-1.5">
          <WarningAmber sx={{ fontSize: 15 }} /> No image was generated
        </p>
      )}
      {error}
    </div>
  )

  return (
    <>
      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={tryClose}>
          <div
            className="w-[1100px] max-w-[95vw] h-[85vh] max-h-[900px] rounded-xl bg-surface border border-border shadow-2xl flex flex-col overflow-hidden"
            onClick={e => e.stopPropagation()}
          >
            {/* Header bar */}
            <div className="flex items-center justify-between px-5 py-3 border-b border-border shrink-0">
              <h2 className="text-base font-semibold flex items-center gap-2">
                <AutoAwesome sx={{ fontSize: 18 }} className="text-accent" /> Image Studio
                {hasSession && (
                  <span className="ml-3 text-xs font-normal text-muted">
                    v{activeIdx + 1} of {turns.length}
                  </span>
                )}
              </h2>
              <div className="flex items-center gap-4">
                <button
                  onClick={reset}
                  disabled={busy}
                  className="text-xs text-muted hover:text-fg disabled:opacity-50 flex items-center gap-1"
                  title="Clear everything and start a new image"
                >
                  <RestartAlt sx={{ fontSize: 14 }} /> New session
                </button>
                <button onClick={tryClose} className="text-muted hover:text-fg flex items-center" title="Close (Esc)"><Close sx={{ fontSize: 20 }} /></button>
              </div>
            </div>

            {/* Body: controls sidebar + preview canvas. Stacks on small screens. */}
            <div className="flex-1 flex flex-col md:flex-row min-h-0">

              {/* Left: first-turn controls, or the edit thread once a session exists */}
              <div className="w-full md:w-[360px] md:border-r border-b md:border-b-0 border-border flex flex-col min-h-0 shrink-0">
                {!hasSession ? (
                  /* ── First turn: full controls ─────────────────────────── */
                  <div className="p-5 flex flex-col gap-4 overflow-y-auto flex-1">
                    <div className="flex flex-col gap-1.5 flex-1 min-h-[140px]">
                      <label className="text-xs font-medium text-muted uppercase tracking-wide">Prompt</label>
                      <textarea
                        value={prompt}
                        onChange={e => setPrompt(e.target.value)}
                        placeholder={inputImages.length > 0
                          ? `Describe how to use your ${inputImages.length} image${inputImages.length > 1 ? 's' : ''}…`
                          : 'Describe the image to generate — or upload images to edit…'}
                        onKeyDown={onPromptKeyDown}
                        className="w-full flex-1 min-h-[120px] rounded-lg border border-border bg-base p-3 text-sm resize-none focus:outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30"
                      />
                      <span className="text-[11px] text-muted">Ctrl+Enter to generate · Esc to close</span>
                    </div>

                    {isAdmin && (
                    <div className="flex flex-col gap-1.5">
                      <label className="text-xs font-medium text-muted uppercase tracking-wide">Model</label>
                      <select
                        value={model}
                        onChange={e => setModel(e.target.value)}
                        disabled={busy}
                        className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm disabled:opacity-50 focus:outline-none focus:border-accent/60"
                      >
                        <option value="gpt-5.5">GPT-5.5 (battle-tested)</option>
                        <option value="gpt-5.6-sol">GPT-5.6 Sol (flagship)</option>
                        <option value="gpt-5.6-terra">GPT-5.6 Terra (balanced)</option>
                        <option value="gpt-5.6-luna">GPT-5.6 Luna (fastest)</option>
                      </select>
                    </div>
                    )}

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
                      <label className="text-xs font-medium text-muted uppercase tracking-wide">
                        Reference images <span className="font-normal">(optional, up to {MAX_INPUT_IMAGES})</span>
                      </label>
                      <input
                        ref={fileRef}
                        type="file"
                        accept={ALLOWED_MIMES.join(',')}
                        multiple
                        className="hidden"
                        onChange={e => { onFiles(e.target.files); if (fileRef.current) fileRef.current.value = '' }}
                      />
                      {inputImages.length < MAX_INPUT_IMAGES && (
                        <button
                          onClick={() => fileRef.current?.click()}
                          disabled={busy}
                          className="w-full px-3 py-2 rounded-lg border border-dashed border-border text-sm text-muted hover:text-fg hover:border-accent/60 disabled:opacity-50 transition-colors flex items-center justify-center gap-1.5"
                        >
                          <AddPhotoAlternateOutlined sx={{ fontSize: 16 }} />
                          {inputImages.length === 0 ? 'Upload images' : `Add more (${inputImages.length}/${MAX_INPUT_IMAGES})`}
                        </button>
                      )}
                      {inputImages.length > 0 && (
                        <div className="flex flex-col gap-1.5 max-h-[200px] overflow-y-auto">
                          {inputImages.map((img, i) => (
                            <div key={i} className="flex items-center gap-2 text-xs text-muted rounded-lg border border-border p-2">
                              <img
                                src={`data:${img.mime};base64,${img.base64}`}
                                alt={`input ${i + 1}`}
                                className="h-10 w-10 object-cover rounded border border-border shrink-0"
                              />
                              <span className="flex-1 truncate">{img.name}</span>
                              <button onClick={() => removeImage(i)} disabled={busy} className="text-muted hover:text-fg flex items-center shrink-0" title="Remove">
                                <Close sx={{ fontSize: 16 }} />
                              </button>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>

                    <div className="flex gap-2">
                      <div className="flex flex-col gap-1.5 flex-1">
                        <label className="text-xs font-medium text-muted uppercase tracking-wide">Size</label>
                        <select
                          value={size}
                          onChange={e => setSize(e.target.value)}
                          disabled={busy}
                          className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm disabled:opacity-50 focus:outline-none focus:border-accent/60"
                        >
                          {SIZE_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                      </div>
                      <div className="flex flex-col gap-1.5 flex-1">
                        <label className="text-xs font-medium text-muted uppercase tracking-wide">Format</label>
                        <select
                          value={outputFormat}
                          onChange={e => setOutputFormat(e.target.value)}
                          disabled={busy}
                          className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm disabled:opacity-50 focus:outline-none focus:border-accent/60"
                        >
                          {FORMAT_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                      </div>
                    </div>

                    <button
                      onClick={run}
                      disabled={busy || !prompt.trim()}
                      className="w-full px-4 py-2.5 rounded-lg bg-accent text-white text-sm font-medium disabled:opacity-50 hover:opacity-90 transition-opacity"
                    >
                      {busy
                        ? `Generating… ${elapsed}s`
                        : inputImages.length > 0 ? `Edit${inputImages.length > 1 ? ` (${inputImages.length} images)` : ' image'}` : 'Generate image'}
                    </button>
                    {busy && (
                      <p className="text-[11px] text-muted text-center -mt-2">Image models can take 1–2 minutes</p>
                    )}

                    {errorBanner}
                  </div>
                ) : (
                  /* ── Session active: edit thread + composer ────────────── */
                  <>
                    <div ref={threadRef} className="flex-1 min-h-0 overflow-y-auto p-4 flex flex-col gap-3">
                      {turns.map((t, i) => (
                        <button
                          key={i}
                          onClick={() => { if (!busy) { setActiveIdx(i); setVersionTipDismissed(true) } }}
                          className={`text-left rounded-lg border p-3 flex gap-3 items-start transition-colors ${
                            i === activeIdx
                              ? 'border-accent/60 bg-accent/5'
                              : 'border-border hover:border-accent/40'
                          }`}
                          title={i === activeIdx ? 'Current version' : 'View this version — your next edit continues from it'}
                        >
                          <img
                            src={`data:${t.mime};base64,${t.base64}`}
                            alt={`v${i + 1}`}
                            className="h-12 w-12 object-cover rounded border border-border shrink-0"
                          />
                          <div className="min-w-0 flex-1">
                            <p className="text-[11px] font-medium text-muted mb-0.5">
                              v{i + 1}{i === activeIdx ? ' · viewing' : ''}
                            </p>
                            <p className="text-sm leading-snug line-clamp-3 break-words">{t.prompt}</p>
                          </div>
                        </button>
                      ))}
                      {busy && (
                        <div className="rounded-lg border border-border border-dashed p-3 text-sm text-muted">
                          Generating v{turns.length + 1}… {elapsed}s
                        </div>
                      )}
                    </div>

                    {/* Composer: next edit, chained from the active version */}
                    <div className="border-t border-border p-4 flex flex-col gap-2 shrink-0">
                      {errorBanner}
                      <p className="text-[11px] text-muted">
                        Editing from <span className="text-fg font-medium">v{activeIdx + 1}</span>
                        {activeIdx !== turns.length - 1 && ' (branching from an earlier version)'}
                      </p>
                      {turns.length >= 2 && !versionTipDismissed && (
                        <div className="flex items-center gap-2 rounded-md bg-accent/10 border border-accent/20 px-3 py-1.5 text-[11px] text-muted">
                          <span>💡</span>
                          <span className="flex-1">
                            <span className="font-medium text-fg">Tip:</span> Click any version above to edit from it. By default, edits continue from the latest.
                          </span>
                          <button
                            onClick={() => setVersionTipDismissed(true)}
                            className="text-muted hover:text-fg transition-colors ml-1 shrink-0"
                            title="Dismiss tip"
                          >
                            <Close sx={{ fontSize: 14 }} />
                          </button>
                        </div>
                      )}
                      <textarea
                        value={prompt}
                        onChange={e => setPrompt(e.target.value)}
                        onKeyDown={onPromptKeyDown}
                        placeholder="Describe your next edit…"
                        rows={3}
                        className="w-full rounded-lg border border-border bg-base p-3 text-sm resize-none focus:outline-none focus:border-accent/60 focus:ring-1 focus:ring-accent/30"
                      />
                      <div className="flex items-center gap-2 flex-wrap">
                        {isAdmin && (
                        <select
                          value={model}
                          onChange={e => setModel(e.target.value)}
                          disabled={busy}
                          title="Model for the next edit"
                          className="rounded-lg border border-border bg-base px-2 py-2 text-xs disabled:opacity-50 focus:outline-none focus:border-accent/60"
                        >
                          <option value="gpt-5.5">GPT-5.5</option>
                          <option value="gpt-5.6-sol">5.6 Sol</option>
                          <option value="gpt-5.6-terra">5.6 Terra</option>
                          <option value="gpt-5.6-luna">5.6 Luna</option>
                        </select>
                        )}
                        <select
                          value={quality}
                          onChange={e => setQuality(e.target.value)}
                          disabled={busy}
                          title="Quality"
                          className="rounded-lg border border-border bg-base px-2 py-2 text-xs disabled:opacity-50 focus:outline-none focus:border-accent/60"
                        >
                          <option value="auto">Auto</option>
                          <option value="low">Low</option>
                          <option value="medium">Medium</option>
                          <option value="high">High</option>
                        </select>
                        <select
                          value={size}
                          onChange={e => setSize(e.target.value)}
                          disabled={busy}
                          title="Size"
                          className="rounded-lg border border-border bg-base px-2 py-2 text-xs disabled:opacity-50 focus:outline-none focus:border-accent/60"
                        >
                          {SIZE_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                        <select
                          value={outputFormat}
                          onChange={e => setOutputFormat(e.target.value)}
                          disabled={busy}
                          title="Output format"
                          className="rounded-lg border border-border bg-base px-2 py-2 text-xs disabled:opacity-50 focus:outline-none focus:border-accent/60"
                        >
                          {FORMAT_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                        <button
                          onClick={run}
                          disabled={busy || !prompt.trim()}
                          className="flex-1 px-4 py-2 rounded-lg bg-accent text-white text-sm font-medium disabled:opacity-50 hover:opacity-90 transition-opacity"
                        >
                          {busy ? `Applying… ${elapsed}s` : 'Apply edit'}
                        </button>
                      </div>
                      <span className="text-[11px] text-muted">Ctrl+Enter to apply · every edit is kept as a version</span>
                    </div>
                  </>
                )}
              </div>

              {/* Right: preview canvas + version filmstrip */}
              <div className="flex-1 min-h-[280px] min-w-0 p-5 overflow-y-auto flex flex-col gap-3 bg-base/40">
                {active ? (
                  <>
                    <div className="flex-1 min-h-0 flex items-center justify-center">
                      <img
                        src={`data:${active.mime};base64,${active.base64}`}
                        alt={`result v${activeIdx + 1}`}
                        className="max-w-full max-h-full object-contain rounded-lg border border-border shadow-lg"
                      />
                    </div>
                    {active.text && (
                      <p className="text-sm text-muted whitespace-pre-wrap break-words leading-relaxed shrink-0 max-h-[20%] overflow-y-auto">
                        {active.text}
                      </p>
                    )}
                    <div className="flex items-end justify-between gap-3 shrink-0">
                      {/* Filmstrip: one thumbnail per version, newest last */}
                      <div className="flex gap-2 overflow-x-auto py-1">
                        {turns.map((t, i) => (
                          <button
                            key={i}
                            onClick={() => { if (!busy) { setActiveIdx(i); setVersionTipDismissed(true) } }}
                            title={`v${i + 1}: ${t.prompt.slice(0, 80)}`}
                            className={`relative shrink-0 rounded-lg overflow-hidden border-2 transition-colors ${
                              i === activeIdx ? 'border-accent' : 'border-border hover:border-accent/50'
                            }`}
                          >
                            <img
                              src={`data:${t.mime};base64,${t.base64}`}
                              alt={`v${i + 1}`}
                              className="h-14 w-14 object-cover"
                            />
                            <span className="absolute bottom-0 right-0 px-1 text-[10px] leading-4 bg-black/60 text-white rounded-tl">
                              v{i + 1}
                            </span>
                          </button>
                        ))}
                      </div>
                      <button
                        onClick={download}
                        className="px-4 py-2 rounded-lg border border-border text-sm text-muted hover:text-fg hover:border-accent/60 shrink-0 transition-colors flex items-center gap-1.5"
                      >
                        <FileDownload sx={{ fontSize: 16 }} /> Download v{activeIdx + 1}
                      </button>
                    </div>
                  </>
                ) : (
                  <div className="flex-1 flex flex-col items-center justify-center text-muted gap-2 select-none">
                    <ImageOutlined sx={{ fontSize: 56 }} className="opacity-40" />
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
