"""Per-session file storage — files uploaded to a chat stay in that chat."""
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException
from database import get_db
from services.ast_parser import ASTParser

router = APIRouter()
parser = ASTParser()


def _get_language(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "javascriptreact", ".tsx": "typescriptreact",
        ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
        ".cpp": "cpp", ".c": "c", ".h": "c", ".hpp": "cpp",
        ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
        ".html": "html", ".css": "css", ".json": "json", ".yaml": "yaml",
        ".yml": "yaml", ".md": "markdown", ".sh": "bash", ".sql": "sql",
        ".toml": "toml",
    }
    return lang_map.get(ext, "plaintext")


@router.post("/{session_id}/files")
def upload_session_file(session_id: str, body: dict):
    """Upload a file to a chat session. Replaces if same filename already exists."""
    filename = body.get("filename", "untitled")
    content = body.get("content", "")
    language = body.get("language") or _get_language(filename)
    lines = len(content.splitlines())

    # Parse symbol map to count symbols
    try:
        smap = parser.parse(content, filename)
        symbol_count = len(smap.symbols)
    except Exception:
        symbol_count = 0

    conn = get_db()
    # Replace if same filename in same session
    existing = conn.execute(
        "SELECT id FROM session_files WHERE session_id = ? AND filename = ?",
        (session_id, filename)
    ).fetchone()

    if existing:
        file_id = existing["id"]
        conn.execute(
            "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ? WHERE id = ?",
            (content, language, lines, symbol_count, file_id)
        )
    else:
        file_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, language, lines, symbol_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, session_id, filename, content, language, lines, symbol_count)
        )

    conn.commit()
    conn.close()

    return {
        "id": file_id,
        "session_id": session_id,
        "filename": filename,
        "language": language,
        "lines": lines,
        "symbol_count": symbol_count,
    }


@router.get("/{session_id}/files")
def list_session_files(session_id: str):
    """List files attached to a session (metadata only, no content)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, session_id, filename, language, lines, symbol_count, created_at
           FROM session_files WHERE session_id = ? ORDER BY created_at ASC""",
        (session_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/{session_id}/files/{file_id}")
def get_session_file(session_id: str, file_id: str):
    """Get a specific file's full content."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM session_files WHERE id = ? AND session_id = ?",
        (file_id, session_id)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    return dict(row)


@router.delete("/{session_id}/files/{file_id}")
def delete_session_file(session_id: str, file_id: str):
    """Remove a file from a session."""
    conn = get_db()
    conn.execute(
        "DELETE FROM session_files WHERE id = ? AND session_id = ?",
        (file_id, session_id)
    )
    conn.commit()
    conn.close()
    return {"ok": True}
