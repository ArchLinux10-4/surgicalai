"""Pydantic schemas for all API requests and responses."""
from pydantic import BaseModel, Field, computed_field
from typing import Optional, List, Any
from enum import Enum


# ─── Settings ────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    architect_model: Optional[str] = None
    surgeon_model: Optional[str] = None
    temperature_architect: Optional[float] = None
    temperature_surgeon: Optional[float] = None
    confidence_threshold: Optional[int] = None
    auto_backup: Optional[bool] = None
    theme: Optional[str] = None
    font_size: Optional[int] = None
    workspace_path: Optional[str] = None
    ollama_enabled: Optional[bool] = None
    ollama_base_url: Optional[str] = None
    ollama_model: Optional[str] = None


class SettingsResponse(BaseModel):
    openai_api_key_set: bool
    anthropic_api_key_set: bool = False
    architect_model: str
    surgeon_model: str
    temperature_architect: float
    temperature_surgeon: float
    confidence_threshold: int
    auto_backup: bool
    theme: str
    font_size: int
    workspace_path: str
    ollama_enabled: bool = False
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:7b"


# ─── Chat ─────────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str  # "user" | "assistant" | "system"
    content: str


class ChatRequest(BaseModel):
    session_id: str
    message: str
    file_path: Optional[str] = None
    file_content: Optional[str] = None
    symbol_context: Optional[str] = None
    model: Optional[str] = None


class ChatSession(BaseModel):
    id: str
    title: str
    file_path: Optional[str]
    model: str
    created_at: str
    updated_at: str
    message_count: int = 0


class NewSessionRequest(BaseModel):
    title: str = "New Chat"
    file_path: Optional[str] = None
    model: Optional[str] = None


# ─── Files ────────────────────────────────────────────────────────────────────

class FileNode(BaseModel):
    name: str
    path: str
    type: str  # "file" | "dir"
    children: Optional[List["FileNode"]] = None
    extension: Optional[str] = None
    size: Optional[int] = None


FileNode.model_rebuild()


class FileContent(BaseModel):
    path: str
    content: str
    language: str
    size: int
    lines: int


class SaveFileRequest(BaseModel):
    path: str
    content: str


# ─── AST / Symbols ───────────────────────────────────────────────────────────

