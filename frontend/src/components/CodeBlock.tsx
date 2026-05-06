import React, { useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { Copy, Check, ChevronDown, ChevronUp, Download } from 'lucide-react'

const COLLAPSE_LINES = 8

const LANG_LABELS: Record<string, string> = {
  python: 'Python', py: 'Python',
  typescript: 'TypeScript', ts: 'TypeScript',
  tsx: 'TSX', jsx: 'JSX',
  javascript: 'JavaScript', js: 'JavaScript',
  go: 'Go', rust: 'Rust', java: 'Java',
  cpp: 'C++', c: 'C', cs: 'C#',
  bash: 'Bash', sh: 'Shell', zsh: 'Shell',
  sql: 'SQL', json: 'JSON', yaml: 'YAML', yml: 'YAML',
  html: 'HTML', css: 'CSS', scss: 'SCSS',
  markdown: 'Markdown', md: 'Markdown',
  dockerfile: 'Dockerfile', toml: 'TOML',
  ruby: 'Ruby', rb: 'Ruby', php: 'PHP',
  kotlin: 'Kotlin', swift: 'Swift',
}

const LANG_COLORS: Record<string, string> = {
  python: 'text-blue-400 bg-blue-400/10 border-blue-400/20',
  typescript: 'text-cyan-400 bg-cyan-400/10 border-cyan-400/20',
  tsx: 'text-cyan-300 bg-cyan-300/10 border-cyan-300/20',
  jsx: 'text-yellow-300 bg-yellow-300/10 border-yellow-300/20',
  javascript: 'text-yellow-400 bg-yellow-400/10 border-yellow-400/20',
  go: 'text-teal-400 bg-teal-400/10 border-teal-400/20',
  rust: 'text-orange-400 bg-orange-400/10 border-orange-400/20',
  bash: 'text-green-400 bg-green-400/10 border-green-400/20',
  sh: 'text-green-400 bg-green-400/10 border-green-400/20',
  sql: 'text-purple-400 bg-purple-400/10 border-purple-400/20',
  json: 'text-zinc-300 bg-zinc-300/10 border-zinc-300/20',
}

interface CodeBlockProps {
  code: string
  language?: string
  filename?: string
}

export function CodeBlock({ code, language = 'text', filename }: CodeBlockProps) {
  const [copied, setCopied] = useState(false)
  const [collapsed, setCollapsed] = useState(true)

  const lines = code.split('\n')
  const isLong = lines.length > COLLAPSE_LINES
  const displayCode = isLong && collapsed ? lines.slice(0, COLLAPSE_LINES).join('\n') : code

  const lang = language.toLowerCase().replace(/^language-/, '')
  const label = LANG_LABELS[lang] || lang.toUpperCase() || 'CODE'
  const colorClass = LANG_COLORS[lang] || 'text-zinc-400 bg-zinc-400/10 border-zinc-400/20'

  const handleCopy = () => {
    navigator.clipboard.writeText(code)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleDownload = () => {
    const ext = lang === 'python' || lang === 'py' ? 'py'
      : lang === 'typescript' || lang === 'ts' ? 'ts'
      : lang === 'tsx' ? 'tsx'
      : lang === 'javascript' || lang === 'js' ? 'js'
      : lang === 'go' ? 'go'
      : lang === 'rust' ? 'rs'
      : lang === 'bash' || lang === 'sh' ? 'sh'
      : lang === 'sql' ? 'sql'
      : lang === 'json' ? 'json'
      : lang === 'yaml' || lang === 'yml' ? 'yaml'
      : 'txt'
    const name = filename || `code.${ext}`
    const blob = new Blob([code], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="my-3 rounded-xl overflow-hidden border border-zinc-700/60 bg-zinc-950 shadow-lg">
      {/* Header bar */}
      <div className="flex items-center justify-between px-3.5 py-2 bg-zinc-800/80 border-b border-zinc-700/60">
        <div className="flex items-center gap-2">
          {/* Traffic lights */}
          <div className="flex gap-1.5">
            <span className="w-3 h-3 rounded-full bg-red-500/60" />
            <span className="w-3 h-3 rounded-full bg-yellow-500/60" />
            <span className="w-3 h-3 rounded-full bg-green-500/60" />
          </div>
          <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${colorClass}`}>
            {label}
          </span>
          {filename && (
            <span className="text-[11px] text-zinc-400 font-mono">{filename}</span>
          )}
          {isLong && (
            <span className="text-[10px] text-zinc-500">{lines.length} lines</span>
          )}
        </div>

        <div className="flex items-center gap-1">
          {/* Download */}
          <button
            onClick={handleDownload}
            className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700/60 transition-colors"
            title="Download file"
          >
            <Download size={12} />
          </button>

          {/* Copy */}
          <button
            onClick={handleCopy}
            className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700/60 transition-colors"
            title="Copy code"
          >
            {copied ? (
              <><Check size={12} className="text-green-400" /><span className="text-green-400">Copied!</span></>
            ) : (
              <><Copy size={12} /><span>Copy</span></>
            )}
          </button>

          {/* Collapse / Expand */}
          {isLong && (
            <button
              onClick={() => setCollapsed(c => !c)}
              className="flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700/60 transition-colors"
              title={collapsed ? 'Expand' : 'Collapse'}
            >
              {collapsed ? <ChevronDown size={12} /> : <ChevronUp size={12} />}
              <span>{collapsed ? `Expand ${lines.length - COLLAPSE_LINES} more` : 'Collapse'}</span>
            </button>
          )}
        </div>
      </div>

      {/* Code area */}
      <div className="relative">
        <SyntaxHighlighter
          language={lang || 'text'}
          style={vscDarkPlus}
          showLineNumbers
          wrapLines
          lineNumberStyle={{
            minWidth: '2.5em',
            paddingRight: '1em',
            color: '#4b5563',
            fontSize: '11px',
            userSelect: 'none',
          }}
          customStyle={{
            margin: 0,
            padding: '1rem',
            background: 'transparent',
            fontSize: '13px',
            lineHeight: '1.6',
            fontFamily: '"JetBrains Mono", "Fira Code", "Cascadia Code", Menlo, monospace',
          }}
        >
          {displayCode}
        </SyntaxHighlighter>

        {/* Fade + expand overlay when collapsed */}
        {isLong && collapsed && (
          <div className="absolute bottom-0 left-0 right-0 h-16 bg-gradient-to-t from-zinc-950 to-transparent pointer-events-none" />
        )}
      </div>

      {/* Expand bar at bottom */}
      {isLong && collapsed && (
        <button
          onClick={() => setCollapsed(false)}
          className="w-full py-2 text-[12px] text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60 transition-colors border-t border-zinc-700/60 flex items-center justify-center gap-1.5"
        >
          <ChevronDown size={13} />
          Show {lines.length - COLLAPSE_LINES} more lines
        </button>
      )}
    </div>
  )
}

/** Drop-in renderer for react-markdown's `code` prop */
export function MarkdownCode({
  inline,
  className,
  children,
  ...props
}: {
  inline?: boolean
  className?: string
  children?: React.ReactNode
}) {
  const match = /language-(\w+)/.exec(className || '')
  const lang = match ? match[1] : ''
  const code = String(children).replace(/\n$/, '')

  if (inline) {
    return (
      <code className="px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-200 text-[12px] font-mono border border-zinc-700/50" {...props}>
        {children}
      </code>
    )
  }

  return <CodeBlock code={code} language={lang} />
}
