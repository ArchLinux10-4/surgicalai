import { create } from 'zustand'

/* ── Types ─────────────────────────────────────────────────────── */

export interface PickedElement {
  index: number
  tag: string
  id: string | null
  classes: string[]
  selector: string
  text: string       // first ~120 chars of textContent
  html: string       // truncated outerHTML
  styles: Record<string, string>
}

interface ElementPickerState {
  pickMode: boolean
  selectedElements: PickedElement[]

  setPickMode: (on: boolean) => void
  addElement: (el: PickedElement) => void
  removeElement: (index: number) => void
  clearElements: () => void

  /** Format all selected elements as a text block for the chat prompt. */
  getFormattedContext: () => string
}

/* ── Store ──────────────────────────────────────────────────────── */

export const useElementPickerStore = create<ElementPickerState>((set, get) => ({
  pickMode: false,
  selectedElements: [],

  setPickMode: (on) => set({ pickMode: on }),

  addElement: (el) =>
    set((s) => ({ selectedElements: [...s.selectedElements, el] })),

  removeElement: (index) =>
    set((s) => ({
      selectedElements: s.selectedElements.filter((e) => e.index !== index),
    })),

  clearElements: () => set({ selectedElements: [] }),

  getFormattedContext: () => {
    const els = get().selectedElements
    if (!els.length) return ''

    const lines = els.map((el, i) => {
      const parts: string[] = [`Element ${i + 1}: <${el.tag}>`]
      if (el.id) parts.push(`  id="${el.id}"`)
      if (el.classes.length) parts.push(`  class="${el.classes.join(' ')}"`)
      parts.push(`  selector: ${el.selector}`)
      if (el.text) parts.push(`  text: "${el.text}"`)
      parts.push(`  html: ${el.html}`)
      const styleStr = Object.entries(el.styles)
        .filter(([, v]) => v && v !== 'none' && v !== 'rgba(0, 0, 0, 0)')
        .map(([k, v]) => `${k}: ${v}`)
        .join('; ')
      if (styleStr) parts.push(`  styles: ${styleStr}`)
      return parts.join('\n')
    })
    return lines.join('\n\n')
  },
}))
