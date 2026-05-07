/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        base:     'rgb(var(--c-base) / <alpha-value>)',
        surface:  'rgb(var(--c-surface) / <alpha-value>)',
        overlay:  'rgb(var(--c-overlay) / <alpha-value>)',
        border:   'rgb(var(--c-border) / <alpha-value>)',
        'border-sub': 'rgb(var(--c-border-sub) / <alpha-value>)',
        ink:      'rgb(var(--c-ink) / <alpha-value>)',
        muted:    'rgb(var(--c-muted) / <alpha-value>)',
        faint:    'rgb(var(--c-faint) / <alpha-value>)',
        accent:   'rgb(var(--c-accent) / <alpha-value>)',
        'accent-dark': 'rgb(var(--c-accent-dark) / <alpha-value>)',
        success:  'rgb(var(--c-success) / <alpha-value>)',
        warning:  'rgb(var(--c-warning) / <alpha-value>)',
        danger:   'rgb(var(--c-danger) / <alpha-value>)',
        purple:   'rgb(var(--c-purple) / <alpha-value>)',
        orange:   'rgb(var(--c-orange) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"Fira Code"', '"Cascadia Code"', 'Consolas', 'monospace'],
      },
      boxShadow: {
        'soft': '0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.4)',
        'modal': '0 24px 48px rgba(0,0,0,0.6), 0 8px 16px rgba(0,0,0,0.4)',
        'glow-accent': '0 0 0 3px rgb(var(--c-accent) / 0.15)',
        'glow-success': '0 0 0 3px rgb(var(--c-success) / 0.15)',
      },
      animation: {
        'fade-in': 'fadeIn 0.15s ease-out',
        'slide-up': 'slideUp 0.2s ease-out',
        'slide-down': 'slideDown 0.2s ease-out',
        'spin-slow': 'spin 2s linear infinite',
        'blink': 'blink 1s step-end infinite',
        'pulse-soft': 'pulseSoft 2s ease-in-out infinite',
      },
      keyframes: {
        fadeIn: { from: { opacity: 0 }, to: { opacity: 1 } },
        slideUp: { from: { opacity: 0, transform: 'translateY(8px)' }, to: { opacity: 1, transform: 'translateY(0)' } },
        slideDown: { from: { opacity: 0, transform: 'translateY(-8px)' }, to: { opacity: 1, transform: 'translateY(0)' } },
        blink: { '0%, 100%': { opacity: 1 }, '50%': { opacity: 0 } },
        pulseSoft: { '0%, 100%': { opacity: 1 }, '50%': { opacity: 0.6 } },
      },
    },
  },
  plugins: [],
}
