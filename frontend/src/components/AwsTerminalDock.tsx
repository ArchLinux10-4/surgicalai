import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { api } from '../api/client'
import { useAppStore } from '../stores/appStore'
import { AwsApprovalCard, AwsLevelPill } from './AwsApprovalCard'

/** Join pasted shell continuations. Mirrors backend normalize_aws_command. */
function normalizeAwsCommand(command: string): string {
  const text = command.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  let out = ''
  let quote = ''
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (quote) {
      out += ch
      if (ch === quote) quote = ''
      continue
    }
    if (ch === "'" || ch === '"') {
      quote = ch
      out += ch
      continue
    }
    if (ch === '\\') {
      const nxt = i + 1 < text.length ? text[i + 1] : ''
      if (nxt === '\n' || nxt === '' || nxt === ' ' || nxt === '\t') {
        i += 1
        while (i < text.length && (text[i] === ' ' || text[i] === '\t' || text[i] === '\n')) i += 1
        i -= 1
        out += ' '
        continue
      }
    }
    out += ch
  }
  return out.trim()
}

const HEIGHT_KEY = 'surgicalai:awsTerminalHeight'
const COLLAPSED_KEY = 'surgicalai:awsTerminalCollapsed'

function loadHeight(): number | null {
  try {
    const n = Number(localStorage.getItem(HEIGHT_KEY))
    if (Number.isFinite(n) && n >= 140 && n <= 2000) return n
  } catch { /* private browsing */ }
  return null
}

function loadCollapsed(): boolean {
  try {
    const v = localStorage.getItem(COLLAPSED_KEY)
    if (v === '1') return true
    if (v === '0') return false
  } catch { /* private browsing */ }
  return false
}

function clampHeight(px: number, parentH: number, compact: boolean) {
  const max = compact
    ? Math.min(parentH * 0.5, window.innerHeight * 0.4)
    : parentH * 0.45
  const cap = Math.max(140, max)
  return Math.min(cap, Math.max(140, px))
}

function deniedLine(stderr: string): string | null {
  if (!/AccessDenied|UnauthorizedOperation|not authorized to perform/i.test(stderr)) return null
  const action = stderr.match(/([a-z0-9-]+:[A-Za-z]+)/)
  return action
    ? `No permission for ${action[1]}. Skipped.`
    : 'No permission for this call. Skipped.'
}

