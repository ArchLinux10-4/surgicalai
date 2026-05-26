/**
 * useSpeechOutput — speak Claude's text responses aloud.
 *
 * Strips code blocks, inline code, markdown syntax, and diff lines
 * before speaking so Claude doesn't read out backticks and angle brackets.
 *
 * Uses the browser's built-in SpeechSynthesis API — no external service.
 * Works on Chrome, Edge, Safari, Firefox (read-aloud is not Chrome-only).
 */
import { useState, useEffect, useRef, useCallback } from 'react'

export interface UseSpeechOutputReturn {
  isSpeaking: boolean
  speak: (text: string) => void
  stop: () => void
}

/**
 * Strip everything that would sound weird when read aloud:
 * - fenced code blocks (```...```)
 * - inline code (`...`)
 * - markdown headers (###)
 * - bold/italic (**text**, *text*, __text__, _text_)
 * - bullet list markers (- , * , 1. )
 * - diff +/- line prefixes
 * - HTML tags
 * - URLs
 * - surgical_edit / new_file XML tags
 * - QA Notes section (already visible in the diff card)
 */
function cleanForSpeech(text: string): string {
  return text
    // Remove fenced code blocks entirely
    .replace(/```[\s\S]*?```/g, ' [code block] ')
    // Remove inline code
    .replace(/`[^`]+`/g, match => {
      const inner = match.slice(1, -1)
      // Short identifiers — say the name; long expressions — skip
      return inner.length <= 30 ? inner : ' [code] '
    })
    // Remove surgical_edit / new_file XML tag content
    .replace(/<surgical_edit>[\s\S]*?<\/surgical_edit>/g, ' [code edit] ')
    .replace(/<new_file>[\s\S]*?<\/new_file>/g, ' [new file] ')
    // Remove markdown headers
    .replace(/^#{1,6}\s+/gm, '')
    // Remove bold/italic
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/_([^_]+)_/g, '$1')
    // Remove URLs
    .replace(/https?:\/\/\S+/g, ' [link] ')
    // Remove HTML tags
    .replace(/<[^>]+>/g, '')
    // Remove diff +/- prefixes at line start
    .replace(/^[+\-]{1,3}\s/gm, '')
    // Remove bullet markers
    .replace(/^[\s]*[-*]\s/gm, '')
    .replace(/^[\s]*\d+\.\s/gm, '')
    // Remove QA Notes section (user can read the card)
    .replace(/\*\*QA Notes:\*\*[\s\S]*$/m, '')
    // Collapse multiple whitespace/newlines
    .replace(/\n{2,}/g, '. ')
    .replace(/\n/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

export function useSpeechOutput(): UseSpeechOutputReturn {
  const [isSpeaking, setIsSpeaking] = useState(false)
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null)

  const stop = useCallback(() => {
    window.speechSynthesis?.cancel()
    setIsSpeaking(false)
    utteranceRef.current = null
  }, [])

  const speak = useCallback((text: string) => {
    if (!window.speechSynthesis) return
    // Stop any current speech
    window.speechSynthesis.cancel()

    const cleaned = cleanForSpeech(text)
    if (!cleaned || cleaned.length < 3) return

    const utterance = new SpeechSynthesisUtterance(cleaned)

    // Prefer a natural English voice if available
    const voices = window.speechSynthesis.getVoices()
    const preferred = voices.find(v =>
      v.lang.startsWith('en') && (v.name.includes('Natural') || v.name.includes('Neural') || v.localService)
    ) || voices.find(v => v.lang.startsWith('en')) || voices[0]
    if (preferred) utterance.voice = preferred

    utterance.rate   = 1.05   // slightly faster than default — less robotic
    utterance.pitch  = 1.0
    utterance.volume = 1.0

    utterance.onstart = () => setIsSpeaking(true)
    utterance.onend   = () => { setIsSpeaking(false); utteranceRef.current = null }
    utterance.onerror = () => { setIsSpeaking(false); utteranceRef.current = null }

    utteranceRef.current = utterance
    window.speechSynthesis.speak(utterance)
  }, [])

  // Stop speech on unmount
  useEffect(() => {
    return () => { window.speechSynthesis?.cancel() }
  }, [])

  return { isSpeaking, speak, stop }
}
