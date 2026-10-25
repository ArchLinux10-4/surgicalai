/**
 * Turn agent text into markdown the chat can render.
 * Strips protocol tags, and while streaming closes an open fence or bold
 * marker so partial output does not flash as raw syntax.
 */
import { stripPlanQuestionsFence } from './planQuestions.ts'

export function stripInternalTags(text: string, streaming = false): string {
  if (!text) return text
  let result = text
    .replace(/<edit_plan>[\s\S]*?<\/edit_plan>/g, '')
    .replace(/<search_request>[\s\S]*?<\/search_request>/g, '')
    .replace(/```(?:implementation_plan|plan-json)[\s\S]*?```/gi, '')
  if (streaming) {
    result = result
      .replace(/<edit_plan>[\s\S]*$/, '')
      .replace(/<search_request>[\s\S]*$/, '')
      .replace(/```(?:implementation_plan|plan-json)[\s\S]*$/, '')
  }
  return stripPlanQuestionsFence(result, streaming)
}

/** Close one trailing ``` or ** that the stream has not finished yet. */
export function closeOpenMarkdown(text: string): string {
  if (!text) return text
  let out = text
  const fences = out.match(/```/g)
  if (fences && fences.length % 2 === 1) {
    out += out.endsWith('\n') ? '```' : '\n```'
  }
  const parts = out.split('```')
  for (let i = 0; i < parts.length; i += 2) {
    const marks = parts[i].match(/\*\*/g)
    if (marks && marks.length % 2 === 1) {
      parts[i] = parts[i] + '**'
      out = parts.join('```')
      break
    }
  }
  return out
}

export function prepareAssistantMarkdown(text: string, opts?: { streaming?: boolean }): string {
  const streaming = !!opts?.streaming
  const stripped = stripInternalTags(text || '', streaming)
  if (!streaming) return stripped
  return closeOpenMarkdown(stripped)
}
