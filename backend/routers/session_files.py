"""Per-session file storage — files uploaded to a chat stay in that chat.
Supports: code, images (vision), PDFs, CSV, Excel.
"""
import uuid
import base64
import io
import logging
from pathlib import Path
import mimetypes
from fastapi import APIRouter, HTTPException, File, UploadFile, Form
from fastapi.responses import Response
from database import get_db, get_db_ctx
from middleware.file_validator import validate_file_size, validate_session_total
from services.ast_parser import ASTParser

logger = logging.getLogger(__name__)

router = APIRouter()
parser = ASTParser()

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".heif"}
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
    """Strip NUL (0x00) bytes — Postgres rejects them in TEXT columns."""
    if not s:
        return s
    if "\x00" in s:
        logger.warning(f"[session_files] Stripping NUL bytes from input (len={len(s)})")
        return s.replace("\x00", "")
    return s


def _maybe_store_datalab_bytes(session_id, file_id, filename, file_type, raw_bytes, mime=""):
    """
    DataLab fork: when the feature is ON and this is a spreadsheet/CSV, persist
    the ORIGINAL bytes so the workbook can be round-tripped/edited later.
    Best-effort and fully isolated — never raises into the upload path, and is a
    complete no-op when the flag is off (zero behavior change).
    """
    try:
        if file_type not in ("csv", "excel"):
            return
        from services.datalab.config import datalab_enabled
        if not datalab_enabled():
            return
        if not raw_bytes:
            return
        from services.datalab import persist
        persist.register_upload_artifact(
            session_id=session_id, source_file_id=file_id,
            filename=filename, raw=raw_bytes, mime=mime,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[datalab] upload artifact store skipped: {type(e).__name__}: {e}")


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
        ".svg": "xml", ".html": "html", ".css": "css", ".json": "json", ".yaml": "yaml",
        ".yml": "yaml", ".md": "markdown", ".sh": "bash", ".sql": "sql",
        ".toml": "toml", ".csv": "csv", ".xlsx": "excel", ".xls": "excel",
        ".pdf": "pdf",
    }
    return lang_map.get(ext, "plaintext")


_CLAUDE_SUPPORTED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _detect_image_magic_bytes(data: bytes) -> str:
    """Return the detected image MIME type from the first 12 bytes, or '' if not an image."""
    if len(data) < 4:
        return ""
    if data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] in (b"GIF8", b"GIF9"):
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        heic_brands = {
            b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
            b"hevm", b"hevs", b"mif1", b"msf1",
        }
        if brand in heic_brands:
            return "image/heic"
        if brand in {b"avif", b"avis"}:
            return "image/avif"
    if data[:2] == b"BM":
        return "image/bmp"
    return ""


def _normalize_image_for_claude(base64_data: str) -> str:
    """Convert any image format to a JPEG data URL that Claude API accepts."""
    if not base64_data:
        return base64_data

    src_mime = ""
    b64 = base64_data
    if "," in base64_data:
        header, b64 = base64_data.split(",", 1)
        if ":" in header and ";" in header:
            src_mime = header.split(":")[1].split(";")[0].lower().strip()

    try:
        padded = b64 + "==" * ((4 - len(b64) % 4) % 4)
        img_bytes = base64.b64decode(padded)
    except Exception as e:
        logger.warning(f"[session_files] normalize_image: b64 decode failed: {e}")
        return base64_data

    detected_mime = _detect_image_magic_bytes(img_bytes)
    effective_mime = detected_mime or src_mime

    logger.warning(
        f"[session_files] normalize_image: src_mime={src_mime!r} "
        f"detected={detected_mime!r} size={len(img_bytes)} bytes"
    )

    if src_mime in _CLAUDE_SUPPORTED_MIME and (not detected_mime or detected_mime in _CLAUDE_SUPPORTED_MIME):
        logger.warning(f"[session_files] normalize_image: {src_mime} already Claude-supported — no conversion")
        return base64_data

    try:
        if effective_mime in ("image/heic", "image/heif", "image/avif") or not detected_mime:
            try:
                import pillow_heif  # type: ignore
                pillow_heif.register_heif_opener()
                logger.warning("[session_files] pillow_heif registered for HEIC/HEIF conversion")
            except ImportError:
                logger.warning(
                    "[session_files] pillow_heif not installed — HEIC conversion may fail."
                )

        from PIL import Image as PILImage  # type: ignore

        img = PILImage.open(io.BytesIO(img_bytes))
        w, h = img.size
        logger.warning(f"[session_files] PIL opened image: mode={img.mode} size={w}x{h}")

        if img.mode in ("RGBA", "LA", "P"):
            bg = PILImage.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            if img.mode in ("RGBA", "LA"):
                bg.paste(img, mask=img.split()[-1])
            else:
                bg.paste(img)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85, optimize=True)
        jpeg_bytes = output.getvalue()
        b64_out = base64.b64encode(jpeg_bytes).decode()

        logger.warning(
            f"[session_files] Converted {effective_mime or 'unknown'} → JPEG "
            f"({len(jpeg_bytes):,} bytes, {w}x{h})"
        )
        return f"data:image/jpeg;base64,{b64_out}"

    except Exception as e:
        logger.warning(
            f"[session_files] Image normalization FAILED "
            f"({type(e).__name__}: {e}) — storing original"
        )
        return base64_data


