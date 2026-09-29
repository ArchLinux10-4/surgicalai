/**
 * Plan-mode clarifying questions: pure helpers shared by the desktop and mobile
 * chat panels. No React, no DOM — testable with node --test.
 */

export interface PlanQuestionOption {
  label: string
  description?: string
  recommended?: boolean
}

export interface PlanQuestion {
  id: string
  question: string
  options: PlanQuestionOption[]
}

export type PlanAnswerKind = 'option' | 'custom' | 'skipped'

export interface PlanAnswer {
  id: string
  kind: PlanAnswerKind
  value?: string
}

export const MAX_CUSTOM_ANSWER_CHARS = 500

const SKIPPED_ITEM = '(skipped: use your best judgment and list the assumption)'
const SKIPPED_ALL = "I'm skipping the clarifying questions. Use your best judgment and list your assumptions."

const FENCE_COMPLETE = /```plan_questions[\s\S]*?```/gi
const FENCE_PARTIAL = /```plan_questions[\s\S]*$/i

/** Hide the machine-readable fence from chat text (also while it is still streaming). */
export function stripPlanQuestionsFence(text: string, streaming = false): string {
  if (!text) return text
  let out = text.replace(FENCE_COMPLETE, '')
  if (streaming) out = out.replace(FENCE_PARTIAL, '')
  return out.trim()
}

/** Accept only well-formed question arrays (e.g. from message metadata). */
export function normalizeQuestions(raw: unknown): PlanQuestion[] | null {
  if (!Array.isArray(raw)) return null
  const out: PlanQuestion[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const q = item as Record<string, unknown>
    const question = typeof q.question === 'string' ? q.question.trim() : ''
    if (!question) continue
    const options: PlanQuestionOption[] = []
    if (Array.isArray(q.options)) {
      for (const o of q.options) {
        if (!o || typeof o !== 'object') continue
        const opt = o as Record<string, unknown>
        const label = typeof opt.label === 'string' ? opt.label.trim() : ''
        if (!label) continue
        options.push({
          label,
          description: typeof opt.description === 'string' ? opt.description : '',
          recommended: opt.recommended === true,
        })
      }
    }
    out.push({
      id: typeof q.id === 'string' && q.id ? q.id : `q${out.length + 1}`,
      question,
      options: options.slice(0, 2),
    })
  }
  return out.length ? out.slice(0, 3) : null
}

/** Answers for every question, defaulting unanswered ones to skipped. */
export function completeAnswers(
  questions: PlanQuestion[],
  answers: Record<string, PlanAnswer | undefined>,
): PlanAnswer[] {
  return questions.map((q) => answers[q.id] ?? { id: q.id, kind: 'skipped' as const })
}

/** The user message sent back to Plan mode. */
export function formatAnswers(
  questions: PlanQuestion[],
  answers: Record<string, PlanAnswer | undefined>,
): string {
  const full = completeAnswers(questions, answers)
  if (full.every((a) => a.kind === 'skipped')) return SKIPPED_ALL
  const lines = questions.map((q, i) => {
    const a = full[i]
    const text =
      a.kind === 'skipped' || !(a.value || '').trim()
        ? SKIPPED_ITEM
        : (a.value as string).trim().slice(0, MAX_CUSTOM_ANSWER_CHARS)
    return `${i + 1}. ${q.question} -> ${text}`
  })
  return `Answers to your clarifying questions:\n${lines.join('\n')}`
}

export interface StepperState {
  index: number
  answers: Record<string, PlanAnswer | undefined>
  /** True once the user chose "Skip all". */
  skippedAll: boolean
}

export type StepperAction =
  | { type: 'select'; optionIndex: number }
  | { type: 'custom'; text: string }
  | { type: 'skip' }
  | { type: 'next' }
  | { type: 'back' }
  | { type: 'skipAll' }

export function initialStepper(): StepperState {
  return { index: 0, answers: {}, skippedAll: false }
}

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n))

export function isLastQuestion(state: StepperState, questions: PlanQuestion[]): boolean {
  return state.index >= questions.length - 1
}

export function stepperReducer(
  state: StepperState,
  action: StepperAction,
  questions: PlanQuestion[],
): StepperState {
  const last = Math.max(0, questions.length - 1)
  const q = questions[clamp(state.index, 0, last)]
  if (!q) return state
  switch (action.type) {
    case 'select': {
      const opt = q.options[action.optionIndex]
      if (!opt) return state
      return {
        ...state,
        answers: { ...state.answers, [q.id]: { id: q.id, kind: 'option', value: opt.label } },
      }
    }
    case 'custom': {
      const text = action.text.slice(0, MAX_CUSTOM_ANSWER_CHARS)
      const answers = { ...state.answers }
      if (text.trim()) answers[q.id] = { id: q.id, kind: 'custom', value: text }
      else delete answers[q.id]
      return { ...state, answers }
    }
    case 'skip':
      return {
        ...state,
        answers: { ...state.answers, [q.id]: { id: q.id, kind: 'skipped' } },
        index: clamp(state.index + 1, 0, last),
      }
    case 'next':
      return { ...state, index: clamp(state.index + 1, 0, last) }
    case 'back':
      return { ...state, index: clamp(state.index - 1, 0, last) }
    case 'skipAll':
      return { ...state, answers: {}, skippedAll: true }
    default:
      return state
  }
}
