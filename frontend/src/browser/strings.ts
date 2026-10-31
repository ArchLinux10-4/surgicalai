export const browserStrings = {
  emptyTitle: 'Open a page',
  emptyBody: 'Enter a URL or start one of your local dev servers.',
  starting: 'Starting the browser…',
  connecting: 'Connecting…',
  navErrorTitle: 'This page did not open',
  retry: 'Retry',
  missingTitle: 'Browser is not installed',
  missingBody: 'Rebuild the local app with ./docker-rebuild-local.sh so Chromium is available.',
  pick: 'Pick',
  pickHint: 'Hover to highlight. Click to add the element to chat. Shift-click keeps picking.',
  console: 'Console',
  inspector: 'Inspect',
  screenshot: 'Add screenshot to chat',
  reloadAfterApply: 'Reload after apply',
  askInChat: 'Ask in chat',
  pickAnother: 'Pick another',
  added: 'Added to chat',
  applyToCode: 'Apply to code',
  resetStyles: 'Reset',
  noToken: 'Pick the element again to edit its live styles.',
}

export const QUICK_URLS = [
  'http://localhost:5173',
  'http://localhost:3000',
  'http://localhost:5000',
  'http://localhost:8080',
]

export const VIEWPORTS = [
  { id: 'responsive', label: 'Responsive', w: 0, h: 0 },
  { id: 'laptop', label: '1280 × 800', w: 1280, h: 800 },
  { id: 'desktop', label: '1024 × 768', w: 1024, h: 768 },
  { id: 'phone', label: 'iPhone 390 × 844', w: 390, h: 844 },
  { id: 'tablet', label: 'iPad 820 × 1180', w: 820, h: 1180 },
]
