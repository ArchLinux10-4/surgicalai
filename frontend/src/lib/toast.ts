import { create } from 'zustand'

// Global kill-switch: flip to true to re-enable toast popups app-wide.
// All ~140 call sites (toast.success/error/info/warning) and the store
// itself are left fully intact — this single flag just stops `add()`
// from ever pushing a toast into the render queue, so nothing renders.
const TOASTS_ENABLED = false

export type ToastType = 'success' | 'error' | 'info' | 'warning'

export interface Toast {
  id: string
  type: ToastType
  title: string
  message?: string
  duration?: number
}

interface ToastStore {
  toasts: Toast[]
  add: (toast: Omit<Toast, 'id'>) => void
  remove: (id: string) => void
}

export const useToastStore = create<ToastStore>((set) => ({
  toasts: [],
  add: (toast) => {
    if (!TOASTS_ENABLED) return
    const id = Math.random().toString(36).slice(2)
    set((s) => ({ toasts: [...s.toasts, { ...toast, id }] }))
    const dur = toast.duration ?? 4000
    if (dur > 0) {
      setTimeout(() => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), dur)
    }
  },
  remove: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

export const toast = {
  success: (title: string, message?: string) =>
    useToastStore.getState().add({ type: 'success', title, message }),
  error: (title: string, message?: string) =>
    useToastStore.getState().add({ type: 'error', title, message, duration: 6000 }),
  info: (title: string, message?: string) =>
    useToastStore.getState().add({ type: 'info', title, message }),
  warning: (title: string, message?: string) =>
    useToastStore.getState().add({ type: 'warning', title, message }),
}
