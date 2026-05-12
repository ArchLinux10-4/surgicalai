import React, { useState, useEffect } from 'react'
import { useToastStore } from '../lib/toast'
import { Cancel, CheckCircle, Close, Info, Warning } from '@mui/icons-material';

const ICONS = {
  success: <CheckCircle sx={{ fontSize: 16 }} className="text-success flex-shrink-0" />,
  error:   <Cancel    sx={{ fontSize: 16 }} className="text-danger  flex-shrink-0" />,
  info:    <Info       sx={{ fontSize: 16 }} className="text-accent  flex-shrink-0" />,
  warning: <Warning sx={{ fontSize: 16 }} className="text-warning flex-shrink-0" />,
}
const BORDER = {
  success: 'border-success/30',
  error:   'border-danger/30',
  info:    'border-accent/30',
  warning: 'border-warning/30',
}

function ToastItem({ id, type, title, message }: { id: string; type: string; title: string; message?: string }) {
  const remove = useToastStore((s) => s.remove)
  const [exiting, setExiting] = useState(false)

  const dismiss = () => {
    setExiting(true)
    setTimeout(() => remove(id), 200)
  }

  return (
    <div
      className={`toast-${exiting ? 'exit' : 'enter'} flex items-start gap-3 bg-overlay border ${BORDER[type as keyof typeof BORDER]} rounded-xl px-4 py-3 shadow-modal min-w-[280px] max-w-[380px]`}
    >
      {ICONS[type as keyof typeof ICONS]}
      <div className="flex-1 min-w-0">
        <div className="text-sm font-semibold text-ink leading-tight">{title}</div>
        {message && <div className="text-xs text-muted mt-0.5 leading-relaxed">{message}</div>}
      </div>
      <button onClick={dismiss} className="btn-icon w-5 h-5 flex-shrink-0 -mr-1 -mt-0.5">
        <Close sx={{ fontSize: 12 }} />
      </button>
    </div>
  )
}

export function Toaster() {
  const toasts = useToastStore((s) => s.toasts)
  return (
    <div className="fixed bottom-5 right-5 z-[9999] flex flex-col gap-2 items-end pointer-events-none">
      {toasts.map((t) => (
        <div key={t.id} className="pointer-events-auto">
          <ToastItem {...t} />
        </div>
      ))}
    </div>
  )
}
