import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Check, EditOutlined, HelpOutline } from '@mui/icons-material'
import {
  MAX_CUSTOM_ANSWER_CHARS,
  formatAnswers,
  initialStepper,
  isLastQuestion,
  stepperReducer,
} from '../lib/planQuestions'
import type { PlanAnswer, PlanQuestion, StepperAction, StepperState } from '../lib/planQuestions'

/**
 * Clarifying questions asked by the Plan agent. One question at a time with up
 * to two suggested options, a free-text "something else" answer and skip.
 * Submitting sends one ordinary Plan-mode message (see formatAnswers), so the
 * server needs no pause/resume state.
 */

const AUTO_ADVANCE_MS = 150
const MIN_TOUCH = 'min-h-[44px] sm:min-h-0'

interface Props {
  questions: PlanQuestion[]
  onSubmit: (message: string) => void
  /** Return false to avoid pulling focus from a composer the user is typing in. */
  canAutoFocus?: () => boolean
}

export function PlanQuestionsCard({ questions, onSubmit, canAutoFocus }: Props) {
  const [state, dispatch] = useReducer(
    (s: StepperState, a: StepperAction) => stepperReducer(s, a, questions),
    undefined,
    initialStepper,
  )
  const [otherOpen, setOtherOpen] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const advanceTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const firstRun = useRef(true)

  const total = questions.length
  const q = questions[Math.min(state.index, total - 1)]
  const answer: PlanAnswer | undefined = q ? state.answers[q.id] : undefined
  const last = isLastQuestion(state, questions)
  const openTextOnly = !!q && q.options.length === 0
  const showInput = openTextOnly || otherOpen || answer?.kind === 'custom'

  const selectedOption = q && answer?.kind === 'option'
    ? q.options.findIndex((o) => o.label === answer.value)
    : -1
  // Roving tabindex: selected option, else "something else" when typing, else first.
  const rovingIndex = selectedOption >= 0 ? selectedOption : showInput && !openTextOnly ? (q?.options.length ?? 0) : 0

  const clearTimer = () => {
    if (advanceTimer.current) clearTimeout(advanceTimer.current)
    advanceTimer.current = null
  }
  useEffect(() => clearTimer, [])

  const focusFirst = useCallback(() => {
    const root = rootRef.current
    if (!root) return
    const target = root.querySelector<HTMLElement>('[role="radio"][tabindex="0"], input[data-plan-answer]')
    target?.focus({ preventScroll: true })
  }, [])

  useEffect(() => {
    rootRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    if (canAutoFocus ? canAutoFocus() : true) focusFirst()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false
      return
    }
    setOtherOpen(false)
    clearTimer()
    if (showInputFor(state, questions)) return
    const id = window.setTimeout(focusFirst, 0)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.index])

  useEffect(() => {
    if (showInput && (otherOpen || openTextOnly)) inputRef.current?.focus()
  }, [otherOpen, openTextOnly, showInput, state.index])

  const send = useCallback((answers: Record<string, PlanAnswer | undefined>) => {
    if (submitted) return
    setSubmitted(true)
    clearTimer()
    onSubmit(formatAnswers(questions, answers))
  }, [onSubmit, questions, submitted])

  const skipAll = useCallback(() => send({}), [send])

  const advanceOrSend = useCallback(() => {
    if (last) send(state.answers)
    else dispatch({ type: 'next' })
  }, [last, send, state.answers])

  const skipThis = () => {
    if (!q) return
    if (last) send({ ...state.answers, [q.id]: { id: q.id, kind: 'skipped' } })
    else dispatch({ type: 'skip' })
  }

  const pickOption = (optionIndex: number) => {
    if (!q || submitted) return
    setOtherOpen(false)
    dispatch({ type: 'select', optionIndex })
    clearTimer()
    if (!last) advanceTimer.current = setTimeout(() => dispatch({ type: 'next' }), AUTO_ADVANCE_MS)
  }

  const openOther = () => {
    clearTimer()
    setOtherOpen(true)
  }

  const radios = () =>
    Array.from(rootRef.current?.querySelectorAll<HTMLElement>('[role="radio"]') ?? [])

  const onRadioKeyDown = (e: KeyboardEvent<HTMLButtonElement>, optionIndex: number | 'other') => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight' || e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
      e.preventDefault()
      const list = radios()
      const at = list.indexOf(e.currentTarget)
      const dir = e.key === 'ArrowDown' || e.key === 'ArrowRight' ? 1 : -1
      const next = list[(at + dir + list.length) % list.length]
      list.forEach((el) => el.setAttribute('tabindex', el === next ? '0' : '-1'))
      next?.focus()
      return
    }
    if (e.key === 'Enter') {
      const selected = optionIndex === 'other'
        ? answer?.kind === 'custom' || otherOpen
        : answer?.kind === 'option' && answer.value === q?.options[optionIndex]?.label
      if (selected) {
        e.preventDefault()
        advanceOrSend()
      }
    }
  }

  const onRootKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (submitted) return
    const inInput = (e.target as HTMLElement).tagName === 'INPUT'
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      skipAll()
      return
    }
    if (inInput) {
      if (e.key === 'Enter') {
        e.preventDefault()
        advanceOrSend()
      }
      return
    }
    if ((e.key === '1' || e.key === '2') && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const idx = Number(e.key) - 1
      if (q?.options[idx]) {
        e.preventDefault()
        pickOption(idx)
      }
    }
  }

  const dots = useMemo(() => Array.from({ length: total }, (_, i) => i), [total])

  if (!q) return null

  const customValue = answer?.kind === 'custom' ? answer.value ?? '' : ''
  const answeredCount = questions.filter((x) => {
    const a = state.answers[x.id]
    return a && a.kind !== 'skipped'
  }).length

  return (
    <div
      ref={rootRef}
      role="group"
      aria-label={total > 1 ? 'Clarifying questions' : 'Clarifying question'}
      onKeyDown={onRootKeyDown}
      className={
        'mx-3 mb-2 rounded-xl border border-purple/40 bg-surface/70 backdrop-blur-sm overflow-hidden ' +
        'animate-slide-up motion-reduce:animate-none'
      }
    >
      <div className="sr-only" aria-live="polite">
        {`Question ${state.index + 1} of ${total}: ${q.question}`}
      </div>

      <div className="flex items-center gap-2 px-3.5 pt-2.5">
        <HelpOutline sx={{ fontSize: 15 }} className="text-purple" aria-hidden />
        <span className="text-[11px] font-bold uppercase tracking-wide text-purple">
          {total > 1 ? 'Clarifying questions' : 'Quick question'}
        </span>
        {total > 1 && (
          <span className="flex items-center gap-1 ml-1" aria-hidden>
            {dots.map((i) => (
              <span
                key={i}
                className={
                  'h-1.5 rounded-full transition-all duration-200 ' +
                  (i === state.index ? 'w-4 bg-purple' : i < state.index ? 'w-1.5 bg-purple/60' : 'w-1.5 bg-border')
                }
              />
            ))}
          </span>
        )}
        {total > 1 && (
          <span className="text-[11px] text-muted tabular-nums">{state.index + 1} of {total}</span>
        )}
        <button
          type="button"
          onClick={skipAll}
          disabled={submitted}
          title="Skip the questions and let the agent decide (Esc)"
          className={`ml-auto text-[11px] font-semibold px-2 py-1 rounded-lg text-muted hover:text-ink hover:bg-overlay transition-colors outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:opacity-50 ${MIN_TOUCH}`}
        >
          Skip all
        </button>
      </div>

      <div className="px-3.5 pt-2 pb-3">
        <p className="text-sm font-medium text-ink leading-snug">{q.question}</p>

        {!openTextOnly && (
          <div role="radiogroup" aria-label={q.question} className="mt-2.5 space-y-1.5">
            {q.options.map((opt, i) => {
              const selected = answer?.kind === 'option' && answer.value === opt.label
              const tab = i === rovingIndex ? 0 : -1
              return (
                <button
                  key={opt.label}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  tabIndex={tab}
                  disabled={submitted}
                  onClick={() => pickOption(i)}
                  onKeyDown={(e) => onRadioKeyDown(e, i)}
                  className={
                    'w-full text-left flex items-start gap-2.5 rounded-lg border px-3 py-2 transition-all duration-150 outline-none ' +
                    'focus-visible:ring-2 focus-visible:ring-accent/50 disabled:opacity-60 ' +
                    (selected
                      ? 'border-purple bg-purple/10 ring-1 ring-purple/40'
                      : 'border-border bg-base/40 hover:bg-overlay hover:border-purple/40') +
                    ` ${MIN_TOUCH}`
                  }
                >
                  <span
                    className={
                      'mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded border text-[10px] font-mono ' +
                      (selected ? 'border-purple bg-purple text-base' : 'border-border text-muted')
                    }
                    aria-hidden
                  >
                    {selected ? <Check sx={{ fontSize: 13 }} /> : i + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-1.5">
                      <span className="text-[13px] font-semibold text-ink">{opt.label}</span>
                      {opt.recommended && (
                        <span className="rounded-full border border-purple/30 bg-purple/10 px-1.5 py-px text-[10px] font-semibold text-purple">
                          Recommended
                        </span>
                      )}
                    </span>
                    {opt.description && (
                      <span className="mt-0.5 block text-[12px] leading-snug text-muted">{opt.description}</span>
                    )}
                  </span>
                </button>
              )
            })}

            <button
              type="button"
              role="radio"
              aria-checked={showInput}
              tabIndex={rovingIndex === q.options.length ? 0 : -1}
              disabled={submitted}
              onClick={openOther}
              onKeyDown={(e) => onRadioKeyDown(e, 'other')}
              className={
                'w-full text-left flex items-center gap-2.5 rounded-lg border px-3 py-2 transition-all duration-150 outline-none ' +
                'focus-visible:ring-2 focus-visible:ring-accent/50 disabled:opacity-60 ' +
                (showInput
                  ? 'border-purple bg-purple/10 ring-1 ring-purple/40'
                  : 'border-dashed border-border bg-transparent hover:bg-overlay hover:border-purple/40') +
                ` ${MIN_TOUCH}`
              }
            >
              <span
                className={
                  'flex h-5 w-5 flex-shrink-0 items-center justify-center rounded border ' +
                  (showInput ? 'border-purple bg-purple text-base' : 'border-border text-muted')
                }
                aria-hidden
              >
                {showInput ? <Check sx={{ fontSize: 13 }} /> : <EditOutlined sx={{ fontSize: 12 }} />}
              </span>
              <span className="text-[13px] font-semibold text-ink">Something else…</span>
            </button>
          </div>
        )}

        {showInput && (
          <input
            ref={inputRef}
            data-plan-answer
            type="text"
            value={customValue}
            maxLength={MAX_CUSTOM_ANSWER_CHARS}
            disabled={submitted}
            onChange={(e) => dispatch({ type: 'custom', text: e.target.value })}
            placeholder="Type your own answer…"
            aria-label={`Your answer to: ${q.question}`}
            className={`input mt-2 ${MIN_TOUCH}`}
          />
        )}

        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            onClick={skipThis}
            disabled={submitted}
            className={`text-[11px] font-semibold px-2.5 py-1 rounded-lg text-muted hover:text-ink hover:bg-overlay transition-colors outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:opacity-50 ${MIN_TOUCH}`}
          >
            Skip
          </button>
          <span className="flex-1" />
          {state.index > 0 && (
            <button
              type="button"
              onClick={() => dispatch({ type: 'back' })}
              disabled={submitted}
              className={`text-[11px] font-semibold px-2.5 py-1 rounded-lg text-muted border border-border hover:text-ink hover:bg-overlay transition-colors outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:opacity-50 ${MIN_TOUCH}`}
            >
              Back
            </button>
          )}
          <button
            type="button"
            onClick={advanceOrSend}
            disabled={submitted}
            className={`text-[12px] font-semibold px-3 py-1.5 rounded-lg bg-accent/15 text-accent border border-accent/30 hover:bg-accent/25 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-accent/50 disabled:opacity-50 ${MIN_TOUCH}`}
          >
            {last ? (answeredCount > 0 ? 'Send answers' : 'Continue without answering') : 'Next'}
          </button>
        </div>
        {!openTextOnly && q.options.length > 0 && (
          <p className="mt-2 hidden text-[10px] text-faint sm:block">
            Press 1{q.options.length > 1 ? ' or 2' : ''} to choose, Enter to continue, Esc to skip all.
          </p>
        )}
      </div>
    </div>
  )
}

function showInputFor(state: StepperState, questions: PlanQuestion[]): boolean {
  const q = questions[Math.min(state.index, questions.length - 1)]
  if (!q) return false
  return q.options.length === 0 || state.answers[q.id]?.kind === 'custom'
}
