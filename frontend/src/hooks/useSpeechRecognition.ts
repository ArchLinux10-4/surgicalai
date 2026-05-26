/**
 * useSpeechRecognition — speech-to-text hook.
 *
 * Shows mic button on any browser that has SpeechRecognition in window.
 * On iOS (service-not-allowed) shows a clear error instead of hiding the button,
 * so the user understands why it doesn't work rather than seeing it vanish.
 *
 * Desktop Chrome: works fully, can be used repeatedly without page refresh.
 * Android Chrome: works fully.
 * iOS (all browsers): API exists but Apple blocks it — shows error message.
 * Firefox / other: API absent — button never renders.
 */
import { useState, useEffect, useRef, useCallback } from 'react'

export type SpeechState = 'idle' | 'listening' | 'processing'

export interface UseSpeechRecognitionReturn {
  isSupported: boolean
  state: SpeechState
  interimTranscript: string
  startListening: () => void
  stopListening: () => void
  errorMessage: string
}

function getSRClass(): any {
  if (typeof window === 'undefined') return null
  return (window as any).SpeechRecognition ||
         (window as any).webkitSpeechRecognition ||
         null
}

export function useSpeechRecognition(
  onTranscript: (text: string) => void
): UseSpeechRecognitionReturn {
  // Derived once — SRClass is stable (it's the constructor on window)
  const SRClass = useRef<any>(getSRClass())
  const isSupported = SRClass.current !== null

  const [state, setState]           = useState<SpeechState>('idle')
  const [interim, setInterim]       = useState('')
  const [errorMsg, setErrorMsg]     = useState('')
  const recRef                      = useRef<any>(null)
  const cbRef                       = useRef(onTranscript)

  useEffect(() => { cbRef.current = onTranscript }, [onTranscript])

  const stopListening = useCallback(() => {
    const rec = recRef.current
    if (rec) {
      try { rec.stop() } catch {}
      recRef.current = null
    }
    setState('idle')
    setInterim('')
  }, [])

  const startListening = useCallback(() => {
    if (!SRClass.current) return

    // Clear previous instance cleanly
    const prev = recRef.current
    if (prev) {
      try { prev.abort() } catch {}
      recRef.current = null
    }

    setErrorMsg('')
    setInterim('')
    setState('idle')

    const rec = new SRClass.current()
    rec.continuous      = false
    rec.interimResults  = true
    rec.lang            = 'en-US'
    rec.maxAlternatives = 1

    rec.onstart = () => {
      setState('listening')
    }

    rec.onresult = (event: any) => {
      let interimText = ''
      let finalText   = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const t = event.results[i][0].transcript
        if (event.results[i].isFinal) {
          finalText += t
        } else {
          interimText += t
        }
      }
      setInterim(interimText)
      if (finalText.trim()) {
        setState('processing')
        setInterim('')
        cbRef.current(finalText.trim())
        // Reset to idle after handing off transcript so button is usable again
        setTimeout(() => setState('idle'), 300)
      }
    }

    rec.onerror = (event: any) => {
      const err = event.error as string
      // service-not-allowed = iOS blocks the API at the platform level
      // Show a clear message — don't hide the button (confusing UX)
      const msg =
        err === 'service-not-allowed' ? 'Voice input not supported on this device.' :
        err === 'not-allowed'         ? 'Mic access denied — allow it in browser settings.' :
        err === 'no-speech'           ? 'No speech detected. Tap and try again.' :
        err === 'network'             ? 'Network error — check connection.' :
        err === 'aborted'             ? '' :
        err === 'audio-capture'       ? 'No microphone found.' :
        ''
      if (msg) setErrorMsg(msg)
      setState('idle')
      setInterim('')
      recRef.current = null
    }

    rec.onend = () => {
      // Only reset to idle if not already in processing (transcript was delivered)
      setState(prev => (prev === 'processing' ? prev : 'idle'))
      setInterim('')
      recRef.current = null
    }

    recRef.current = rec
    try {
      rec.start()
    } catch (e) {
      // start() can throw if called too quickly after a previous stop
      // Small delay and retry once
      setTimeout(() => {
        try {
          const rec2 = new SRClass.current()
          rec2.continuous = false
          rec2.interimResults = true
          rec2.lang = 'en-US'
          rec2.onresult  = rec.onresult
          rec2.onerror   = rec.onerror
          rec2.onend     = rec.onend
          rec2.onstart   = rec.onstart
          recRef.current = rec2
          rec2.start()
        } catch {
          setState('idle')
        }
      }, 250)
    }
  }, []) // No deps — uses refs only, never stale

  // Cleanup on unmount
  useEffect(() => () => {
    const rec = recRef.current
    if (rec) { try { rec.abort() } catch {} }
  }, [])

  // Auto-clear error after 5 s
  useEffect(() => {
    if (!errorMsg) return
    const t = setTimeout(() => setErrorMsg(''), 5000)
    return () => clearTimeout(t)
  }, [errorMsg])

  return {
    isSupported,
    state,
    interimTranscript: interim,
    startListening,
    stopListening,
    errorMessage: errorMsg,
  }
}
