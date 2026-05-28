"""Per-session file storage — files uploaded to a chat stay in that chat.
Supports: code, images (vision), PDFs, CSV, Excel.
"""
import uuid
import base64
import io
import logging
from pathlib import Path
import mimetypes
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from database import get_db
from services.ast_parser import ASTParser

logger = logging.getLogger(__name__)

router = APIRouter()
parser = ASTParser()

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".heic", ".heif"}
PDF_EXTENSIONS = {".pdf"}
CSV_EXTENSIONS = {".csv"}
EXCEL_EXTENSIONS = {".xlsx", ".xls"}

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs",
    ".cpp", ".c", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
    ".html", ".css", ".json", ".yaml", ".yml", ".md", ".sh", ".sql",
    ".toml", ".txt",
}


def _sanitize_for_postgres(s: str) -> str:
    """Strip NUL (0x00) bytes — Postgres rejects them in TEXT columns.
    Defense-in-depth: if the frontend ever ships binary bytes as text
    (e.g. iOS HEIC misclassified), this prevents a 500."""
    if not s:
        return s
    if "\x00" in s:
        logger.warning(f"[session_files] Stripping NUL bytes from input (len={len(s)})")
        return s.replace("\x00", "")
    return s


def _get_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in CSV_EXTENSIONS:
        return "csv"
    if ext in EXCEL_EXTENSIONS:
        return "excel"
    return "code"


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
        ".toml": "toml", ".csv": "csv", ".xlsx": "excel", ".xls": "excel",
        ".pdf": "pdf",
    }
    return lang_map.get(ext, "plaintext")


def _extract_pdf_text(base64_data: str) -> str:
    """Extract text from a base64-encoded PDF using pdfplumber."""
    try:
        import pdfplumber
        # Strip data URL prefix if present
        if "," in base64_data:
            base64_data = base64_data.split(",", 1)[1]
        pdf_bytes = base64.b64decode(base64_data)
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text()
                if text:
                    text_parts.append(f"--- Page {i} ---\n{text}")
                # Also extract tables
                tables = page.extract_tables()
                for table in tables:
                    if table:
                        rows = []
                        for row in table:
                            rows.append(" | ".join(str(cell or "") for cell in row))
                        text_parts.append("\n".join(rows))
        return "\n\n".join(text_parts) if text_parts else "(PDF has no extractable text)"
    except ImportError:
        return "(pdfplumber not installed — PDF text extraction unavailable)"
    except Exception as e:
        return f"(PDF extraction error: {e})"


def _parse_csv_to_markdown(content: str) -> str:
    """Convert CSV text to a markdown table."""
    try:
        import pandas as pd
        df = pd.read_csv(io.StringIO(content))
        # Cap rows for context
        if len(df) > 200:
            preview = df.head(200)
            md = preview.to_markdown(index=False)
            md += f"\n\n... [{len(df) - 200} more rows not shown]"
            return md
        return df.to_markdown(index=False)
    except ImportError:
        # Fallback: return raw CSV
        return content
    except Exception as e:
        return f"(CSV parse error: {e})\n\n{content[:2000]}"


def _parse_excel_to_markdown(base64_data: str, filename: str) -> str:
    """Convert Excel file (base64) to markdown tables, one per sheet."""
    try:
        import pandas as pd
        if "," in base64_data:
            base64_data = base64_data.split(",", 1)[1]
        excel_bytes = base64.b64decode(base64_data)
        xl = pd.ExcelFile(io.BytesIO(excel_bytes))
        parts = []
        for sheet_name in xl.sheet_names:
            df = xl.parse(sheet_name)
            if len(df) > 200:
                preview = df.head(200)
                md = preview.to_markdown(index=False)
                md += f"\n\n... [{len(df) - 200} more rows not shown]"
            else:
                md = df.to_markdown(index=False)
            parts.append(f"### Sheet: {sheet_name}\n\n{md}")
        return "\n\n".join(parts)
    except ImportError:
        return "(pandas/openpyxl not installed — Excel extraction unavailable)"
    except Exception as e:
        return f"(Excel parse error: {e})"


