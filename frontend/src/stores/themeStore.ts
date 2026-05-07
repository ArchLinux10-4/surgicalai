/**
 * Theme store — manages dark/light theme preference.
 * Persists per-user in localStorage, applies data-theme on <html>.
 */
import { create } from 'zustand'

type Theme = 'dark' | 'light'

interface ThemeState {
  theme: Theme
  setTheme: (t: Theme) => void
  toggleTheme: () => void
}

function getStorageKey(): string {
  // Namespace by username if available
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key && key.startsWith('surgicalai-auth-') && key !== 'surgicalai-auth') {
        const username = key.replace('surgicalai-auth-', '')
        return `surgicalai-theme-${username}`
      }
    }
  } catch {}
  return 'surgicalai-theme'
}

function loadTheme(): Theme {
  try {
    const saved = localStorage.getItem(getStorageKey())
    if (saved === 'light' || saved === 'dark') return saved
  } catch {}
  return 'dark'
}

function applyTheme(theme: Theme) {
  document.documentElement.setAttribute('data-theme', theme)
  localStorage.setItem(getStorageKey(), theme)
}

// Initialize on load
const initialTheme = loadTheme()
applyTheme(initialTheme)

export const useThemeStore = create<ThemeState>((set) => ({
  theme: initialTheme,
  setTheme: (t) => {
    applyTheme(t)
    set({ theme: t })
  },
  toggleTheme: () => {
    set((state) => {
      const next = state.theme === 'dark' ? 'light' : 'dark'
      applyTheme(next)
      return { theme: next }
    })
  },
}))
