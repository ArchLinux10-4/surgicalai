/**
 * VoiceButton — microphone input + speaker output for SurgicalAI.
 *
 * Self-contained. Returns null on non-Chrome browsers.
 * Receives onTranscript (fill the input box) and lastResponse (text to speak).
 *
 * Two modes in one button:
 *   • Mic mode  — tap to start/stop voice input
 *   • Speaker   — separate small button to read Claude's last response aloud
 *
 * No backend. No new API calls. No store changes.
 */
import React, { useEffect } from 'react'
import { useSpeechRecognition } from '../hooks/useSpeechRecognition'
import { useSpeechOutput } from '../hooks/useSpeechOutput'

interface VoiceButtonProps {
  /** Called with the final transcript — parent sets it as input value */
  onTranscript: (text: string) => void
  /** The last assistant response text — spoken when user taps speaker */
  lastResponse?: string
  /** Disable while streaming */
  disabled?: boolean
  /** compact = mobile style (smaller) */
  size?: 'default' | 'compact'
}

// Animated waveform shown while listening
function WaveIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 16" fill="currentColor">
      {[2, 5.5, 9, 12.5, 16].map((x, i) => (
        <rect
          key={i}
          x={x} y={0} width={2.5} rx={1.25}
          style={{
            height: 16,
            transformOrigin: `${x + 1.25}px 8px`,
            animation: `wave 0.8s ease-in-out infinite`,
            animationDelay: `${i * 0.12}s`,
          }}
        />
      ))}
      <style>{`
        @keyframes wave {
          0%, 100% { transform: scaleY(0.25); }
          50%       { transform: scaleY(1); }
        }
      `}</style>
    </svg>
  )
}

function MicIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
      <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
      <line x1="12" y1="19" x2="12" y2="23"/>
      <line x1="8" y1="23" x2="16" y2="23"/>
    </svg>
  )
}

function SpeakerIcon({ playing }: { playing: boolean }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      {playing ? (
        <>
          <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
          <line x1="22" y1="9" x2="16" y2="15"/><line x1="16" y1="9" x2="22" y2="15"/>
        </>
      ) : (
        <>
          <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
          <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
          <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
        </>
      )}
    </svg>
  )
}

export function VoiceButton({
  onTranscript,
  lastResponse,
  disabled = false,
  size = 'default',
}: VoiceButtonProps) {
  const { isSupported, state, interimTranscript, startListening, stopListening, errorMessage }
    = useSpeechRecognition(onTranscript)

  const { isSpeaking, speak, stop: stopSpeaking } = useSpeechOutput()

  // Auto-stop speech when a new recording starts
  const handleMicClick = () => {
    if (isSpeaking) stopSpeaking()
    if (state === 'listening') {
      stopListening()
    } else {
      startListening()
    }
  }

  const handleSpeakerClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (isSpeaking) {
      stopSpeaking()
    } else if (lastResponse) {
      speak(lastResponse)
    }
  }

  // Not Chrome — render nothing, zero impact on existing UI
  if (!isSupported) return null

  const isListening  = state === 'listening'
  const isProcessing = state === 'processing'
  const compact      = size === 'compact'

  const btnSize = compact
    ? 'w-9 h-9 rounded-xl'
    : 'h-8 w-8 rounded-lg'

  return (
    <div className="flex items-center gap-1 relative">
      {/* Interim transcript tooltip */}
      {interimTranscript && (
        <div className="absolute bottom-full mb-2 right-0
          bg-surface border border-border rounded-lg px-2.5 py-1.5 text-[11px] text-ink/80
          shadow-lg max-w-[200px] truncate z-50 whitespace-nowrap">
          {interimTranscript}
        </div>
      )}

      {/* Error tooltip */}
      {errorMessage && (
        <div className="absolute bottom-full mb-2 right-0
          bg-red-900/80 border border-red-500/40 rounded-lg px-2.5 py-1.5 text-[11px] text-red-300
          shadow-lg max-w-[220px] z-50 whitespace-normal leading-snug">
          {errorMessage}
        </div>
      )}

      {/* Mic button */}
      <button
        onClick={handleMicClick}
        disabled={disabled || isProcessing}
        title={isListening ? 'Stop recording' : 'Voice input (Chrome only)'}
        className={`
          ${btnSize} flex items-center justify-center transition-all relative
          ${isListening
            ? 'bg-red-500/20 border border-red-500/50 text-red-400 hover:bg-red-500/30'
            : isProcessing
              ? 'bg-orange/20 border border-orange/30 text-orange/60 cursor-wait'
              : 'border border-border text-muted/60 hover:text-ink/80 hover:border-border/80 hover:bg-overlay/60'
          }
          disabled:opacity-40 disabled:cursor-not-allowed
        `}
      >
        {isListening ? <WaveIcon /> : <MicIcon />}

        {/* Pulse ring while listening */}
        {isListening && (
          <span className="absolute inset-0 rounded-xl border-2 border-red-500/40 animate-ping" />
        )}
      </button>

      {/* Speaker button — only shown when there's something to read */}
      {lastResponse && (
        <button
          onClick={handleSpeakerClick}
          title={isSpeaking ? 'Stop speaking' : 'Read response aloud'}
          className={`
            ${compact ? 'w-7 h-7 rounded-lg' : 'w-6 h-6 rounded-md'}
            flex items-center justify-center transition-all
            ${isSpeaking
              ? 'bg-blue-500/20 border border-blue-500/40 text-blue-400 hover:bg-blue-500/30'
              : 'border border-border/60 text-muted/50 hover:text-ink/70 hover:border-border hover:bg-overlay/40'
            }
          `}
        >
          <SpeakerIcon playing={isSpeaking} />
        </button>
      )}
    </div>
  )
}
