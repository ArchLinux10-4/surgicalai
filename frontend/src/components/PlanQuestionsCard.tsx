import React, { useState } from 'react'

export type PlanQuestionOption = { id: string; label: string }
export type PlanQuestion = {
  id: string
  prompt: string
  recommendedId: string
  options: PlanQuestionOption[]
}

const FENCE_RE = /```clarifying_questions\s*([\s\S]*?)```/i
const MAX_QUESTIONS = 5
const PROMPT_CAP = 240
const LABEL_CAP = 120

/** Same drop/cap rules as backend parse_clarifying_questions. */
export function parseClarifyingQuestions(text: string): PlanQuestion[] | null {
  if (!text?.trim()) return null
  const m = FENCE_RE.exec(text)
  if (!m) return null
  let payload: unknown
  try {
    payload = JSON.parse(m[1].trim())
  } catch {
    return null
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  const items = (payload as { questions?: unknown }).questions
  if (!Array.isArray(items) || items.length === 0) return null

  const kept: PlanQuestion[] = []
  const seen = new Set<string>()
  for (const item of items) {
    if (kept.length >= MAX_QUESTIONS) break
    if (!item || typeof item !== 'object') continue
    const raw = item as { id?: unknown; prompt?: unknown; recommendedId?: unknown; options?: unknown }
    const id = String(raw.id ?? '').trim()
    const prompt = String(raw.prompt ?? '').trim().slice(0, PROMPT_CAP)
    if (!id || !prompt || seen.has(id)) continue
    if (!Array.isArray(raw.options) || raw.options.length !== 3) continue
    const options: PlanQuestionOption[] = []
    const optionIds = new Set<string>()
    let bad = false
    for (const opt of raw.options) {
      if (!opt || typeof opt !== 'object') { bad = true; break }
      const o = opt as { id?: unknown; label?: unknown }
      const oid = String(o.id ?? '').trim()
      const label = String(o.label ?? '').trim().slice(0, LABEL_CAP)
      if (!oid || !label || optionIds.has(oid)) { bad = true; break }
      optionIds.add(oid)
      options.push({ id: oid, label })
    }
    if (bad) continue
    let recommended = String(raw.recommendedId ?? '').trim()
    if (!optionIds.has(recommended)) recommended = options[0].id
    options.sort((a, b) => (a.id === recommended ? 0 : 1) - (b.id === recommended ? 0 : 1))
    seen.add(id)
    kept.push({ id, prompt, recommendedId: recommended, options })
  }
  return kept.length ? kept : null
}

type Choice = { skip: boolean; optionId: string; other: string }

function answerFor(q: PlanQuestion, choice: Choice | undefined): string {
  if (!choice || choice.skip) return 'Skipped — decide yourself'
  const other = choice.other.trim()
  if (other) return other
  const picked = q.options.find(o => o.id === choice.optionId)
  if (picked) return picked.label
  return 'Skipped — decide yourself'
}

export function PlanQuestionsCard({
  content,
  laterText,
  disabled,
  onContinue,
}: {
  content: string
  laterText: string
  disabled: boolean
  onContinue: (body: string) => void
}) {
  const questions = parseClarifyingQuestions(content)
  const [choices, setChoices] = useState<Record<string, Choice>>({})
  const [sent, setSent] = useState(false)

  if (!questions) return null

  const answered = questions.every(q =>
    laterText.includes('Clarifying answers (Plan):') && laterText.includes(`[${q.id}]`)
  )
  const locked = disabled || answered || sent

  const submit = () => {
    if (locked) return
    const lines = ['Clarifying answers (Plan):']
    questions.forEach((q, i) => {
      lines.push(`${i + 1}. [${q.id}] ${q.prompt}`)
      lines.push(`   Answer: ${answerFor(q, choices[q.id])}`)
    })
    setSent(true)
    onContinue(lines.join('\n'))
  }

  return (
    <div className="mt-3 rounded-xl border border-purple/50 bg-purple/15 px-3 py-3">
      <div className="text-[12px] font-semibold text-ink mb-2">A few choices before the plan</div>
      {questions.map(q => {
        const choice = choices[q.id] || { skip: false, optionId: '', other: '' }
        const name = `plan-q-${q.id}`
        return (
          <fieldset key={q.id} className="mb-3 border-0 p-0" disabled={locked}>
            <legend className="text-[13px] text-ink mb-1.5">{q.prompt}</legend>
            <div className="flex flex-col gap-1">
              {q.options.map(opt => (
                <label key={opt.id} className="flex items-center gap-2 text-[12px] text-ink">
                  <input
                    type="radio"
                    name={name}
                    checked={!choice.skip && choice.optionId === opt.id}
                    onChange={() => setChoices(prev => ({
                      ...prev,
                      [q.id]: { skip: false, optionId: opt.id, other: prev[q.id]?.other || '' },
                    }))}
                  />
                  <span>{opt.label}</span>
                  {opt.id === q.recommendedId && (
                    <span className="text-[10px] font-medium text-purple">Recommended</span>
                  )}
                </label>
              ))}
              <label className="flex items-center gap-2 text-[12px] text-muted">
                <input
                  type="radio"
                  name={name}
                  checked={choice.skip}
                  onChange={() => setChoices(prev => ({
                    ...prev,
                    [q.id]: { skip: true, optionId: '', other: prev[q.id]?.other || '' },
                  }))}
                />
                <span>Skip</span>
              </label>
              <input
                type="text"
                value={choice.other}
                disabled={locked || choice.skip}
                placeholder="Or type your own answer"
                className="mt-1 w-full rounded-lg border border-border bg-surface px-2 py-1 text-[12px] text-ink placeholder:text-muted"
                onChange={e => setChoices(prev => ({
                  ...prev,
                  [q.id]: { skip: false, optionId: prev[q.id]?.optionId || '', other: e.target.value },
                }))}
              />
            </div>
          </fieldset>
        )
      })}
      {locked ? (
        <div className="text-[11px] text-muted">{answered || sent ? 'Answers sent' : 'Wait for the current reply to finish'}</div>
      ) : (
        <button
          type="button"
          onClick={submit}
          className="mt-1 rounded-lg bg-purple/15 border border-purple/50 px-3 py-1.5 text-[12px] font-medium text-ink"
        >
          Continue
        </button>
      )}
    </div>
  )
}
