/** Large pastes become pills. The model still receives the full text.

The wire format is length-prefixed so a paste that happens to contain a
marker cannot split the block or leak into the typed text. */
export const PASTE_MIN_CHARS = 1500
export const PASTE_MIN_LINES = 15
export const PASTE_WARN_CHARS = 200_000
export const PASTE_VIEW_CAP = 300_000

const OPEN_PREFIX = '<!--PASTED_BLOCK '
const CLOSE = '<!--/PASTED_BLOCK-->'

export interface PastedBlock {
  id: string
  text: string
}

export interface PastedStats {
  lines: number
  bytes: number
  tokens: number
}

export function newPastedId(): string {
  return `p${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
}

export function shouldPillPaste(text: string): boolean {
  if (!text) return false
  const lines = text.split(/\r\n|\n|\r/).length
  return text.length >= PASTE_MIN_CHARS || lines >= PASTE_MIN_LINES
}

export function pastedStats(text: string): PastedStats {
  const lines = text.length === 0 ? 0 : text.split(/\r\n|\n|\r/).length
  const bytes = typeof TextEncoder !== 'undefined' ? new TextEncoder().encode(text).length : text.length
  const tokens = Math.max(text.length === 0 ? 0 : 1, Math.round(text.length / 4))
  return { lines, bytes, tokens }
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${kb >= 10 ? Math.round(kb) : kb.toFixed(1)} KB`
  const mb = kb / 1024
  return `${mb >= 10 ? Math.round(mb) : mb.toFixed(1)} MB`
}

export function formatTokens(tokens: number): string {
  if (tokens < 1000) return String(tokens)
  const k = tokens / 1000
  return `${k >= 10 ? Math.round(k) : k.toFixed(1)}k`
}

export function formatPasteMeta(text: string): string {
  const stats = pastedStats(text)
  const lineLabel = `${stats.lines} line${stats.lines === 1 ? '' : 's'}`
  return `${lineLabel} · ${formatBytes(stats.bytes)} · ~${formatTokens(stats.tokens)} tokens`
}

export function pastedTitle(text: string): string {
  const line = text.split(/\r?\n/).map((part) => part.trim()).find(Boolean) || 'Pasted text'
  return line.slice(0, 55)
}

export function pastedTotalChars(blocks: PastedBlock[]): number {
  return blocks.reduce((sum, block) => sum + block.text.length, 0)
}

function openTag(id: string, chars: number): string {
  return `${OPEN_PREFIX}id=${id} chars=${chars}-->`
}

export function serializePastedBlocks(typed: string, blocks: PastedBlock[]): string {
  if (blocks.length === 0) return typed
  const body = blocks.map((block) => `${openTag(block.id, block.text.length)}${block.text}${CLOSE}`).join('')
  const trimmed = typed.trim()
  return trimmed ? `${trimmed}\n\n${body}` : body
}

/** null when a marker is truncated or the length does not match the closer. */
function parseStrict(content: string): { visibleText: string; blocks: PastedBlock[] } | null {
  const blocks: PastedBlock[] = []
  let visible = ''
  let i = 0
  while (i < content.length) {
    const start = content.indexOf(OPEN_PREFIX, i)
    if (start === -1) {
      visible += content.slice(i)
      break
    }
    const headerEnd = content.indexOf('-->', start)
    if (headerEnd === -1) return null
    const header = content.slice(start, headerEnd + 3)
    const match = header.match(/^<!--PASTED_BLOCK id=([A-Za-z0-9_-]+) chars=(\d+)-->$/)
    if (!match) return null
    const len = Number(match[2])
    if (!Number.isFinite(len) || len < 0) return null
    const bodyStart = headerEnd + 3
    const bodyEnd = bodyStart + len
    if (bodyEnd + CLOSE.length > content.length) return null
    if (content.slice(bodyEnd, bodyEnd + CLOSE.length) !== CLOSE) return null
    visible += content.slice(i, start)
    blocks.push({ id: match[1], text: content.slice(bodyStart, bodyEnd) })
    i = bodyEnd + CLOSE.length
  }
  return {
    visibleText: visible.replace(/\s+$/g, '').replace(/^\s+/g, ''),
    blocks,
  }
}

export function parsePastedBlocks(content: string): { visibleText: string; blocks: PastedBlock[] } {
  if (!content || !content.includes(OPEN_PREFIX)) return { visibleText: content || '', blocks: [] }
  return parseStrict(content) ?? { visibleText: content, blocks: [] }
}

/** Typed text plus the pasted bodies, without markers. */
export function stripPastedMarkers(content: string): string {
  const { visibleText, blocks } = parsePastedBlocks(content)
  if (blocks.length === 0) return content
  return [visibleText, ...blocks.map((block) => block.text)].filter(Boolean).join('\n\n')
}

export function messageTitle(content: string): string {
  const { visibleText, blocks } = parsePastedBlocks(content)
  const source = visibleText.trim() || (blocks[0] ? pastedTitle(blocks[0].text) : content)
  return source.replace(/\s+/g, ' ').trim().slice(0, 55) || 'New Chat'
}
