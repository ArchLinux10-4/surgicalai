/**
 * useSpeechRecognition — Chrome-only speech-to-text hook.
 *
 * Returns null on any non-Chrome browser so callers can hide the UI entirely.
 * Uses the Web Speech API (SpeechRecognition / webkitSpeechRecognition).
 *
 * Zero dependencies beyond React. Zero backend calls.
 */
import { useState, useEffect, useRef, useCallback } from 'react'

export type SpeechState = 'idle' | 'listening' | 'processing' | 'error'

export interface UseSpeechRecognitionReturn {
  /** Whether Chrome SpeechRecognition is available in this browser */
  isSupported: boolean
  state: SpeechState
  /** Interim transcript shown in real time while speaking */
  interimTranscript: string
  /** Start listening — calls onTranscript when a final result arrives */
  startListening: () => void
  /** Stop listening manually */
  stopListening: () => void
  errorMessage: string
}

function isChrome(): boolean {
  if (typeof window === 'undefined') return false
  // Base support on whether the API actually exists — more reliable than UA sniffing.
  // SpeechRecognition is present on Chrome desktop, Chrome Android, and Edge.
  // It is NOT present on iOS Safari/Chrome (Apple doesn't expose it).
  // This correctly shows the button on every browser that can actually use it.
  return !!(
    (window as any).SpeechRecognition ||
    (window as any).webkitSpeechRecognition
  )
}

function getSpeechRecognition(): any {
  if (typeof window === 'undefined') return null
  return (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition || null
}

export function useSpeechRecognition(
  onTranscript: (text: string) => void
): UseSpeechRecognitionReturn {
  const SpeechRecognition = getSpeechRecognition()
  // Start with API existence check — will be set false if service-not-allowed fires
  const [isSupported, setIsSupported]       = useState(() => SpeechRecognition !== null)

  const [state, setState]                   = useState<SpeechState>('idle')
  const [interimTranscript, setInterim]     = useState('')
  const [errorMessage, setErrorMessage]     = useState('')
  const recognitionRef                      = useRef<any>(null)
  const onTranscriptRef                     = useRef(onTranscript)

  // Keep ref fresh without re-creating the recognition instance
  useEffect(() => { onTranscriptRef.current = onTranscript }, [onTranscript])

  const stopListening = useCallback(() => {
    if (recognitionRef.current) {
      recognitionRef.current.stop()
      recognitionRef.current = null
    }
    setState('idle')
    setInterim('')
  }, [])

  const startListening = useCallback(() => {
    if (!isSupported) return
    if (recognitionRef.current) {
      recognitionRef.current.stop()
      recognitionRef.current = null
    }

    setErrorMessage('')
    setInterim('')

    const rec = new SpeechRecognition()
    rec.continuous      = false   // stop after first pause
    rec.interimResults  = true    // show words as they come
    rec.lang            = 'en-US'
    rec.maxAlternatives = 1

    rec.onstart = () => setState('listening')

    rec.onresult = (event: any) => {
      let interim = ''
      let final   = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const t = event.results[i][0].transcript
        if (event.results[i].isFinal) {
          final += t
        } else {
          interim += t
        }
      }
      setInterim(interim)
      if (final.trim()) {
        setState('processing')
        setInterim('')
        onTranscriptRef.current(final.trim())
      }
    }

    rec.onerror = (event: any) => {
      if (event.error === 'service-not-allowed' || event.error === 'not-allowed') {
        // Device has the API but won't allow it (iOS, or mic permission denied permanently)
        // Hide the button entirely so user isn't confused by a non-functional mic icon
        if (event.error === 'service-not-allowed') {
          setIsSupported(false)
        } else {
          setErrorMessage('Microphone access denied. Allow it in browser settings.')
        }
      } else {
        const msg =
          event.error === 'no-speech'  ? 'No speech detected. Try again.' :
          event.error === 'network'    ? 'Network error. Check your connection.' :
          event.error === 'aborted'    ? '' :
          `Speech error: ${event.error}`
        if (msg) setErrorMessage(msg)
      }
      setState('idle')
      setInterim('')
    }

    rec.onend = () => {
      setState(prev => prev === 'processing' ? 'processing' : 'idle')
      setInterim('')
      recognitionRef.current = null
    }

    recognitionRef.current = rec
    rec.start()
  }, [isSupported, SpeechRecognition])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (recognitionRef.current) {
        recognitionRef.current.abort()
        recognitionRef.current = null
      }
    }
  }, [])

  return {
    isSupported,
    state,
    interimTranscript,
    startListening,
    stopListening,
    errorMessage,
  }
}
