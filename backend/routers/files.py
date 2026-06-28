"""File system router — browse, read, write files.

Security: All path access is sandboxed to the configured workspace root.
Path traversal (../) and symlink escapes are blocked.
"""
import os
import mimetypes
from pathlib import Path
from fastapi import APIRouter, HTTPException
from models.schemas import FileNode, FileContent, SaveFileRequest
from database import get_setting
from services.ast_parser import ASTParser

router = APIRouter()
parser = ASTParser()

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".surgicalai_backups", 
                "dist", "build", ".next", "venv", ".venv", ".env", "target"}
IGNORED_EXTS = {".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB


# ─── Path Security ────────────────────────────────────────────────────────────

def _get_workspace_root() -> str:
    """Return the resolved (absolute) workspace root directory."""
    raw = get_setting("workspace_path", str(Path.home()))
    return os.path.realpath(raw)


def _safe_path(requested: str) -> Path:
    """Resolve requested path and ensure it stays within the workspace.

    Blocks path traversal (../), absolute paths outside workspace,
    and symlink escapes.  Raises 403 on any violation.
    """
    # Reject null bytes (path injection vector)
    if "\x00" in requested:
        raise HTTPException(status_code=400, detail="Invalid path")

    workspace = _get_workspace_root()

    # Resolve: relative paths join to workspace, absolute paths resolve directly
    if os.path.isabs(requested):
        resolved = os.path.realpath(requested)
    else:
        resolved = os.path.realpath(os.path.join(workspace, requested))

    # Prefix check — trailing sep prevents /app matching /application
    if resolved != workspace and not resolved.startswith(workspace + os.sep):
        raise HTTPException(status_code=403, detail="Access denied")

    return Path(resolved)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_language(path: str) -> str:
    ext = Path(path).suffix.lower()
    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "javascriptreact", ".tsx": "typescriptreact",
        ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
        ".cpp": "cpp", ".c": "c", ".h": "c", ".hpp": "cpp",
        ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
        ".html": "html", ".css": "css", ".scss": "scss",
        ".json": "json", ".yaml": "yaml", ".yml": "yaml",
        ".md": "markdown", ".sh": "bash", ".bash": "bash",
        ".sql": "sql", ".xml": "xml", ".toml": "toml",
    }
    return lang_map.get(ext, "plaintext")


def _build_tree(path: Path, workspace: str, max_depth: int = 6, depth: int = 0) -> FileNode:
    """Build file tree, restricted to workspace root."""
    if depth > max_depth:
        return None

    # Safety: skip anything that resolved outside the workspace
    real = os.path.realpath(str(path))
    if real != workspace and not real.startswith(workspace + os.sep):
        return None

    name = path.name
    if path.is_dir():
        if name in IGNORED_DIRS:
            return None
        children = []
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            for entry in entries[:200]:  # Cap at 200 entries per dir
                child = _build_tree(entry, workspace, max_depth, depth + 1)
                if child:
                    children.append(child)
        except PermissionError:
            pass
        return FileNode(name=name, path=str(path), type="dir", children=children)
    else:
        ext = path.suffix.lower()
        if ext in IGNORED_EXTS:
            return None
        try:
            size = path.stat().st_size
        except:
            size = 0
        return FileNode(name=name, path=str(path), type="file",
                       extension=ext, size=size)


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/tree")
def get_file_tree(root: str = None):
    workspace = _get_workspace_root()

    if root:
        # Validate that any client-supplied root is within the workspace
        real_root = os.path.realpath(root)
        if real_root != workspace and not real_root.startswith(workspace + os.sep):
            raise HTTPException(status_code=403, detail="Access denied")
        root_path = Path(real_root)
    else:
        root_path = Path(workspace)

    if not root_path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {root_path}")
    tree = _build_tree(root_path, workspace)
    return tree


@router.get("/read")
def read_file(path: str):
    p = _safe_path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not p.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    size = p.stat().st_size
    if size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"File too large ({size // 1024}KB). Max 5MB.")

    try:
        with open(str(p), "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    language = _get_language(str(p))
    return FileContent(
        path=str(p),
        content=content,
        language=language,
        size=size,
        lines=len(content.splitlines())
    )


@router.post("/save")
def save_file(req: SaveFileRequest):
    p = _safe_path(req.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        with open(str(p), "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"ok": True, "lines": len(req.content.splitlines())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/symbols")
def get_symbols(path: str, content: str = None):
    """Parse and return the symbol map for a file."""
    if content is None:
        p = _safe_path(path)
        if not p.exists():
            raise HTTPException(status_code=404, detail="File not found")
        with open(str(p), "r", encoding="utf-8") as f:
            content = f.read()
    symbol_map = parser.parse(content, path)
    return symbol_map


@router.get("/backups")
def list_backups(path: str):
    _safe_path(path)  # validate before delegating
    from services.surgical_editor import list_backups
    return list_backups(path)


@router.post("/restore")
def restore_backup(body: dict):
    from services.surgical_editor import restore_backup
    file_path = body.get("file_path")
    backup_path = body.get("backup_path")
    if not file_path or not backup_path:
        raise HTTPException(status_code=400, detail="file_path and backup_path required")
    _safe_path(file_path)    # validate both paths
    _safe_path(backup_path)
    ok = restore_backup(file_path, backup_path)
    return {"ok": ok}
