export interface ChatMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  message_type?: string
  surgical_data?: string
}

export interface ChatSession {
  id: string
  title: string
  file_path: string | null
  model: string
  created_at: string
  updated_at: string
  message_count: number
}

export interface FileNode {
  name: string
  path: string
  type: 'file' | 'dir'
  children?: FileNode[]
  extension?: string
  size?: number
}

export interface FileContent {
  path: string
  content: string
  language: string
  size: number
  lines: number
}

export interface SymbolInfo {
  name: string
  symbol_type: 'function' | 'method' | 'class' | 'arrow_function' | 'variable'
  start_line: number
  end_line: number
  parent: string | null
  indentation: number
  code: string
  signature: string
  full_path?: string
}

export interface SurgicalChange {
  id: string
  symbol: SymbolInfo
  original_code: string
  new_code: string
  diff: string
  confidence: number
  description: string
  applied: boolean
}

export interface ArchitectPlan {
  summary: string
  targets: Array<{
    symbol_path: string
    change_type: string
    description: string
    new_logic: string
    dependencies: string[]
    confidence: number
  }>
  new_symbols_needed: string[]
  import_changes: string[]
  risks: string[]
}

export interface SurgicalAnalysis {
  session_id: string
  plan: ArchitectPlan
  changes: SurgicalChange[]
  tokens_used: number
}

export interface AppSettings {
  openai_api_key_set: boolean
  anthropic_api_key_set: boolean
  architect_model: string
  surgeon_model: string
  temperature_architect: number
  temperature_surgeon: number
  confidence_threshold: number
  auto_backup: boolean
  theme: string
  font_size: number
  workspace_path: string
}

export interface GitStatus {
  is_repo: boolean
  branch: string | null
  staged: string[]
  unstaged: string[]
  untracked: string[]
}

export interface PinnedContext {
  id: string
  workspace_path: string
  file_path: string
  symbol_path: string | null
  label: string | null
  created_at: string
}

export interface ProjectMemory {
  id: string | null
  workspace_path: string
  content: string
  updated_at?: string
}

export interface PromptTemplate {
  id: string
  name: string
  prompt: string
  mode: 'chat' | 'surgical'
  created_at: string
}

export interface ImpactResult {
  symbol_path: string
  file_path: string
  impact_type: string
  description: string
}

export interface ImpactAnalysis {
  target_symbol: string
  impacts: ImpactResult[]
  risk_level: 'low' | 'medium' | 'high'
  summary: string
}

export interface MultiFileAnalysis {
  session_id: string
  files_analyzed: number
  changes_by_file: Record<string, any>
  overall_summary: string
}

export type StreamChunkType = 'token' | 'done' | 'error' | 'progress' | 'result' | 'thinking_start' | 'thinking' | 'thinking_end'

export interface StreamChunk {
  type: StreamChunkType
  content: string
  metadata?: Record<string, any>
}

export interface SessionFile {
  id: string
  session_id: string
  filename: string
  language: string
  lines: number
  symbol_count: number
  created_at: string
  content?: string  // only present when explicitly fetched
  file_type?: string  // 'code' | 'image' | 'pdf' | 'csv' | 'excel' | 'text'
}

export interface SmartResult {
  intent: 'edit' | 'chat'
  summary: string
  reasoning: string
  risks: string[]
  changes_by_file: Record<string, {
    filename: string
    file_id: string
    changes: SurgicalChange[]
  }>
  skipped_changes?: Record<string, {
    symbol: string
    reason: string
  }[]>
}
