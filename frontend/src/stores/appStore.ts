import { create } from 'zustand'
import type { ChatSession, ChatMessage, FileContent, SurgicalAnalysis, AppSettings, FileNode, PromptTemplate, ImpactAnalysis, GitHubTargetState } from '../types'
import { api } from '../api/client'
import type { EditLiveEvent } from '../api/client'
import { clientLog } from '../lib/clientLog'
import { normalizeQuestions } from '../lib/planQuestions'
import type { PlanQuestion } from '../lib/planQuestions'
import { parsePastedBlocks, type PastedBlock } from '../lib/pastedBlocks'

export type LiveEditMode = 'symbol' | 'lines' | 'snippet' | 'new_file' | 'unresolved'

export interface LiveEditCardState {
  id: string
  kind: 'edit' | 'new_file'
  filename: string
  symbol: string
  mode: LiveEditMode
  original: string | null
  originalTruncated: boolean
  oldCode: string
  newCode: string
  status: 'writing' | 'done' | 'dropped'
  reason?: string
  capped: boolean
  startLine: number | null
  endLine: number | null
}

const LIVE_EDIT_MODES = new Set<LiveEditMode>(['symbol', 'lines', 'snippet', 'new_file', 'unresolved'])

function liveEditMode(value: string | undefined): LiveEditMode {
  return value && LIVE_EDIT_MODES.has(value as LiveEditMode) ? value as LiveEditMode : 'unresolved'
}

// Shared across ChatPanel (model picker) and SettingsModal (models tab) so
// that verifying a new provider key (e.g. Grok) — which happens in
// SettingsModal — is reflected immediately in ChatPanel's picker without a
// page reload. Both previously kept their own independent local
// `useState`/`useEffect(fetch, [])` copies with no way to notify each other.
export interface AvailableModel {
  id: string
  name: string
  role: string
  description?: string
  cost?: number
  provider?: string
}

export interface PreviewBundlePayload {
  entry?: string
  entryImport?: string
  files: Record<string, string>
  dependencies?: Record<string, string>
  unresolved?: string[]
  component?: string
}

interface AppState {
  // Settings
  settings: AppSettings | null
  settingsOpen: boolean
  settingsInitialTab: string | null
  setSettings: (s: AppSettings) => void
  setSettingsOpen: (v: boolean) => void
  setSettingsInitialTab: (tab: string | null) => void
  githubTargets: Record<string, GitHubTargetState | undefined>
  setGithubTarget: (sessionId: string, value: GitHubTargetState | undefined) => void

  // Model picker list — shared so any component that adds/verifies a
  // provider key can call refreshModels() and have every consumer
  // (ChatPanel's inline picker, SettingsModal's Models tab) update in sync.
  availableModels: AvailableModel[]
  setAvailableModels: (m: AvailableModel[]) => void
  refreshModels: () => Promise<void>

  // Active file
  activeFile: FileContent | null
  setActiveFile: (f: FileContent | null) => void
  fileTree: FileNode | null
  setFileTree: (t: FileNode | null) => void

  // Chat
  sessions: ChatSession[]
  activeSessions: string | null
  messages: ChatMessage[]
  isLoading: boolean
  setSessions: (s: ChatSession[]) => void
  setActiveSession: (id: string | null) => void
  setMessages: (m: ChatMessage[]) => void
  addMessage: (m: ChatMessage) => void
  setLoading: (v: boolean) => void

  // Surgical
  surgicalAnalysis: SurgicalAnalysis | null
  setSurgicalAnalysis: (a: SurgicalAnalysis | null) => void
  surgicalPanelOpen: boolean
  setSurgicalPanelOpen: (v: boolean) => void

