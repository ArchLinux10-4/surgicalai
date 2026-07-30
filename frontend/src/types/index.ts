export interface ChatMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
  message_type?: string
  surgical_data?: string
  _thinking?: string
  _steps?: string[]
  _model?: string
  _aborted?: boolean
  compact_summary?: string
  compact_count?: number
  compact_kept?: number
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

export interface RiskVerdict {
  risk: string
  status: 'verified_safe' | 'warning' | 'blocked'
  reason: string
}

export interface QAResult {
  verdict: 'safe' | 'warning' | 'blocked' | 'skipped'
  qa_score: number | null
  summary: string
  import_issues: string[]
  downstream_risks: string[]
  type_errors: string[]
  // logic_errors: mental-tracing issues found by the QA reviewer. Backend has
  // always computed this (models/schemas.py) but it was missing from this
  // type and silently dropped by every UI surface that reads QAResult
  // fields — fixed alongside the retry-gate hardening (trace 414dfaef).
  logic_errors?: string[]
  plan_deviation: string
  skipped_reason?: string | null
  risk_verdicts?: RiskVerdict[]
  // hard_blocked: true when the FINAL QA/tsc gate — after every auto-retry —
  // still shows a real `blocked` verdict (confirmed compile errors, or a
  // genuine blocked semantic verdict the retry loop couldn't resolve).
  // Distinct from a routine soft/borderline advisory. Drives the louder
  // warning + "Retry with QA" button on the diff card.
  hard_blocked?: boolean
  // regression_detected: true when an auto-correction round made things
  // worse (more real compile errors) than the version before it, and the
  // pipeline reverted that round's attempt rather than keeping it.
  regression_detected?: boolean
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
  surgeon_notes?: string[]
  qa_result?: QAResult
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
  ollama_enabled?: boolean
  architect_model: string
  surgeon_model: string
  temperature_architect: number
  temperature_surgeon: number
  confidence_threshold: number
  auto_backup: boolean
  theme: string
  font_size: number
  workspace_path: string
  is_hosted?: boolean
}

export interface GitStatus {
  is_repo: boolean
  branch: string | null
  staged: string[]
  unstaged: string[]
  untracked: string[]
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

export interface MemoryPreset {
  id: string
  icon: string
  title: string
  category: string
  description: string
  content: string
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
  updated_at?: string  // set on every code edit — always fresh
  content?: string  // only present when explicitly fetched
  file_type?: string  // 'code' | 'image' | 'pdf' | 'csv' | 'excel' | 'text'
  origin?: 'uploaded' | 'created' | 'edited'  // 'created' = AI-generated net-new file; 'edited' = transform output
  edited?: number  // 1 if AI/user has modified content since import/upload (0/1 from SQLite), 0 = pristine
  github_meta?: string  // JSON string with owner/repo/branch/path/sha
  github_pushed_at?: string  // timestamp of last push to GitHub
}

export interface NewFile {
  filename: string
  content: string
  language: string
  summary: string
}

export type AgentTaskStatus = 'pending' | 'running' | 'done' | 'blocked' | 'cancelled' | 'error'

export interface AgentTask {
  id: string
  seq: number
  title: string
  detail: string
  kind?: 'code' | 'answer'
  status: AgentTaskStatus
  qa_score?: number | null
  verdict?: string | null
  run_id?: string
  progress?: string  // latest live progress line (client-side only)
  thinking?: string  // model's extended-thinking trail (streamed live + persisted)
}

export interface SmartResult {
  intent: 'edit' | 'chat' | 'create'
  summary: string
  reasoning: string
  risks: string[]
  changes_by_file: Record<string, {
    filename: string
    file_id: string
    changes: SurgicalChange[]
  }>
  skipped_changes?: { symbol: string; reason: string }[]
  new_files?: NewFile[]
}