class SymbolType(str, Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    ARROW_FUNCTION = "arrow_function"
    VARIABLE = "variable"


class SymbolInfo(BaseModel):
    name: str
    symbol_type: SymbolType
    start_line: int
    end_line: int
    parent: Optional[str] = None
    indentation: int = 0
    code: str
    signature: str = ""

    @computed_field
    @property
    def full_path(self) -> str:
        if self.parent:
            return f"{self.parent}.{self.name}"
        return self.name


class SymbolMap(BaseModel):
    file_path: str
    language: str
    symbols: List[SymbolInfo]
    imports: List[str] = []
    total_lines: int = 0


# ─── Surgical ─────────────────────────────────────────────────────────────────

class ChangeType(str, Enum):
    MODIFY = "modify"
    ADD = "add"
    DELETE = "delete"
    REFACTOR = "refactor"


class ChangeTarget(BaseModel):
    symbol_path: str
    change_type: ChangeType
    description: str
    new_logic: str
    dependencies: List[str] = []
    confidence: int = 8


class ArchitectPlan(BaseModel):
    summary: str
    targets: List[ChangeTarget]
    new_symbols_needed: List[str] = []
    import_changes: List[str] = []
    risks: List[str] = []


class QAResult(BaseModel):
    verdict: str = "skipped"          # safe | warning | blocked | skipped
    qa_score: Optional[int] = None    # 1-10, None if skipped
    summary: str = ""
    import_issues: List[str] = []
    downstream_risks: List[str] = []
    type_errors: List[str] = []
    plan_deviation: str = ""
    skipped_reason: Optional[str] = None


class QAResult(BaseModel):
    verdict: str = "skipped"          # safe | warning | blocked | skipped
    qa_score: Optional[int] = None    # 1-10, None if skipped
    summary: str = ""
    import_issues: List[str] = []
    downstream_risks: List[str] = []
    type_errors: List[str] = []
    plan_deviation: str = ""
    skipped_reason: Optional[str] = None


class SurgicalOperation(BaseModel):
    """A single search-and-replace operation. The backend applies these mechanically."""
    find: str       # exact text to locate in the file (character-for-character)
    replace: str    # replacement text


class SurgicalChange(BaseModel):
    id: str
    symbol: SymbolInfo
    original_code: str
    new_code: str
    diff: str
    confidence: int
    description: str
    applied: bool = False
    surgeon_notes: List[str] = []
    qa_result: Optional[QAResult] = None
    # v3.4.0: search-and-replace operations (primary apply path)
    operations: List[SurgicalOperation] = []
    # Legacy fields kept for backward compat (v3.3.x sessions still in DB)
    target_element: Optional[str] = None
    replacement: Optional[str] = None
    insert_mode: bool = False
    insert_anchor: Optional[str] = None


class SurgicalAnalyzeRequest(BaseModel):
    file_path: str
    file_content: str
    request: str
    session_id: Optional[str] = None


class SurgicalAnalyzeResponse(BaseModel):
    session_id: str
    plan: ArchitectPlan
    changes: List[SurgicalChange]
    tokens_used: int = 0


class SurgicalApplyRequest(BaseModel):
    file_path: str
    change_id: Optional[str] = None
    changes: List[SurgicalChange]
    file_content: Optional[str] = None  # Required when file isn't on server disk (uploaded files)


class SurgicalApplyResponse(BaseModel):
    file_path: str
    new_content: str
    applied_count: int
    backup_path: Optional[str] = None
    cloud_mode: bool = False
    modified_content: Optional[str] = None


class SurgicalPreviewRequest(BaseModel):
    file_path: str
    file_content: str
    symbol_path: str
    new_code: str


# ─── Git ──────────────────────────────────────────────────────────────────────

class GitStatus(BaseModel):
    is_repo: bool
    branch: Optional[str] = None
    staged: List[str] = []
    unstaged: List[str] = []
    untracked: List[str] = []


class GitCommitRequest(BaseModel):
    repo_path: str
    message: str
    files: Optional[List[str]] = None  # None = all staged


class GitDiffRequest(BaseModel):
    repo_path: str
    file_path: Optional[str] = None


# ─── Context Pinning ──────────────────────────────────────────────────────────

class PinnedContext(BaseModel):
    id: str
    workspace_path: str
    file_path: str
    symbol_path: Optional[str] = None
    label: Optional[str] = None
    created_at: str

class PinRequest(BaseModel):
    session_id: Optional[str] = None
    workspace_path: Optional[str] = None
    file_path: str
    symbol_path: Optional[str] = None
    label: Optional[str] = None

# ─── Project Memory ───────────────────────────────────────────────────────────

class ProjectMemory(BaseModel):
    id: Optional[str] = None
    session_id: Optional[str] = None
    workspace_path: Optional[str] = None
    content: str
    updated_at: Optional[str] = None

# ─── Prompt Templates ─────────────────────────────────────────────────────────

class PromptTemplate(BaseModel):
    id: str
    name: str
    prompt: str
    mode: str
    created_at: str

class PromptTemplateCreate(BaseModel):
    name: str
    prompt: str
    mode: str = "chat"

# ─── Multi-file Surgical ──────────────────────────────────────────────────────

class MultiFileAnalyzeRequest(BaseModel):
    file_paths: List[str]
    file_contents: dict  # {file_path: content}
    request: str
    session_id: Optional[str] = None

class MultiFileAnalyzeResponse(BaseModel):
    session_id: str
    files_analyzed: int
    changes_by_file: dict  # {file_path: SurgicalAnalyzeResponse}
    overall_summary: str

class MultiFileApplyRequest(BaseModel):
    changes_by_file: dict  # {file_path: [change_id, ...]}
    all_changes: dict  # {file_path: [SurgicalChange, ...]}

# ─── Impact Analysis ─────────────────────────────────────────────────────────

class ImpactResult(BaseModel):
    symbol_path: str
    file_path: str
    impact_type: str  # "calls", "imports", "inherits", "uses"
    description: str

class ImpactAnalysisResponse(BaseModel):
    target_symbol: str
    impacts: List[ImpactResult]
    risk_level: str  # "low", "medium", "high"
    summary: str

# ─── Streaming ────────────────────────────────────────────────────────────────

class StreamChunk(BaseModel):
    type: str  # "token", "done", "error", "progress"
    content: str
    metadata: Optional[dict] = None