  // UI
  sidebarTab: 'files' | 'sessions' | 'context' | 'github' | 'linear' | 'vercel' | 'aws' | 'railway'
  setSidebarTab: (t: 'files' | 'sessions' | 'context' | 'github' | 'linear' | 'vercel' | 'aws' | 'railway') => void
  awsTerminalLines: {
    sessionId: string
    command: string
    stdout: string
    stderr: string
    exit_code: number
    level?: string
    title?: string
    target?: string
    files?: { name: string; content: string }[]
  }[]
  pushAwsTerminalLine: (line: {
    sessionId: string
    command: string
    stdout: string
    stderr: string
    exit_code: number
    level?: string
    title?: string
    target?: string
    files?: { name: string; content: string }[]
  }) => void
  // Live SSE reply. Not persisted and not cloned — the closure belongs to the
  // stream that asked for approval.
  awsApproval: {
    sessionId: string
    command: string
    level?: string
    title?: string
    target?: string
    reply: (resp: { action: 'approve' | 'reject' | 'edit'; command?: string }) => boolean
  } | null
  setAwsApproval: (value: AppState['awsApproval']) => void
  mcpApproval: {
    sessionId: string
    server: string
    serverName?: string
    tool: string
    arguments: Record<string, unknown>
    reply: (resp: { action: 'approve' | 'reject' }) => boolean
  } | null
  setMcpApproval: (value: AppState['mcpApproval']) => void
  mcpCalls: {
    sessionId: string
    server: string
    tool: string
    ok: boolean
    error?: string
    text?: string
  }[]
  pushMcpCall: (line: AppState['mcpCalls'][number]) => void
  previewCheck: {
    sessionId: string
    checkId: string
    entry?: string
    bundle: PreviewBundlePayload
    baselineBundle?: PreviewBundlePayload | null
    unresolved?: string[]
    reply: (payload: Record<string, unknown>) => boolean
  } | null
  setPreviewCheck: (value: AppState['previewCheck']) => void
  sidebarPanelOpen: boolean
  setSidebarPanelOpen: (open: boolean) => void
  sidebarPinned: boolean
  setSidebarPinned: (pinned: boolean) => void
  imageStudioOpen: boolean
  setImageStudioOpen: (open: boolean) => void
  sendLinearIssue: ((issue: any) => void) | null
  setSendLinearIssue: (fn: ((issue: any) => void) | null) => void
  rightTab: 'editor' | 'diff' | 'git'
  setRightTab: (t: 'editor' | 'diff' | 'git') => void
  workspacePath: string
  setWorkspacePath: (p: string) => void



  // Project memory
  projectMemory: string
  setProjectMemory: (m: string) => void
  memoryPanelOpen: boolean
  setMemoryPanelOpen: (v: boolean) => void

  // Prompt templates
  templates: PromptTemplate[]
  setTemplates: (t: PromptTemplate[]) => void

  // Streaming
  streamingMessage: string
  setStreamingMessage: (m: string) => void
  isStreaming: boolean
  setIsStreaming: (v: boolean) => void
  streamProgress: string
  setStreamProgress: (p: string) => void

  // Per-session streaming state (concurrent sessions)
  streamingSessions: Record<string, { isStreaming: boolean; streamingMessage: string; streamProgress: string }>
  setSessionStreaming: (sessionId: string, isStreaming: boolean) => void
  setSessionStreamingMessage: (sessionId: string, msg: string) => void
  setSessionStreamProgress: (sessionId: string, progress: string) => void
  clearSessionStream: (sessionId: string) => void
  liveEdits: Record<string, Record<string, LiveEditCardState>>
  liveEditOrder: Record<string, string[]>
  applyLiveEdit: (sessionId: string, ev: EditLiveEvent) => void

  // Multi-file
  multiFileMode: boolean
  setMultiFileMode: (v: boolean) => void
  selectedFiles: string[]
  toggleSelectedFile: (path: string) => void
  clearSelectedFiles: () => void

  // Impact analysis
  impactAnalysis: ImpactAnalysis | null
  setImpactAnalysis: (a: ImpactAnalysis | null) => void

  // Session files (per-chat)
  sessionFiles: import('../types').SessionFile[]
  setSessionFiles: (files: import('../types').SessionFile[]) => void
  addSessionFile: (file: import('../types').SessionFile) => void
  removeSessionFile: (fileId: string) => void
  // Shared file filter — keeps the side panel + chat-box tray + mobile in sync
  fileFilter: import('../lib/fileClassify').FileFilter
  setFileFilter: (f: import('../lib/fileClassify').FileFilter) => void

  // Agentic tasks
  agentTasks: import('../types').AgentTask[]
  taskRunId: string | null
  taskPreamble: string
  setAgentTasks: (tasks: import('../types').AgentTask[]) => void
  updateAgentTask: (id: string, patch: Partial<import('../types').AgentTask>) => void
  clearAgentTasks: () => void
  setTaskRunId: (id: string | null) => void
  setTaskPreamble: (s: string) => void

  // Agent phase (dual-agent mission control)
  agentPhase: 'idle' | 'planning' | 'executing' | 'complete'
  setAgentPhase: (phase: 'idle' | 'planning' | 'executing' | 'complete') => void

