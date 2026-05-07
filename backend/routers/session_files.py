"""Per-session file storage — files uploaded to a chat stay in that chat.
Supports: code, images (vision), PDFs, CSV, Excel.
"""
import uuid
import base64
import io
from pathlib import Path
from fastapi import APIRouter, HTTPException
from database import get_db
from services.ast_parser import ASTParser

router = APIRouter()
parser = ASTParser()

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}
PDF_EXTENSIONS = {".pdf"}
CSV_EXTENSIONS = {".csv"}
EXCEL_EXTENSIONS = {".xlsx", ".xls"}

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs",
    ".cpp", ".c", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
    ".html", ".css", ".json", ".yaml", ".yml", ".md", ".sh", ".sql",
    ".toml", ".txt",
}


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

    conn = get_db()
    # Replace if same filename in same session
    existing = conn.execute(
        "SELECT id FROM session_files WHERE session_id = ? AND filename = ?",
        (session_id, filename)
    ).fetchone()

    if existing:
        file_id = existing["id"] if hasattr(existing, "__getitem__") else existing[0]
        conn.execute(
            "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ?, file_type = ? WHERE id = ?",
            (content, language, lines, symbol_count, file_type, file_id)
        )
    else:
        file_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, language, lines, symbol_count, file_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
    }


@router.get("/{session_id}/files")
def list_session_files(session_id: str):
    """List files attached to a session (metadata only, no content)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, session_id, filename, language, lines, symbol_count, file_type, created_at
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
