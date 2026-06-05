import { create } from 'zustand'
import type { ChatSession, ChatMessage, FileContent, SurgicalAnalysis, AppSettings, FileNode, PinnedContext, PromptTemplate, ImpactAnalysis } from '../types'

interface AppState {
  // Settings
  settings: AppSettings | null
  settingsOpen: boolean
  setSettings: (s: AppSettings) => void
  setSettingsOpen: (v: boolean) => void

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
  sidebarTab: 'files' | 'sessions' | 'context' | 'github' | 'linear'
  setSidebarTab: (t: 'files' | 'sessions' | 'context' | 'github' | 'linear') => void
  sidebarPanelOpen: boolean
  setSidebarPanelOpen: (open: boolean) => void
  sendLinearIssue: ((issue: any) => void) | null
  setSendLinearIssue: (fn: ((issue: any) => void) | null) => void
  rightTab: 'editor' | 'diff' | 'git'
  setRightTab: (t: 'editor' | 'diff' | 'git') => void
  workspacePath: string
  setWorkspacePath: (p: string) => void

  // Context pinning
  pins: PinnedContext[]
  setPins: (pins: PinnedContext[]) => void

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
}

export const useAppStore = create<AppState>((set) => ({
  settings: null,
  settingsOpen: false,
  setSettings: (settings) => set({ settings }),
  setSettingsOpen: (settingsOpen) => set({ settingsOpen }),

  activeFile: null,
  setActiveFile: (activeFile) => set({ activeFile }),
  fileTree: null,
  setFileTree: (fileTree) => set({ fileTree }),

  sessions: [],
  activeSessions: null,
  messages: [],
  isLoading: false,
  setSessions: (sessions) => set({ sessions }),
  setActiveSession: (activeSessions) => set({ activeSessions }),
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
  setSidebarTab: (sidebarTab) => set({ sidebarTab }),
  sidebarPanelOpen: false,
  setSidebarPanelOpen: (sidebarPanelOpen) => set({ sidebarPanelOpen }),
  sendLinearIssue: null,
  setSendLinearIssue: (fn) => set({ sendLinearIssue: fn }),
  rightTab: 'editor',
  setRightTab: (rightTab) => set({ rightTab }),
  workspacePath: '',
  setWorkspacePath: (workspacePath) => set({ workspacePath }),

  pins: [],
  setPins: (pins) => set({ pins }),

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
  clearAgentTasks: () => set({ agentTasks: [], taskRunId: null, taskPreamble: '' }),
  setTaskRunId: (taskRunId) => set({ taskRunId }),
  setTaskPreamble: (taskPreamble) => set({ taskPreamble }),
}))
