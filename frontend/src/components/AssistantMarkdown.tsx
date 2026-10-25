import { memo, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import { MarkdownCode } from './CodeBlock'
import { prepareAssistantMarkdown } from '../lib/prepareAssistantMarkdown'

export type AssistantDensity = 'chat' | 'compact'

const plugins = [remarkGfm, remarkBreaks]

function useThrottledText(value: string, active: boolean, ms = 80) {
  const [shown, setShown] = useState(value)
  const latest = useRef(value)
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastFlush = useRef(0)
  latest.current = value

  useEffect(() => {
    if (!active) {
      if (pending.current) {
        clearTimeout(pending.current)
        pending.current = null
      }
      setShown(value)
      return
    }
    if (pending.current) return
    const wait = Math.max(0, ms - (Date.now() - lastFlush.current))
    pending.current = setTimeout(() => {
      pending.current = null
      lastFlush.current = Date.now()
      setShown(latest.current)
    }, wait)
  }, [value, active, ms])

  useEffect(() => {
    return () => {
      if (pending.current) clearTimeout(pending.current)
    }
  }, [])

  return shown
}

function markdownComponents(compact: boolean) {
  const body = compact
    ? 'text-[13px] leading-relaxed text-ink'
    : 'text-[15px] leading-[1.65] text-ink'
  return {
    code: (({ className, children, ...props }: any) => (
      <MarkdownCode className={className} {...props}>{children}</MarkdownCode>
    )) as any,
    h1: ({ children }: any) => (
      <h1 className="text-lg font-bold text-ink mt-5 mb-2">{children}</h1>
    ),
    h2: ({ children }: any) => (
      <h2 className="text-base font-bold text-ink mt-4 mb-1.5">{children}</h2>
    ),
    h3: ({ children }: any) => (
      <h3 className="text-sm font-semibold text-ink mt-3 mb-1">{children}</h3>
    ),
    p: ({ children }: any) => (
      <p className={`${body} mb-3 last:mb-0`}>{children}</p>
    ),
    ul: ({ children }: any) => (
      <ul className="my-2 list-disc space-y-1 pl-5 marker:text-accent/80">{children}</ul>
    ),
    ol: ({ children }: any) => (
      <ol className="my-2 list-decimal space-y-1 pl-5 marker:text-muted">{children}</ol>
    ),
    li: ({ children }: any) => (
      <li className={`${body} pl-1 [&>p]:mb-1 [&>p:last-child]:mb-0`}>{children}</li>
    ),
    blockquote: ({ children }: any) => (
      <blockquote className={`my-3 pl-4 border-l-2 border-accent/50 text-muted italic ${body}`}>{children}</blockquote>
    ),
    strong: ({ children }: any) => (
      <strong className="font-semibold text-ink">{children}</strong>
    ),
    em: ({ children }: any) => (
      <em className="italic">{children}</em>
    ),
    a: ({ children, href }: any) => (
      <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent underline underline-offset-2 hover:text-accent/80">{children}</a>
    ),
    hr: () => <hr className="my-4 border-border/50" />,
    table: ({ children }: any) => (
      <div className="my-3 overflow-x-auto rounded-lg border border-border/60">
        <table className="w-full text-sm">{children}</table>
      </div>
    ),
    thead: ({ children }: any) => <thead className="bg-surface/80">{children}</thead>,
    th: ({ children }: any) => (
      <th className="px-3 py-2 text-left text-xs font-semibold text-ink uppercase tracking-wide">{children}</th>
    ),
    td: ({ children }: any) => (
      <td className="px-3 py-2 text-ink text-sm border-t border-border/40">{children}</td>
    ),
  }
}

const MarkdownBody = memo(function MarkdownBody({
  text,
  streaming,
  density,
}: {
  text: string
  streaming: boolean
  density: AssistantDensity
}) {
  const source = prepareAssistantMarkdown(text, { streaming })
  if (!source) return null
  return (
    <ReactMarkdown remarkPlugins={plugins} components={markdownComponents(density === 'compact')}>
      {source}
    </ReactMarkdown>
  )
})

export function AssistantMarkdown({
  text,
  streaming = false,
  density = 'chat',
}: {
  text: string
  streaming?: boolean
  density?: AssistantDensity
}) {
  const live = useThrottledText(text || '', streaming)
  const source = streaming ? live : (text || '')
  if (!source && !streaming) return null
  return (
    <div className={`assistant-prose break-words ${density === 'compact' ? 'assistant-prose-compact' : ''} ${streaming ? 'is-streaming' : ''}`}>
      <MarkdownBody text={source} streaming={streaming} density={density} />
    </div>
  )
}
