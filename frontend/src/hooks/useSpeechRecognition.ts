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
  const ua = navigator.userAgent
  // Chrome on desktop and Android — exclude Edge (Chromium) and Opera
  // to keep the feature focused on the best-tested environment
  return (
    /Chrome\//.test(ua) &&
    !/Edg\//.test(ua) &&   // Edge Chromium
    !/OPR\//.test(ua) &&   // Opera
    !/Brave\//.test(ua)    // Brave
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
  const isSupported = isChrome() && SpeechRecognition !== null

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
      const msg =
        event.error === 'not-allowed'   ? 'Microphone access denied. Allow it in browser settings.' :
        event.error === 'no-speech'     ? 'No speech detected. Try again.' :
        event.error === 'network'       ? 'Network error. Check your connection.' :
        event.error === 'aborted'       ? '' :   // user stopped — not an error
        `Speech error: ${event.error}`
      if (msg) setErrorMessage(msg)
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