def _extract_pdf_text(base64_data: str) -> str:
    """Extract text from a base64-encoded PDF using pdfplumber."""
    try:
        import pdfplumber
        if "," in base64_data:
            base64_data = base64_data.split(",", 1)[1]
        pdf_bytes = base64.b64decode(base64_data)
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text()
                if text:
                    text_parts.append(f"--- Page {i} ---\n{text}")
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
        if len(df) > 200:
            preview = df.head(200)
            md = preview.to_markdown(index=False)
            md += f"\n\n... [{len(df) - 200} more rows not shown]"
            return md
        return df.to_markdown(index=False)
    except ImportError:
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
    """Upload a file to a chat session (legacy JSON path)."""
    filename = body.get("filename", "untitled")
    raw_content = body.get("content", "")
    base64_data = body.get("base64_data", "")

    # ── File size validation ──────────────────────────────────────────────
    _payload_bytes = len(base64_data) if base64_data else len(raw_content.encode("utf-8", errors="replace"))
    validate_file_size(filename, _payload_bytes)
    with get_db_ctx() as _vconn:
        _session_total = _vconn.execute(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM session_files WHERE session_id = ?",
            (session_id,)
        ).fetchone()[0]
    validate_session_total(_session_total, _payload_bytes)
    file_type = body.get("file_type") or _get_file_type(filename)
    language = body.get("language") or _get_language(filename)
    # 'created' = AI-generated net-new file (added from a New File card);
    # anything else is treated as a user-provided upload.
    origin = "created" if body.get("origin") == "created" else "uploaded"

    # ── DIAGNOSTIC: log at WARNING so it always appears in Railway logs ──────
    _data_url_mime = ""
    if base64_data and base64_data.startswith("data:") and ";" in base64_data:
        _data_url_mime = base64_data.split(":")[1].split(";")[0]
    logger.warning(
        f"[upload-json] session={session_id[:8]} filename={filename!r} "
        f"file_type={file_type} raw_len={len(raw_content)} "
        f"base64_len={len(base64_data)} data_url_mime={_data_url_mime!r} "
        f"raw_has_nul={chr(0) in raw_content}"
    )

    if file_type == "image":
        raw_b64 = base64_data if base64_data else raw_content

        if not base64_data and raw_content:
            logger.warning(
                f"[upload-json] image via text path — filename={filename!r} "
                f"raw_len={len(raw_content)} has_nul={chr(0) in raw_content}"
            )
            cleaned = raw_content.replace("\x00", "")
            try:
                raw_bytes = cleaned.encode("latin-1", errors="replace")
                detected = _detect_image_magic_bytes(raw_bytes)
                logger.warning(f"[upload-json] magic bytes after latin-1 encode: detected={detected!r} first4={raw_bytes[:4].hex()!r}")
                if detected:
                    b64_attempt = base64.b64encode(raw_bytes).decode()
                    raw_b64 = f"data:{detected};base64,{b64_attempt}"
                    logger.warning(f"[upload-json] Recovered binary image from text path: {detected}")
            except Exception as e:
                logger.warning(f"[upload-json] Binary recovery failed: {e}")
                raw_b64 = raw_content

        content = _normalize_image_for_claude(raw_b64)
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
        content = raw_content
        lines = len(content.splitlines())
        try:
            smap = parser.parse(content, filename)
            symbol_count = len(smap.symbols)
        except Exception:
            symbol_count = 0

    filename = _sanitize_for_postgres(filename)
    content = _sanitize_for_postgres(content)

    with get_db_ctx() as conn:
        existing = conn.execute(
            "SELECT id, updated_at FROM session_files WHERE session_id = ? AND filename = ?",
            (session_id, filename)
        ).fetchone()

        if existing:
            file_id = existing["id"] if hasattr(existing, "__getitem__") else existing[0]
            row_updated_at = existing["updated_at"] if hasattr(existing, "__getitem__") else existing[1]
            result = conn.execute(
                "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ?, file_type = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND updated_at = ?",
                (content, language, lines, symbol_count, file_type, file_id, row_updated_at)
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=409, detail="File was modified concurrently — please retry")
        else:
            file_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO session_files (id, session_id, filename, content, language, lines, symbol_count, file_type, origin, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (file_id, session_id, filename, content, language, lines, symbol_count, file_type, origin)
            )

        conn.commit()

    # DataLab fork (flag-gated, best-effort): keep the original spreadsheet bytes.
    if file_type in ("csv", "excel"):
        try:
            if file_type == "excel":
                _b = base64_data.split(",", 1)[1] if "," in base64_data else base64_data
                _raw = base64.b64decode(_b) if _b else b""
            else:
                _raw = (raw_content or "").encode("utf-8", errors="replace")
            _maybe_store_datalab_bytes(session_id, file_id, filename, file_type, _raw)
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[datalab] json upload byte capture skipped: {_e}")

    return {
        "id": file_id,
        "session_id": session_id,
        "filename": filename,
        "language": language,
        "lines": lines,
        "symbol_count": symbol_count,
        "file_type": file_type,
        "origin": origin,
        "updated_at": None,
    }


