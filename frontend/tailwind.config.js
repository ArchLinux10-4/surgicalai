/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Surfaces
        base:     '#0d1117',
        surface:  '#161b22',
        overlay:  '#1c2128',
        // Borders
        border:   '#30363d',
        'border-sub': '#21262d',
        // Text
        ink:      '#e6edf3',
        muted:    '#8b949e',
        faint:    '#484f58',
        // Accents
        accent:   '#58a6ff',
        'accent-dark': '#1f6feb',
        success:  '#3fb950',
        warning:  '#d29922',
        danger:   '#f85149',
        purple:   '#bc8cff',
        orange:   '#f0883e',
      },
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['"JetBrains Mono"', '"Fira Code"', '"Cascadia Code"', 'Consolas', 'monospace'],
      },
      boxShadow: {
        'soft': '0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.4)',
        'modal': '0 24px 48px rgba(0,0,0,0.6), 0 8px 16px rgba(0,0,0,0.4)',
        'glow-accent': '0 0 0 3px rgba(88, 166, 255, 0.15)',
        'glow-success': '0 0 0 3px rgba(63, 185, 80, 0.15)',
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