export function AwsTerminalDock({ compact = false }: { compact?: boolean }) {
  const activeSessions = useAppStore(s => s.activeSessions)
  const sidebarTab = useAppStore(s => s.sidebarTab)
  const lines = useAppStore(s => s.awsTerminalLines)
  const push = useAppStore(s => s.pushAwsTerminalLine)
  const awsApproval = useAppStore(s => s.awsApproval)
  const setAwsApproval = useAppStore(s => s.setAwsApproval)
  const awsOpen = sidebarTab === 'aws'
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const visible = lines.filter(l => !activeSessions || l.sessionId === activeSessions)
  const approval = awsApproval && (!activeSessions || awsApproval.sessionId === activeSessions)
    ? awsApproval
    : null
  const [collapsed, setCollapsed] = useState(loadCollapsed)
  const [height, setHeight] = useState(() => loadHeight() ?? 240)
  const rootRef = useRef<HTMLDivElement>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const stuckRef = useRef(true)
  const prevLen = useRef(visible.length)

  useEffect(() => {
    if (visible.length > prevLen.current) setCollapsed(false)
    prevLen.current = visible.length
  }, [visible.length])

  const prevTab = useRef(sidebarTab)
  useEffect(() => {
    if (sidebarTab === 'aws' && prevTab.current !== 'aws') setCollapsed(false)
    prevTab.current = sidebarTab
  }, [sidebarTab])

  useEffect(() => {
    if (approval) setCollapsed(false)
  }, [approval])

  const run = async () => {
    const typed = normalizeAwsCommand(draft)
    if (!typed || busy) return
    const command = typed.startsWith('aws ') ? typed : `aws ${typed}`
    setDraft('')
    setCollapsed(false)
    setBusy(true)
    try {
      const out = await api.aws.exec(command)
      push({
        sessionId: activeSessions || '',
        command,
        stdout: out.stdout || '',
        stderr: out.stderr || '',
        exit_code: out.exit_code ?? 1,
      })
    } catch (e: any) {
      push({
        sessionId: activeSessions || '',
        command,
        stdout: '',
        stderr: e?.message || 'Command failed',
        exit_code: 1,
      })
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    const parentH = rootRef.current?.parentElement?.clientHeight || window.innerHeight
    setHeight(h => clampHeight(loadHeight() ?? h, parentH, compact))
  }, [compact])

  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const onScroll = () => {
      stuckRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [collapsed, visible.length])

  useEffect(() => {
    const el = scrollerRef.current
    if (!el || !stuckRef.current) return
    el.scrollTop = el.scrollHeight
  }, [visible, collapsed])

  if (!visible.length && !approval && !awsOpen) return null

  const persistCollapsed = (next: boolean) => {
    setCollapsed(next)
    try { localStorage.setItem(COLLAPSED_KEY, next ? '1' : '0') } catch { /* ignore */ }
  }

  const last = visible[visible.length - 1]

  if (collapsed) {
    return (
      <div className="flex-shrink-0 border-t border-border bg-[#100808]">
        <button
          type="button"
          onClick={() => persistCollapsed(false)}
          className="h-7 w-full flex items-center gap-2 px-3 text-left"
        >
          <span className="text-[10px] font-bold uppercase tracking-wide text-[#d6d6d6]">AWS</span>
          <span className="text-[11px] font-mono text-accent truncate">{last ? `$ ${last.command}` : 'Waiting for approval'}</span>
          <span className="ml-auto text-[10px] text-muted">Show</span>
        </button>
        {approval && (
          <AwsApprovalCard
            key={`${approval.command}\n${approval.title || ''}`}
            command={approval.command}
            level={approval.level}
            title={approval.title}
            target={approval.target}
            onDecide={(action, command) => {
              approval.reply({ action, command })
              setAwsApproval(null)
            }}
          />
        )}
      </div>
    )
  }

  const onResizeDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault()
    const handle = e.currentTarget
    handle.setPointerCapture(e.pointerId)
    const startY = e.clientY
    const startH = rootRef.current?.getBoundingClientRect().height || height
    const move = (ev: PointerEvent) => {
      const parentH = rootRef.current?.parentElement?.clientHeight || window.innerHeight
      setHeight(clampHeight(startH + (startY - ev.clientY), parentH, compact))
    }
    const up = (ev: PointerEvent) => {
      handle.releasePointerCapture(ev.pointerId)
      handle.removeEventListener('pointermove', move)
      handle.removeEventListener('pointerup', up)
      const parentH = rootRef.current?.parentElement?.clientHeight || window.innerHeight
      const next = clampHeight(startH + (startY - ev.clientY), parentH, compact)
      setHeight(next)
      try { localStorage.setItem(HEIGHT_KEY, String(Math.round(next))) } catch { /* ignore */ }
    }
    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', up)
  }

  return (
    <div
      ref={rootRef}
      className="relative flex flex-col flex-shrink-0 min-h-0 border-t border-border bg-[#100808]"
      style={{ height }}
    >
      <div
        onPointerDown={onResizeDown}
        className="absolute top-0 left-0 right-0 h-1.5 -translate-y-1/2 cursor-row-resize z-10 touch-none hover:bg-accent/40"
        title="Drag to resize the terminal"
      />
      <div className="flex items-center gap-2 px-3 h-7 shrink-0 border-b border-white/10">
        <span className="text-[10px] font-bold uppercase tracking-wide text-[#d6d6d6]">AWS</span>
        <button
          type="button"
          onClick={() => persistCollapsed(true)}
          className="ml-auto text-[10px] text-muted hover:text-[#d6d6d6]"
        >
          Hide
        </button>
      </div>
      <div
        ref={scrollerRef}
        className="flex-1 min-h-[120px] overflow-auto overscroll-contain font-mono text-[12px] text-[#d6d6d6]"
      >
        {!visible.length && (
          <div className="px-3 py-2 text-[12px] text-muted">
            Output scrolls sideways. Type a command on the line below.
          </div>
        )}
        {visible.map((line, i) => {
          const skipped = deniedLine(line.stderr || '')
          const files = (line.files || []).map(f => `${f.name}:\n${f.content}`).join('\n')
          return (
            <div key={i} className="min-w-max px-3 py-1.5 border-b border-white/5">
              <div className="flex items-start gap-2">
                <AwsLevelPill level={line.level} />
                <span className="text-accent">$ {line.command}</span>
              </div>
              {skipped ? (
                <pre className="whitespace-pre text-muted">{skipped}</pre>
              ) : (
                <>
                  {line.stdout ? <pre className="whitespace-pre">{line.stdout}</pre> : null}
                  {line.stderr ? <pre className="whitespace-pre text-danger">{line.stderr}</pre> : null}
                  {files ? <pre className="whitespace-pre">{files}</pre> : null}
                </>
              )}
            </div>
          )
        })}
      </div>
      {approval && (
        <AwsApprovalCard
          key={`${approval.command}\n${approval.title || ''}`}
          command={approval.command}
          level={approval.level}
          title={approval.title}
          target={approval.target}
          onDecide={(action, command) => {
            approval.reply({ action, command })
            setAwsApproval(null)
          }}
        />
      )}
      <form
        className="flex items-center gap-2 h-9 shrink-0 border-t border-white/10 px-3"
        onSubmit={(e) => { e.preventDefault(); void run() }}
      >
        <span className="text-accent font-mono text-[12px]">$</span>
        <input
          className="flex-1 min-w-0 bg-transparent font-mono text-[12px] text-[#d6d6d6] outline-none placeholder:text-muted"
          placeholder="s3 ls"
          value={draft}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          onPaste={(e) => {
            const text = e.clipboardData.getData('text')
            if (!/[\n\r\\]/.test(text)) return
            e.preventDefault()
            const el = e.currentTarget
            const start = el.selectionStart ?? draft.length
            const end = el.selectionEnd ?? draft.length
            setDraft(normalizeAwsCommand(draft.slice(0, start) + text + draft.slice(end)))
          }}
        />
      </form>
    </div>
  )
}