@router.post("/{session_id}/files/upload")
async def upload_session_file_multipart(
    session_id: str,
    file: UploadFile = File(...),
    filename: str = Form(None),
):
    """Multipart file upload — correct approach for iOS / Android / WKWebView."""
    actual_filename = filename or file.filename or "upload"
    raw_bytes = await file.read()

    # ── File size validation ──────────────────────────────────────────────
    validate_file_size(actual_filename, len(raw_bytes))
    with get_db_ctx() as _vconn:
        _session_total = _vconn.execute(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM session_files WHERE session_id = ?",
            (session_id,)
        ).fetchone()[0]
    validate_session_total(_session_total, len(raw_bytes))

    file_type = _get_file_type(actual_filename)
    language = _get_language(actual_filename)

    logger.warning(
        f"[upload-multipart] session={session_id[:8]} filename={actual_filename!r} "
        f"content_type={file.content_type!r} size={len(raw_bytes):,} bytes file_type={file_type}"
    )

    if file_type == "image":
        detected_mime = _detect_image_magic_bytes(raw_bytes)
        effective_mime = detected_mime or file.content_type or "image/jpeg"
        logger.warning(
            f"[upload-multipart] image: detected={detected_mime!r} "
            f"content_type={file.content_type!r} effective={effective_mime!r}"
        )
        import base64 as _b64
        b64 = _b64.b64encode(raw_bytes).decode()
        data_url = f"data:{effective_mime};base64,{b64}"
        content = _normalize_image_for_claude(data_url)
        lines = 0
        symbol_count = 0

    elif file_type == "pdf":
        import base64 as _b64
        b64 = _b64.b64encode(raw_bytes).decode()
        data_url = f"data:application/pdf;base64,{b64}"
        content = _extract_pdf_text(data_url)
        lines = len(content.splitlines())
        symbol_count = 0

    elif file_type in ("csv", "excel"):
        if file_type == "excel":
            import base64 as _b64
            b64 = _b64.b64encode(raw_bytes).decode()
            data_url = f"data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,{b64}"
            content = _parse_excel_to_markdown(data_url, actual_filename)
        else:
            try:
                raw_text = raw_bytes.decode("utf-8", errors="replace")
            except Exception:
                raw_text = raw_bytes.decode("latin-1", errors="replace")
            content = _parse_csv_to_markdown(raw_text)
        lines = len(content.splitlines())
        symbol_count = 0

    else:
        try:
            content = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            content = raw_bytes.decode("latin-1", errors="replace")
        lines = len(content.splitlines())
        try:
            smap = parser.parse(content, actual_filename)
            symbol_count = len(smap.symbols)
        except Exception:
            symbol_count = 0

    actual_filename = _sanitize_for_postgres(actual_filename)
    content = _sanitize_for_postgres(content)

    with get_db_ctx() as conn:
        existing = conn.execute(
            "SELECT id, updated_at FROM session_files WHERE session_id = ? AND filename = ?",
            (session_id, actual_filename)
        ).fetchone()

        if existing:
            file_id = existing["id"] if hasattr(existing, "__getitem__") else existing[0]
            row_updated_at = existing["updated_at"] if hasattr(existing, "__getitem__") else existing[1]
            result = conn.execute(
                "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ?, file_type = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND updated_at = ?",
                (content, language, lines, symbol_count, file_type, file_id, row_updated_at)
            )
            if result.rowcount == 0:
                raise HTTPException(status_code=409, detail="File was modified concurrently — please retry")
        else:
            file_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO session_files (id, session_id, filename, content, language, lines, symbol_count, file_type, origin, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'uploaded', CURRENT_TIMESTAMP)",
                (file_id, session_id, actual_filename, content, language, lines, symbol_count, file_type)
            )

        conn.commit()

    # DataLab fork (flag-gated, best-effort): keep the original spreadsheet bytes.
    _maybe_store_datalab_bytes(
        session_id, file_id, actual_filename, file_type, raw_bytes,
        mime=(file.content_type or ""),
    )

    return {
        "id": file_id,
        "session_id": session_id,
        "filename": actual_filename,
        "language": language,
        "lines": lines,
        "symbol_count": symbol_count,
        "file_type": file_type,
        "origin": "uploaded",
        "updated_at": None,
    }


