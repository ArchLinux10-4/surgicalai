import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { useAppStore } from '../stores/appStore'
import { AwsApprovalCard, AwsLevelPill } from './AwsApprovalCard'

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
  const lines = useAppStore(s => s.awsTerminalLines)
  const awsApproval = useAppStore(s => s.awsApproval)
  const setAwsApproval = useAppStore(s => s.setAwsApproval)
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

  useEffect(() => {
    if (approval) setCollapsed(false)
  }, [approval])

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

  if (!visible.length && !approval) return null

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
    </div>
  )
}
