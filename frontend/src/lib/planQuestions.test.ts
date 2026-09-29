/**
 * Run from frontend/: node --experimental-strip-types --test src/lib/planQuestions.test.ts
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  MAX_CUSTOM_ANSWER_CHARS,
  completeAnswers,
  formatAnswers,
  initialStepper,
  isLastQuestion,
  normalizeQuestions,
  stepperReducer,
  stripPlanQuestionsFence,
} from './planQuestions.ts'
import type { PlanQuestion, StepperAction, StepperState } from './planQuestions.ts'

const QS: PlanQuestion[] = [
  { id: 'q1', question: 'Extend /items or add /search?', options: [{ label: 'Extend /items', recommended: true }, { label: 'New /search' }] },
  { id: 'q2', question: 'Which DB?', options: [] },
]

function run(actions: StepperAction[], qs = QS): StepperState {
  return actions.reduce((s, a) => stepperReducer(s, a, qs), initialStepper())
}

describe('stripPlanQuestionsFence', () => {
  const fence = '```plan_questions\n{"questions":[{"question":"Q?"}]}\n```'
  it('removes a complete fence', () => {
    assert.equal(stripPlanQuestionsFence(`One quick thing.\n\n${fence}`), 'One quick thing.')
  })
  it('keeps an unterminated fence when not streaming, hides it when streaming', () => {
    const partial = 'Hi\n```plan_questions\n{"questions":[{"que'
    assert.equal(stripPlanQuestionsFence(partial), partial)
    assert.equal(stripPlanQuestionsFence(partial, true), 'Hi')
  })
  it('leaves other fences and empty text alone', () => {
    assert.equal(stripPlanQuestionsFence('```ts\nx\n```'), '```ts\nx\n```')
    assert.equal(stripPlanQuestionsFence(''), '')
  })
})

describe('normalizeQuestions', () => {
  it('rejects non-arrays and empty results', () => {
    assert.equal(normalizeQuestions(null), null)
    assert.equal(normalizeQuestions({}), null)
    assert.equal(normalizeQuestions([{ question: '  ' }, 'x']), null)
  })
  it('caps to 3 questions and 2 options and fills ids', () => {
    const raw = Array.from({ length: 5 }, (_, i) => ({
      question: `Q${i}?`,
      options: [{ label: 'a' }, { label: 'b' }, { label: 'c' }],
    }))
    const out = normalizeQuestions(raw)!
    assert.equal(out.length, 3)
    assert.deepEqual(out.map((q) => q.id), ['q1', 'q2', 'q3'])
    assert.ok(out.every((q) => q.options.length === 2))
  })
})

describe('formatAnswers', () => {
  it('returns the single skip line when everything is skipped', () => {
    assert.match(formatAnswers(QS, {}), /^I'm skipping the clarifying questions\./)
    assert.match(formatAnswers(QS, { q1: { id: 'q1', kind: 'skipped' } }), /^I'm skipping/)
  })
  it('formats mixed option, custom and skipped answers', () => {
    const text = formatAnswers(QS, { q1: { id: 'q1', kind: 'option', value: 'Extend /items' } })
    assert.equal(
      text,
      'Answers to your clarifying questions:\n' +
        '1. Extend /items or add /search? -> Extend /items\n' +
        '2. Which DB? -> (skipped: use your best judgment and list the assumption)',
    )
    const custom = formatAnswers(QS, { q2: { id: 'q2', kind: 'custom', value: '  Postgres  ' } })
    assert.match(custom, /2\. Which DB\? -> Postgres$/)
  })
  it('caps custom answers', () => {
    const long = 'x'.repeat(MAX_CUSTOM_ANSWER_CHARS + 100)
    const text = formatAnswers(QS, { q2: { id: 'q2', kind: 'custom', value: long } })
    const line = text.split('\n')[2]
    assert.equal(line.split(' -> ')[1].length, MAX_CUSTOM_ANSWER_CHARS)
  })
  it('completeAnswers fills gaps with skipped', () => {
    assert.deepEqual(completeAnswers(QS, {}).map((a) => a.kind), ['skipped', 'skipped'])
  })
})

describe('stepperReducer', () => {
  it('select records the option label without advancing', () => {
    const s = run([{ type: 'select', optionIndex: 1 }])
    assert.equal(s.index, 0)
    assert.deepEqual(s.answers.q1, { id: 'q1', kind: 'option', value: 'New /search' })
  })
  it('ignores out-of-range option index', () => {
    assert.deepEqual(run([{ type: 'select', optionIndex: 5 }]).answers, {})
  })
  it('custom text replaces the option, and clearing it removes the answer', () => {
    let s = run([{ type: 'select', optionIndex: 0 }, { type: 'custom', text: 'my own' }])
    assert.equal(s.answers.q1?.kind, 'custom')
    s = stepperReducer(s, { type: 'custom', text: '   ' }, QS)
    assert.equal(s.answers.q1, undefined)
  })
  it('skip marks skipped and advances, clamped at the last question', () => {
    let s = run([{ type: 'skip' }])
    assert.equal(s.index, 1)
    assert.equal(s.answers.q1?.kind, 'skipped')
    assert.ok(isLastQuestion(s, QS))
    s = stepperReducer(s, { type: 'skip' }, QS)
    assert.equal(s.index, 1)
  })
  it('back and next clamp at both ends', () => {
    assert.equal(run([{ type: 'back' }]).index, 0)
    assert.equal(run([{ type: 'next' }, { type: 'next' }, { type: 'next' }]).index, 1)
    assert.equal(run([{ type: 'next' }, { type: 'back' }]).index, 0)
  })
  it('skipAll clears answers and sets the flag', () => {
    const s = run([{ type: 'select', optionIndex: 0 }, { type: 'skipAll' }])
    assert.equal(s.skippedAll, true)
    assert.deepEqual(s.answers, {})
  })
  it('is a no-op with no questions', () => {
    const s = stepperReducer(initialStepper(), { type: 'next' }, [])
    assert.equal(s.index, 0)
  })
})
