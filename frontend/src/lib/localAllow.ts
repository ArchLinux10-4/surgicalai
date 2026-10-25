/** Whether "Always allow" is safe for a local approval. No React. */

export function canAlwaysAllowLocal(level?: string, command?: string): boolean {
  if (level === 'delete' || level === 'protected') return false
  const text = command || ''
  if (/\b(?:bash|zsh|sh)\s+-lc\b/.test(text)) return false
  return level === 'change' || level === 'network'
}
