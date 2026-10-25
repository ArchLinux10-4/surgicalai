import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { useAppStore } from '../stores/appStore'
import { PlanTracker } from './PlanTracker'

const HEIGHT_KEY = 'surgicalai:planPaneHeight'

function loadHeight(): number | null {
  try {
    const n = Number(localStorage.getItem(HEIGHT_KEY))
    if (Number.isFinite(n) && n >= 120 && n <= 2000) return n
  } catch { /* private browsing */ }
  return null
}

function clampHeight(px: number, parentH: number, compact: boolean) {
  const minWanted = compact ? 180 : 220
  const ratioCap = compact ? window.innerHeight * 0.45 : parentH * 0.7
  const max = Math.max(120, Math.min(parentH - 120, ratioCap))
  const min = Math.min(minWanted, max)
  return Math.min(max, Math.max(min, px))
}

export function PlanPane({ compact = false, placement = 'dock' }: { compact?: boolean; placement?: 'dock' | 'tab' }) {
  const planTasks = useAppStore(s => s.planTasks)
  const planPhase = useAppStore(s => s.planPhase)
  const planMarkdown = useAppStore(s => s.planMarkdown)
  const planVersion = useAppStore(s => s.planVersion)
  const hasTasks = planPhase !== 'idle' && planTasks.length > 0
  const hasDoc = !!(planMarkdown || '').trim()
  const [collapsed, setCollapsed] = useState(false)
  const [height, setHeight] = useState(() => loadHeight() ?? 320)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setCollapsed(false)
  }, [planVersion])

  useEffect(() => {
    const parentH = rootRef.current?.parentElement?.clientHeight || window.innerHeight
    setHeight(h => clampHeight(loadHeight() ?? parentH * 0.42, parentH, compact))
  }, [compact, hasTasks, hasDoc])

  if (!hasTasks && !hasDoc) return null
  if (placement === 'tab') {
    return (
      <div className="flex flex-1 min-h-0 flex-col overflow-hidden">
        <PlanTracker docked />
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
      const next = clampHeight(startH + (startY - ev.clientY), parentH, compact)
      setHeight(next)
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
      className="relative flex flex-col flex-shrink-0 border-t border-purple/40 min-h-0 overflow-hidden"
      style={collapsed ? undefined : { height }}
    >
      {!collapsed && (
        <div
          onPointerDown={onResizeDown}
          className="h-1.5 shrink-0 cursor-row-resize touch-none hover:bg-accent/40 bg-purple/20"
          title="Drag to resize the plan"
        />
      )}
      <PlanTracker
        docked
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed(v => !v)}
      />
    </div>
  )
}