  // Chat Plan tracker — separate from agentTasks so Agent auto-run/dismiss
  // cannot clobber a Ready plan.
  planTasks: import('../types').PlanTask[]
  planRunId: string | null
  planPhase: import('../types').PlanPhase
  planMarkdown: string
  planTitle: string
  planVersion: number | null
  // Clarifying questions the Plan agent asked on its last turn (live SSE
  // event). Reloads derive the same card from the last message's
  // `_plan_questions`, so this is only the live-stream half.
  pendingPlanQuestions: PlanQuestion[] | null
  clearPendingPlanQuestions: () => void
  setPlanTasks: (tasks: import('../types').PlanTask[]) => void
  setPlanRunId: (id: string | null) => void
  setPlanPhase: (phase: import('../types').PlanPhase) => void
  setPlanDocument: (doc: { markdown?: string; title?: string; version?: number | null }) => void
  applyPlanEvent: (event: any) => void
  clearPlanTracker: () => void

  // Pending chat input — set from sidebar components (e.g. deploy watcher)
  pendingChatInput: string | null
  setPendingChatInput: (msg: string | null) => void

  // Element Picker — full-screen live-view modal + picked-element chips.
  // Chips are additive context above the composer (mirrors SessionFilesTray),
  // never injected into the visible draft text — kept separate so the
  // textarea itself needs zero changes (see PickedElementsTray.tsx).
  elementPickerModalOpen: boolean
  setElementPickerModalOpen: (open: boolean) => void
  pickedElements: PickedElementRef[]
  addPickedElement: (el: PickedElementRef) => void
  removePickedElement: (id: string) => void
  clearPickedElements: () => void

  pastedBySession: Record<string, PastedBlock[]>
  addPastedBlock: (sessionKey: string, block: PastedBlock) => void
  updatePastedBlock: (sessionKey: string, id: string, text: string) => void
  removePastedBlock: (sessionKey: string, id: string) => void
  clearPastedBlocks: (sessionKey: string) => void

  steerQueues: Record<string, SteerQueueItem[]>
  steerPaused: Record<string, boolean>
  enqueueSteer: (sessionId: string, item: SteerQueueItem) => void
  unshiftSteer: (sessionId: string, item: SteerQueueItem) => void
  removeSteer: (sessionId: string, id: string) => void
  moveSteer: (sessionId: string, id: string, dir: -1 | 1) => void
  reorderSteer: (sessionId: string, fromId: string, toId: string) => void
  setSteerPaused: (sessionId: string, paused: boolean) => void
  clearSteerQueue: (sessionId: string) => void
  takeFirstQueued: (sessionId: string) => SteerQueueItem | null
  patchMessage: (id: string, patch: Partial<ChatMessage>) => void
  removeMessage: (id: string) => void
  applySteerFrame: (sessionId: string, frame: { type: string; id?: string; text?: string }) => void
}

export interface SteerQueueItem {
  id: string
  text: string
  pasted: PastedBlock[]
  status: 'queued' | 'sending'
}

export interface PickedElementRef {
  id: string
  tag: string
  elId: string | null
  className: string | null
  text: string
  outerHTML: string
  pageUrl: string | null
}

