/**
 * Mobile-local chat mode helpers.
 * Keys and mode values MUST stay identical to desktop ChatPanel
 * (sai_chat_mode / sai_web_research_enabled / sai_agent_mode migration).
 * Do not import this from desktop — ChatPanel stays frozen.
 */

export type ChatMode = 'edit' | 'ask' | 'plan' | 'agent'

export const CHAT_MODES: ChatMode[] = ['edit', 'ask', 'plan', 'agent']

/** Labels/descriptions mirror ChatPanel MODE_META (~30–35). */
export const MODE_META: Record<ChatMode, { label: string; desc: string }> = {
  edit:  { label: 'Edit',  desc: 'Code edits with QA review' },
  ask:   { label: 'Ask',   desc: 'Questions & research — no edits' },
  plan:  { label: 'Plan',  desc: 'Implementation plan — no edits' },
  agent: { label: 'Agent', desc: 'Multi-agent task breakdown (Claude)' },
}

export const MODE_COLOR: Record<ChatMode, { text: string; bg: string; border: string; dot: string }> = {
  edit:  { text: 'text-muted/70', bg: 'bg-overlay/60',  border: 'border-border/80', dot: 'bg-muted/70' },
  ask:   { text: 'text-accent',   bg: 'bg-accent/15',   border: 'border-accent/50', dot: 'bg-accent' },
  plan:  { text: 'text-purple',   bg: 'bg-purple/15',   border: 'border-purple/50', dot: 'bg-purple' },
  agent: { text: 'text-orange',   bg: 'bg-orange/15',   border: 'border-orange/50', dot: 'bg-orange' },
}

/** Read at send time so closures never see a stale mode (same as desktop). */
export function readChatMode(): ChatMode {
  try {
    const v = localStorage.getItem('sai_chat_mode')
    if (v === 'edit' || v === 'ask' || v === 'plan' || v === 'agent') return v
    if (localStorage.getItem('sai_agent_mode') === '1') return 'agent'
  } catch { /* storage blocked */ }
  return 'edit'
}

export function writeChatMode(m: ChatMode): void {
  try { localStorage.setItem('sai_chat_mode', m) } catch { /* session-only */ }
}

export function readWebResearch(): boolean {
  try { return localStorage.getItem('sai_web_research_enabled') === '1' } catch { return false }
}

export function writeWebResearch(enabled: boolean): void {
  try { localStorage.setItem('sai_web_research_enabled', enabled ? '1' : '0') } catch { /* session-only */ }
}

/** Offline mirror of ChatPanel isOffline — Qwen/Ollama cannot Plan/Agent. */
export function isOfflineSettings(settings: {
  architect_model?: string
  ollama_enabled?: boolean
  openai_api_key_set?: boolean
} | null | undefined): boolean {
  if (!settings) return false
  return !!(
    settings.architect_model?.startsWith('ollama:') ||
    (settings.ollama_enabled && !settings.openai_api_key_set)
  )
}

export function degradeModeForOffline(mode: ChatMode, offline: boolean): ChatMode {
  if (!offline) return mode
  if (mode === 'agent') return 'edit'
  if (mode === 'plan') return 'ask'
  return mode
}

/** Research is Claude-only (backend); use architect model prefix like production gates. */
export function webResearchAvailableFor(
  settings: { architect_model?: string } | null | undefined,
  offline: boolean,
): boolean {
  if (offline) return false
  return !!settings?.architect_model?.startsWith('claude-')
}