@router.get("/{session_id}/files")
def list_session_files(session_id: str):
    """List files attached to a session (metadata only, no content)."""
    with get_db_ctx() as conn:
        rows = conn.execute(
            """SELECT id, session_id, filename, language, lines, symbol_count, file_type, origin, github_meta, created_at, updated_at, github_pushed_at, edited
               FROM session_files WHERE session_id = ? ORDER BY created_at ASC""",
            (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{session_id}/files/{file_id}")
def get_session_file(session_id: str, file_id: str):
    """Get a specific file's full content."""
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    return dict(row)


@router.put("/{session_id}/files/{file_id}")
def update_session_file(session_id: str, file_id: str, body: dict):
    """Update file content after applying a change.

    Uses optimistic concurrency: the UPDATE includes AND updated_at = ?
    so a concurrent write between our SELECT and UPDATE is detected and
    returns 409 instead of silently overwriting the other change.
    """
    new_content = body.get("content", "")
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT content, filename, updated_at FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="File not found")

        if hasattr(row, "__getitem__"):
            filename = row["filename"]
            prev_content = row["content"]
            row_updated_at = row["updated_at"]
        else:
            prev_content, filename, row_updated_at = row[0], row[1], row[2]

        lines = len(new_content.splitlines())
        try:
            smap = parser.parse(new_content, filename)
            symbol_count = len(smap.symbols)
        except Exception:
            symbol_count = 0

        new_content = _sanitize_for_postgres(new_content)
        prev_content = _sanitize_for_postgres(prev_content)

        result = conn.execute(
            "UPDATE session_files SET content = ?, previous_content = ?, lines = ?, symbol_count = ?, edited = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND session_id = ? AND updated_at = ?",
            (new_content, prev_content, lines, symbol_count, file_id, session_id, row_updated_at)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="File was modified concurrently — please retry with fresh content")
        conn.commit()
    return {"id": file_id, "lines": lines, "symbol_count": symbol_count, "ok": True}


@router.post("/{session_id}/files/{file_id}/undo")
def undo_session_file(session_id: str, file_id: str):
    """Restore a file to its previous version (one-step undo).

    Optimistic concurrency: AND updated_at = ? prevents undo from
    silently overwriting a concurrent edit.
    """
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT content, previous_content, filename, updated_at FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="File not found")

        if hasattr(row, "__getitem__"):
            current = row["content"]
            prev = row["previous_content"]
            filename = row["filename"]
            row_updated_at = row["updated_at"]
        else:
            current, prev, filename, row_updated_at = row[0], row[1], row[2], row[3]

        if not prev:
            raise HTTPException(status_code=400, detail="No previous version to restore")

        lines = len(prev.splitlines())
        try:
            smap = parser.parse(prev, filename)
            symbol_count = len(smap.symbols)
        except Exception:
            symbol_count = 0

        result = conn.execute(
            "UPDATE session_files SET content = ?, previous_content = ?, lines = ?, symbol_count = ?, edited = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND session_id = ? AND updated_at = ?",
            (prev, current, lines, symbol_count, file_id, session_id, row_updated_at)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="File was modified concurrently — undo cancelled")
        conn.commit()
    return {"id": file_id, "content": prev, "lines": lines, "symbol_count": symbol_count, "ok": True}