export const useAppStore = create<AppState>((set) => ({
  settings: null,
  settingsOpen: false,
  settingsInitialTab: null,
  setSettings: (settings) => set({ settings }),
  setSettingsOpen: (settingsOpen) => set({ settingsOpen }),
  setSettingsInitialTab: (settingsInitialTab) => set({ settingsInitialTab }),
  githubTargets: {},
  setGithubTarget: (sessionId, value) => set((state) => {
    const githubTargets = { ...state.githubTargets }
    if (value === undefined) delete githubTargets[sessionId]
    else githubTargets[sessionId] = value
    return { githubTargets }
  }),

  availableModels: [],
  setAvailableModels: (availableModels) => set({ availableModels }),
  refreshModels: async () => {
    try {
      const d: any = await api.settings.getModels()
      const models = d?.models || []
      set({ availableModels: models })
      clientLog('models_refreshed', { count: models.length, ids: models.map((m: any) => m.id) })
    } catch (e: any) {
      clientLog('models_refresh_failed', { error: String(e?.message || e).slice(0, 200) })
    }
  },

  activeFile: null,
  setActiveFile: (activeFile) => set({ activeFile }),
  fileTree: null,
  setFileTree: (fileTree) => set({ fileTree }),

  sessions: [],
  activeSessions: null,
  messages: [],
  isLoading: false,
  setSessions: (sessions) => set({ sessions }),
  setActiveSession: (activeSessions) => set((state) => {
    const entry = activeSessions ? state.streamingSessions[activeSessions] : null
    return {
      activeSessions,
      isStreaming: entry?.isStreaming ?? false,
      streamingMessage: entry?.streamingMessage ?? '',
      streamProgress: entry?.streamProgress ?? '',
    }
  }),
  setMessages: (messages) => set({ messages }),
  addMessage: (m) => set((state) => {
    // Guard: drop messages that belong to a different session (prevents
    // cross-session bleed when the user switches tabs mid-stream)
    if (m.session_id && state.activeSessions && m.session_id !== state.activeSessions) return {}
    return { messages: [...state.messages, m] }
  }),
  setLoading: (isLoading) => set({ isLoading }),

  surgicalAnalysis: null,
  setSurgicalAnalysis: (surgicalAnalysis) => set({ surgicalAnalysis }),
  surgicalPanelOpen: false,
  setSurgicalPanelOpen: (surgicalPanelOpen) => set({ surgicalPanelOpen }),

  sidebarTab: 'sessions',
  awsTerminalLines: [],
  pushAwsTerminalLine: (line) => set((s) => ({
    awsTerminalLines: [...s.awsTerminalLines, line].slice(-40),
  })),
  awsApproval: null,
  setAwsApproval: (awsApproval) => set({ awsApproval }),
  mcpApproval: null,
  setMcpApproval: (mcpApproval) => set({ mcpApproval }),
  mcpCalls: [],
  pushMcpCall: (line) => set((s) => ({ mcpCalls: [...s.mcpCalls, line].slice(-12) })),
  previewCheck: null,
  setPreviewCheck: (previewCheck) => set({ previewCheck }),
  setSidebarTab: (sidebarTab) => set({ sidebarTab }),
  sidebarPanelOpen: false,
  setSidebarPanelOpen: (sidebarPanelOpen) => set({ sidebarPanelOpen }),
  sidebarPinned: (() => {
    try {
      const stored = localStorage.getItem('surgicalai_sidebar_pinned')
      return stored ? JSON.parse(stored) : false
    } catch {
      return false
    }
  })(),
  setSidebarPinned: (sidebarPinned) => {
    try {
      localStorage.setItem('surgicalai_sidebar_pinned', JSON.stringify(sidebarPinned))
    } catch {
      // ignore write errors (e.g. private browsing)
    }
    set({ sidebarPinned })
  },
  imageStudioOpen: false,
  setImageStudioOpen: (imageStudioOpen) => set({ imageStudioOpen }),
  sendLinearIssue: null,
  setSendLinearIssue: (fn) => set({ sendLinearIssue: fn }),
  rightTab: 'editor',
  setRightTab: (rightTab) => set({ rightTab }),
  workspacePath: '',
  setWorkspacePath: (workspacePath) => set({ workspacePath }),



  projectMemory: '',
  setProjectMemory: (projectMemory) => set({ projectMemory }),
  memoryPanelOpen: false,
  setMemoryPanelOpen: (memoryPanelOpen) => set({ memoryPanelOpen }),

  templates: [],
  setTemplates: (templates) => set({ templates }),

  streamingMessage: '',
  setStreamingMessage: (streamingMessage) => set({ streamingMessage }),
  isStreaming: false,
  setIsStreaming: (isStreaming) => set({ isStreaming }),
  streamProgress: '',
  setStreamProgress: (streamProgress) => set({ streamProgress }),

  // Per-session streaming state — enables concurrent sessions
  streamingSessions: {},
  setSessionStreaming: (sessionId, isStreaming) => set((state) => {
    const entry = state.streamingSessions[sessionId] || { isStreaming: false, streamingMessage: '', streamProgress: '' }
    const streamingSessions = { ...state.streamingSessions, [sessionId]: { ...entry, isStreaming } }
    const display = state.activeSessions === sessionId ? { isStreaming } : {}
    return { streamingSessions, ...display }
  }),
  setSessionStreamingMessage: (sessionId, streamingMessage) => set((state) => {
    const entry = state.streamingSessions[sessionId] || { isStreaming: false, streamingMessage: '', streamProgress: '' }
    const streamingSessions = { ...state.streamingSessions, [sessionId]: { ...entry, streamingMessage } }
    const display = state.activeSessions === sessionId ? { streamingMessage } : {}
    return { streamingSessions, ...display }
  }),
  setSessionStreamProgress: (sessionId, streamProgress) => set((state) => {
    const entry = state.streamingSessions[sessionId] || { isStreaming: false, streamingMessage: '', streamProgress: '' }
    const streamingSessions = { ...state.streamingSessions, [sessionId]: { ...entry, streamProgress } }
    const display = state.activeSessions === sessionId ? { streamProgress } : {}
    return { streamingSessions, ...display }
  }),
  clearSessionStream: (sessionId) => set((state) => {
    const { [sessionId]: _, ...rest } = state.streamingSessions
    const { [sessionId]: _edits, ...liveRest } = state.liveEdits
    const { [sessionId]: _order, ...orderRest } = state.liveEditOrder
    const display = state.activeSessions === sessionId
      ? { isStreaming: false, streamingMessage: '', streamProgress: '' }
      : {}
    return { streamingSessions: rest, liveEdits: liveRest, liveEditOrder: orderRest, ...display }
  }),

  liveEdits: {},
  liveEditOrder: {},
  applyLiveEdit: (sessionId, ev) => set((state) => {
    if (!sessionId || !ev?.id) return state
    const prev = state.liveEdits[sessionId] || {}
    const order = state.liveEditOrder[sessionId] || []
    const existing = prev[ev.id]
    if (ev.op !== 'open' && !existing) return state

    let card: LiveEditCardState
    let nextOrder = order
    if (ev.op === 'open') {
      card = {
        id: ev.id,
        kind: ev.kind === 'new_file' ? 'new_file' : 'edit',
        filename: ev.filename || 'file',
        symbol: ev.symbol || '',
        mode: liveEditMode(ev.mode),
        original: ev.original ?? null,
        originalTruncated: !!ev.original_truncated,
        oldCode: '',
        newCode: '',
        status: 'writing',
        capped: false,
        startLine: ev.start_line ?? null,
        endLine: ev.end_line ?? null,
      }
      if (!order.includes(ev.id)) nextOrder = [...order, ev.id]
    } else {
      card = { ...existing }
      if (ev.symbol) card.symbol = ev.symbol
      if (ev.op === 'delta') {
        const text = ev.text || ''
        if (ev.field === 'old_code') card.oldCode += text
        else card.newCode += text
        if (ev.capped) card.capped = true
      } else if (ev.op === 'close') {
        if (card.status !== 'dropped') card.status = 'done'
        if (ev.mode) card.mode = liveEditMode(ev.mode)
        if ('original' in ev) card.original = ev.original ?? null
        card.originalTruncated = !!ev.original_truncated
        if (ev.filename) card.filename = ev.filename
        if (ev.start_line != null) card.startLine = ev.start_line
        if (ev.end_line != null) card.endLine = ev.end_line
      } else if (ev.op === 'discard') {
        card.status = 'dropped'
        card.reason = ev.reason || 'Dropped'
      }
    }
    return {
      liveEdits: { ...state.liveEdits, [sessionId]: { ...prev, [ev.id]: card } },
      liveEditOrder: { ...state.liveEditOrder, [sessionId]: nextOrder },
    }
  }),

  multiFileMode: false,
  setMultiFileMode: (multiFileMode) => set({ multiFileMode }),
  selectedFiles: [],
  toggleSelectedFile: (path) => set((state) => ({
    selectedFiles: state.selectedFiles.includes(path)
      ? state.selectedFiles.filter(p => p !== path)
      : [...state.selectedFiles, path]
  })),
  clearSelectedFiles: () => set({ selectedFiles: [] }),

  impactAnalysis: null,
  setImpactAnalysis: (impactAnalysis) => set({ impactAnalysis }),

  sessionFiles: [],
  setSessionFiles: (sessionFiles) => set({ sessionFiles }),
  addSessionFile: (file) => set((state) => ({ sessionFiles: [...state.sessionFiles.filter(f => f.filename !== file.filename), file] })),
  removeSessionFile: (fileId) => set((state) => ({ sessionFiles: state.sessionFiles.filter(f => f.id !== fileId) })),
  fileFilter: 'all',
  setFileFilter: (fileFilter) => set({ fileFilter }),

  agentTasks: [],
  taskRunId: null,
  taskPreamble: '',
  setAgentTasks: (agentTasks) => set({ agentTasks }),
  updateAgentTask: (id, patch) => set((state) => ({
    agentTasks: state.agentTasks.map(t => t.id === id ? { ...t, ...patch } : t)
  })),
  clearAgentTasks: () => set({ agentTasks: [], taskRunId: null, taskPreamble: '', agentPhase: 'idle' }),
  setTaskRunId: (taskRunId) => set({ taskRunId }),
  setTaskPreamble: (taskPreamble) => set({ taskPreamble }),

  agentPhase: 'idle',
  setAgentPhase: (agentPhase) => set({ agentPhase }),

  planTasks: [],
  planRunId: null,
  planPhase: 'idle',
  planMarkdown: '',
  planTitle: 'Plan',
  planVersion: null,
  pendingPlanQuestions: null,
  clearPendingPlanQuestions: () => set({ pendingPlanQuestions: null }),
  setPlanTasks: (planTasks) => set({ planTasks }),
  setPlanRunId: (planRunId) => set({ planRunId }),
  setPlanPhase: (planPhase) => set({ planPhase }),
  setPlanDocument: (doc) => set((s) => ({
    planMarkdown: doc.markdown !== undefined ? (doc.markdown || '') : s.planMarkdown,
    planTitle: doc.title !== undefined ? (doc.title || 'Plan') : s.planTitle,
    planVersion: doc.version !== undefined ? (doc.version ?? null) : s.planVersion,
  })),
  clearPlanTracker: () => set({
    planTasks: [],
    planRunId: null,
    planPhase: 'idle',
    planMarkdown: '',
    planTitle: 'Plan',
    planVersion: null,
  }),
  applyPlanEvent: (event) => {
    if (!event || typeof event !== 'object') return
    const t = event.type
    if (t === 'plan_step' && event.task_id) {
      set((s) => {
        const planTasks = s.planTasks.map((task) =>
          task.id === event.task_id
            ? { ...task, status: event.status || task.status }
            : task,
        )
        const implementing = planTasks.some((task) => task.status === 'running')
        return {
          planTasks,
          planRunId: event.run_id || s.planRunId,
          planPhase: implementing ? 'implementing' : s.planPhase,
        }
      })
      return
    }
    if (t === 'plan_questions') {
      set({ pendingPlanQuestions: normalizeQuestions(event.questions) })
      return
    }
    const docPatch: Record<string, any> = {}
    if (typeof event.markdown === 'string') docPatch.planMarkdown = event.markdown
    if (typeof event.title === 'string' && event.title) docPatch.planTitle = event.title
    if (event.version != null && Number.isFinite(Number(event.version))) {
      docPatch.planVersion = Number(event.version)
    }
    if (t === 'plan_ready' || t === 'plan_updated' || t === 'plan_coverage' || t === 'plan_failed') {
      set({
        planRunId: event.run_id || null,
        planTasks: Array.isArray(event.tasks) ? event.tasks : [],
        planPhase: event.phase || (t === 'plan_failed' ? 'ready' : 'ready'),
        ...docPatch,
      })
      return
    }
    if (t === 'plan_unchanged' || t === 'plan_locked') {
      set({
        planRunId: event.run_id || null,
        planTasks: Array.isArray(event.tasks) ? event.tasks : [],
        planPhase: event.phase || (t === 'plan_locked' ? 'implementing' : 'ready'),
        ...docPatch,
      })
    }
    // plan_missing: leave the tracker empty / unchanged
  },

  pendingChatInput: null,
  setPendingChatInput: (pendingChatInput) => set({ pendingChatInput }),

  elementPickerModalOpen: false,
  setElementPickerModalOpen: (elementPickerModalOpen) => set({ elementPickerModalOpen }),
  pickedElements: [],
  addPickedElement: (el) => set((s) => (
    s.pickedElements.some(p => p.id === el.id)
      ? s
      : { pickedElements: [...s.pickedElements, el] }
  )),
  removePickedElement: (id) => set((s) => ({ pickedElements: s.pickedElements.filter(p => p.id !== id) })),
  clearPickedElements: () => set({ pickedElements: [] }),

  pastedBySession: {},
  addPastedBlock: (sessionKey, block) => set((s) => {
    const cur = s.pastedBySession[sessionKey] || []
    return { pastedBySession: { ...s.pastedBySession, [sessionKey]: [...cur, block] } }
  }),
  updatePastedBlock: (sessionKey, id, text) => set((s) => ({
    pastedBySession: {
      ...s.pastedBySession,
      [sessionKey]: (s.pastedBySession[sessionKey] || []).map((b) => b.id === id ? { ...b, text } : b),
    },
  })),
  removePastedBlock: (sessionKey, id) => set((s) => ({
    pastedBySession: {
      ...s.pastedBySession,
      [sessionKey]: (s.pastedBySession[sessionKey] || []).filter((b) => b.id !== id),
    },
  })),
  clearPastedBlocks: (sessionKey) => set((s) => ({
    pastedBySession: { ...s.pastedBySession, [sessionKey]: [] },
  })),

  steerQueues: {},
  steerPaused: {},
  enqueueSteer: (sessionId, item) => set((s) => ({
    steerQueues: { ...s.steerQueues, [sessionId]: [...(s.steerQueues[sessionId] || []), item] },
  })),
  unshiftSteer: (sessionId, item) => set((s) => {
    const cur = (s.steerQueues[sessionId] || []).filter((row) => row.id !== item.id)
    return { steerQueues: { ...s.steerQueues, [sessionId]: [item, ...cur] } }
  }),
  removeSteer: (sessionId, id) => set((s) => ({
    steerQueues: { ...s.steerQueues, [sessionId]: (s.steerQueues[sessionId] || []).filter((row) => row.id !== id) },
  })),
  moveSteer: (sessionId, id, dir) => set((s) => {
    const list = [...(s.steerQueues[sessionId] || [])]
    const index = list.findIndex((row) => row.id === id)
    const next = index + dir
    if (index < 0 || next < 0 || next >= list.length) return s
    const [row] = list.splice(index, 1)
    list.splice(next, 0, row)
    return { steerQueues: { ...s.steerQueues, [sessionId]: list } }
  }),
  reorderSteer: (sessionId, fromId, toId) => set((s) => {
    const list = [...(s.steerQueues[sessionId] || [])]
    const from = list.findIndex((row) => row.id === fromId)
    const to = list.findIndex((row) => row.id === toId)
    if (from < 0 || to < 0 || from === to) return s
    const [row] = list.splice(from, 1)
    list.splice(to, 0, row)
    return { steerQueues: { ...s.steerQueues, [sessionId]: list } }
  }),
  setSteerPaused: (sessionId, paused) => set((s) => ({
    steerPaused: { ...s.steerPaused, [sessionId]: paused },
  })),
  clearSteerQueue: (sessionId) => set((s) => ({
    steerQueues: { ...s.steerQueues, [sessionId]: [] },
    steerPaused: { ...s.steerPaused, [sessionId]: false },
  })),
  takeFirstQueued: (sessionId) => {
    let taken: SteerQueueItem | null = null
    set((s) => {
      if (s.steerPaused[sessionId]) return s
      const list = s.steerQueues[sessionId] || []
      const index = list.findIndex((row) => row.status === 'queued')
      if (index < 0) return s
      taken = list[index]
      return {
        steerQueues: {
          ...s.steerQueues,
          [sessionId]: list.filter((_, i) => i !== index),
        },
      }
    })
    return taken
  },
  patchMessage: (id, patch) => set((s) => ({
    messages: s.messages.map((m) => m.id === id ? { ...m, ...patch } : m),
  })),
  removeMessage: (id) => set((s) => ({
    messages: s.messages.filter((m) => m.id !== id),
  })),
  applySteerFrame: (sessionId, frame) => set((s) => {
    if (frame.type === 'steer_applied' && frame.id) {
      return {
        messages: s.messages.map((m) => m.id === frame.id ? { ...m, _steered: true, _steerStatus: 'applied' as const } : m),
      }
    }
    if (frame.type === 'steer_unapplied' && frame.id) {
      const msg = s.messages.find((m) => m.id === frame.id)
      const parsed = parsePastedBlocks(msg?.content || frame.text || '')
      const item: SteerQueueItem = {
        id: frame.id,
        text: parsed.blocks.length ? parsed.visibleText : (msg?.content || frame.text || ''),
        pasted: parsed.blocks,
        status: 'queued',
      }
      const cur = (s.steerQueues[sessionId] || []).filter((row) => row.id !== item.id)
      return {
        messages: s.messages.filter((m) => m.id !== frame.id),
        steerQueues: { ...s.steerQueues, [sessionId]: [item, ...cur] },
      }
    }
    return s
  }),
}))