@router.post("/{session_id}/files")
def upload_session_file(session_id: str, body: dict):
    """Upload a file to a chat session. Replaces if same filename already exists."""
    filename = body.get("filename", "untitled")
    raw_content = body.get("content", "")
    base64_data = body.get("base64_data", "")
    file_type = body.get("file_type") or _get_file_type(filename)
    language = body.get("language") or _get_language(filename)

    # Defensive logging — helps diagnose iOS/Android/edge-case uploads
    _data_url_mime = ""
    if base64_data and base64_data.startswith("data:") and ";" in base64_data:
        _data_url_mime = base64_data.split(":")[1].split(";")[0]
    logger.info(
        f"[upload] session={session_id[:8]} filename={filename!r} type={file_type} "
        f"raw_len={len(raw_content)} base64_len={len(base64_data)} "
        f"data_url_mime={_data_url_mime!r} raw_has_nul={chr(0) in raw_content}"
    )

    # Process content based on file type
    if file_type == "image":
        # Store the base64 data URL directly — pipeline will use it for vision
        content = base64_data if base64_data else raw_content
        lines = 0
        symbol_count = 0
    elif file_type == "pdf":
        content = _extract_pdf_text(base64_data)
        lines = len(content.splitlines())
        symbol_count = 0
    elif file_type == "csv":
        content = _parse_csv_to_markdown(raw_content)
        lines = len(raw_content.splitlines())
        symbol_count = 0
    elif file_type == "excel":
        content = _parse_excel_to_markdown(base64_data, filename)
        lines = len(content.splitlines())
        symbol_count = 0
    else:
        # Code / text — existing behavior
        content = raw_content
        lines = len(content.splitlines())
        try:
            smap = parser.parse(content, filename)
            symbol_count = len(smap.symbols)
        except Exception:
            symbol_count = 0

    # ── Defense-in-depth: strip NUL bytes before DB insert ──────────
    # Postgres TEXT columns reject embedded \x00. iOS HEIC files
    # read as text via file.text() produce NUL-laced strings that
    # cause a 500 here. Sanitize filename + content unconditionally.
    filename = _sanitize_for_postgres(filename)
    content = _sanitize_for_postgres(content)

    conn = get_db()
    # Replace if same filename in same session
    existing = conn.execute(
        "SELECT id FROM session_files WHERE session_id = ? AND filename = ?",
        (session_id, filename)
    ).fetchone()

    if existing:
        file_id = existing["id"] if hasattr(existing, "__getitem__") else existing[0]
        conn.execute(
            "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ?, file_type = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (content, language, lines, symbol_count, file_type, file_id)
        )
    else:
        file_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, language, lines, symbol_count, file_type, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (file_id, session_id, filename, content, language, lines, symbol_count, file_type)
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
        "file_type": file_type,
        "updated_at": None,  # fresh upload — DB default applies
    }


@router.get("/{session_id}/files")
def list_session_files(session_id: str):
    """List files attached to a session (metadata only, no content)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, session_id, filename, language, lines, symbol_count, file_type, github_meta, created_at, updated_at, github_pushed_at
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


@router.put("/{session_id}/files/{file_id}")
def update_session_file(session_id: str, file_id: str, body: dict):
    """Update file content after applying a change. Saves current content as previous for undo."""
    new_content = body.get("content", "")
    conn = get_db()
    row = conn.execute(
        "SELECT content, filename FROM session_files WHERE id = ? AND session_id = ?",
        (file_id, session_id)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="File not found")

    filename = row["filename"] if hasattr(row, "__getitem__") else row[1]
    prev_content = row["content"] if hasattr(row, "__getitem__") else row[0]
    lines = len(new_content.splitlines())
    try:
        smap = parser.parse(new_content, filename)
        symbol_count = len(smap.symbols)
    except Exception:
        symbol_count = 0

    new_content = _sanitize_for_postgres(new_content)
    prev_content = _sanitize_for_postgres(prev_content)

    conn.execute(
        "UPDATE session_files SET content = ?, previous_content = ?, lines = ?, symbol_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND session_id = ?",
        (new_content, prev_content, lines, symbol_count, file_id, session_id)
    )
    conn.commit()
    conn.close()
    return {"id": file_id, "lines": lines, "symbol_count": symbol_count, "ok": True}


@router.post("/{session_id}/files/{file_id}/undo")
def undo_session_file(session_id: str, file_id: str):
    """Restore a file to its previous version (one-step undo)."""
    conn = get_db()
    row = conn.execute(
        "SELECT content, previous_content, filename FROM session_files WHERE id = ? AND session_id = ?",
        (file_id, session_id)
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="File not found")

    if hasattr(row, "__getitem__"):
        current = row["content"]
        prev = row["previous_content"]
        filename = row["filename"]
    else:
        current, prev, filename = row[0], row[1], row[2]

    if not prev:
        conn.close()
        raise HTTPException(status_code=400, detail="No previous version to restore")

    lines = len(prev.splitlines())
    try:
        smap = parser.parse(prev, filename)
        symbol_count = len(smap.symbols)
    except Exception:
        symbol_count = 0

    # Swap current ↔ previous so undo is reversible (acts as redo too)
    conn.execute(
        "UPDATE session_files SET content = ?, previous_content = ?, lines = ?, symbol_count = ? WHERE id = ? AND session_id = ?",
        (prev, current, lines, symbol_count, file_id, session_id)
    )
    conn.commit()
    conn.close()
    return {"id": file_id, "content": prev, "lines": lines, "symbol_count": symbol_count, "ok": True}


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


@router.get("/{session_id}/files/{file_id}/preview")
def preview_session_file(session_id: str, file_id: str):
    """Serve file content for live preview with correct Content-Type.
    No auth required — session_id + file_id are UUIDs (unguessable).
    Used by the frontend LivePreview iframe so HTML assets resolve correctly."""
    conn = get_db()
    row = conn.execute(
        "SELECT filename, content FROM session_files WHERE id = ? AND session_id = ?",
        (file_id, session_id),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")

    if hasattr(row, "__getitem__"):
        filename, content = row["filename"], row["content"]
    else:
        filename, content = row[0], row[1]

    content_type = mimetypes.guess_type(filename)[0] or "text/plain"
    return Response(
        content=content,
        media_type=content_type,
        headers={"Access-Control-Allow-Origin": "*"},
    )