@router.delete("/{session_id}/files/{file_id}")
def delete_session_file(session_id: str, file_id: str):
    """Remove a file from a session."""
    with get_db_ctx() as conn:
        conn.execute(
            "DELETE FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        )
        conn.commit()
    return {"ok": True}


@router.get("/{session_id}/files/{file_id}/preview")
def preview_session_file(session_id: str, file_id: str):
    """Serve file content for live preview with correct Content-Type."""
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT filename, content FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id),
        ).fetchone()
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


# File types that are NOT source/style text and must never enter a preview graph.
_NON_GRAPH_TYPES = {"image", "pdf", "csv", "excel"}


@router.post("/{session_id}/files/{file_id}/preview-bundle")
def preview_bundle(session_id: str, file_id: str, body: dict = None):
    """Resolve the import graph for a TSX/JSX/TS/JS file across the session.

    Returns a Sandpack-ready file map so the live preview renders with all of
    its real local imports (components + CSS) instead of empty stubs, and with
    every bare npm dependency declared so the bundler can install it.

    Body (optional): {"content": "<exact source to preview>"}. When omitted, the
    stored content is used. This lets the frontend preview the *modified* version
    of a file without first persisting it.
    """
    body = body or {}
    override = body.get("content")

    with get_db_ctx() as conn:
        target = conn.execute(
            "SELECT filename, content, file_type FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="File not found")

        if hasattr(target, "__getitem__"):
            filename = target["filename"]
            stored = target["content"]
        else:
            filename, stored = target[0], target[1]

        rows = conn.execute(
            "SELECT filename, content, file_type FROM session_files WHERE session_id = ?",
            (session_id,),
        ).fetchall()

    session_map = {}
    for r in rows:
        if hasattr(r, "__getitem__"):
            fn, ct, ft = r["filename"], r["content"], r["file_type"]
        else:
            fn, ct, ft = r[0], r[1], r[2]
        if (ft or "code") in _NON_GRAPH_TYPES:
            continue
        session_map[fn] = ct or ""

    entry_content = override if isinstance(override, str) and override.strip() else (stored or "")

    try:
        from services.preview_bundle import build_bundle
        bundle = build_bundle(filename, entry_content, session_map)
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never 500 the preview
        logger.warning(f"[preview-bundle] resolver failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=422, detail=f"preview resolve failed: {e}")

    return bundle
