"""Per-session file storage — files uploaded to a chat stay in that chat.
Supports: code, images (vision), PDFs, CSV, Excel.
"""
import difflib
import os
import uuid
import base64
import io
import logging
import datetime as _dt
from pathlib import Path
import mimetypes
from fastapi import APIRouter, HTTPException, File, UploadFile, Form, Request
from fastapi.responses import Response
from database import get_db, get_db_ctx, _dlog
from middleware.file_validator import validate_file_size, validate_session_total
from services.ast_parser import ASTParser
from services.session_auth import require_session_access_from_request

logger = logging.getLogger(__name__)

router = APIRouter()
parser = ASTParser()


def _emit_upload_ok(session_id: str, file_id: str, filename: str, file_type: str,
                    lines: int, size_bytes: int, replaced: bool, route: str) -> None:
    """Record a decisive per-attempt upload success in the exportable log.

    Never raises — an observability write must never affect the upload result.
    session_id is truncated like the rest of this module; filenames/sizes only,
    never file contents.
    """
    try:
        from debug_events import emit as _emit
        _emit(
            "session_file_upload_ok",
            session_id=(session_id[:8] if session_id else None),
            file_id=file_id,
            filename=filename,
            file_type=file_type,
            lines=lines,
            size_bytes=size_bytes,
            replaced=replaced,
            route=route,
        )
    except Exception:
        pass


def _emit_upload_rejected(session_id: str, filename: str, status: int,
                          reason: str, route: str) -> None:
    """Record a decisive per-attempt upload rejection/exception in the log.

    `reason` must be the real reason from the failing code path (validation,
    size cap, session cap, unsupported type, concurrent-modification, DB
    error, …) — never a generic placeholder. Never raises.
    """
    try:
        from debug_events import emit as _emit
        _emit(
            "session_file_upload_rejected",
            session_id=(session_id[:8] if session_id else None),
            filename=filename,
            status=status,
            reason=reason,
            route=route,
        )
    except Exception:
        pass


def _snapshot_version(conn, session_id: str, file_id: str, content: str, lines: int,
                       symbol_count: int, label: str = "Edit") -> None:
    """Append the given content as a new row in the version history table.

    Called with the CONTENT BEING REPLACED (i.e. the state right before an
    edit is written), so the history reads as a list of "what the file used
    to look like" checkpoints a user can always go back to. Never raises —
    a history-write failure must not block the primary edit from saving.
    """
    try:
        version_id = str(uuid.uuid4())
        # Explicit microsecond-precision timestamp (not SQL CURRENT_TIMESTAMP,
        # which is only second-resolution on sqlite) so versions from rapid
        # successive edits still sort correctly newest-first.
        created_at = _dt.datetime.utcnow().isoformat(sep=" ", timespec="microseconds")
        conn.execute(
            "INSERT INTO session_file_versions "
            "(id, session_id, file_id, content, lines, symbol_count, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version_id, session_id, file_id, content, lines, symbol_count, label, created_at)
        )
        _dlog("session_file_version_snapshot", session_id=session_id[:8] if session_id else None,
              file_id=file_id, label=label, lines=lines, version_id=version_id)
    except Exception as e:
        _dlog("session_file_version_snapshot_failed", session_id=session_id[:8] if session_id else None,
              file_id=file_id, error=str(e))

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".heif"}
PDF_EXTENSIONS = {".pdf"}
CSV_EXTENSIONS = {".csv", ".tsv"}
EXCEL_EXTENSIONS = {".xlsx", ".xls"}

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs",
    ".cpp", ".c", ".h", ".hpp", ".cc", ".cxx", ".m", ".mm",
    ".rb", ".php", ".swift", ".kt",
    ".html", ".css", ".scss", ".sass", ".less",
    ".json", ".jsonl", ".ndjson", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".properties",
    ".md", ".rst", ".txt", ".log",
    ".sh", ".bash", ".zsh", ".fish", ".sql",
    ".vue", ".svelte", ".astro",
    ".prisma", ".graphql", ".gql", ".proto",
    ".r", ".R", ".scala", ".dart", ".lua", ".zig", ".v", ".nim",
    ".ex", ".exs", ".erl", ".hs", ".ml", ".clj",
    ".tf", ".hcl", ".dockerfile", ".conf", ".nginx",
    ".diff", ".patch", ".tex", ".bib", ".makefile",
    ".tsv",
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
        ".cpp": "cpp", ".c": "c", ".h": "c", ".hpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
        ".m": "objectivec", ".mm": "objectivecpp",
        ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
        ".svg": "xml", ".xml": "xml",
        ".html": "html", ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less",
        ".json": "json", ".jsonl": "json", ".ndjson": "json",
        ".yaml": "yaml", ".yml": "yaml",
        ".md": "markdown", ".rst": "restructuredtext",
        ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish",
        ".sql": "sql",
        ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".env": "dotenv", ".properties": "properties",
        ".vue": "vue", ".svelte": "svelte", ".astro": "astro",
        ".prisma": "prisma", ".graphql": "graphql", ".gql": "graphql", ".proto": "protobuf",
        ".r": "r", ".R": "r", ".scala": "scala", ".dart": "dart", ".lua": "lua",
        ".zig": "zig", ".v": "v", ".nim": "nim",
        ".ex": "elixir", ".exs": "elixir", ".erl": "erlang",
        ".hs": "haskell", ".ml": "ocaml", ".clj": "clojure",
        ".tf": "terraform", ".hcl": "hcl", ".dockerfile": "dockerfile",
        ".conf": "nginx", ".nginx": "nginx",
        ".log": "log", ".diff": "diff", ".patch": "diff",
        ".tex": "latex", ".bib": "bibtex", ".makefile": "makefile",
        ".csv": "csv", ".tsv": "csv", ".xlsx": "excel", ".xls": "excel",
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
def upload_session_file(session_id: str, body: dict, request: Request):
    """Upload a file to a chat session (legacy JSON path).

    Thin observability wrapper: emits a decisive per-attempt outcome
    (`session_file_upload_ok` / `session_file_upload_rejected`) so a retry
    storm is reconstructable from the exportable log. Behaviour, status codes
    and response body are unchanged — every path still returns/raises exactly
    as before.
    """
    require_session_access_from_request(session_id, request)
    filename = body.get("filename", "untitled") if isinstance(body, dict) else "untitled"
    try:
        return _upload_session_file_impl(session_id, body)
    except HTTPException as he:
        _emit_upload_rejected(session_id, filename, he.status_code, str(he.detail), "json")
        raise
    except Exception as e:
        _emit_upload_rejected(session_id, filename, 500, f"{type(e).__name__}: {e}", "json")
        raise


def _upload_session_file_impl(session_id: str, body: dict):
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
        replaced = existing is not None

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

    _emit_upload_ok(session_id, file_id, filename, file_type, lines,
                    _payload_bytes, replaced, "json")

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
    request: Request,
    file: UploadFile = File(...),
    filename: str = Form(None),
):
    """Multipart file upload — correct approach for iOS / Android / WKWebView.

    Thin observability wrapper: emits a decisive per-attempt outcome
    (`session_file_upload_ok` / `session_file_upload_rejected`) so a retry
    storm is reconstructable. Behaviour, status codes and response body are
    unchanged.
    """
    require_session_access_from_request(session_id, request)
    _wrapper_filename = filename or (file.filename if file is not None else None) or "upload"
    try:
        return await _upload_session_file_multipart_impl(session_id, file, filename)
    except HTTPException as he:
        _emit_upload_rejected(session_id, _wrapper_filename, he.status_code, str(he.detail), "multipart")
        raise
    except Exception as e:
        _emit_upload_rejected(session_id, _wrapper_filename, 500, f"{type(e).__name__}: {e}", "multipart")
        raise


async def _upload_session_file_multipart_impl(
    session_id: str,
    file: UploadFile,
    filename: str = None,
):
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
        replaced = existing is not None

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

    _emit_upload_ok(session_id, file_id, actual_filename, file_type, lines,
                    len(raw_bytes), replaced, "multipart")

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
def list_session_files(session_id: str, request: Request):
    """List files attached to a session (metadata only, no content)."""
    require_session_access_from_request(session_id, request)
    with get_db_ctx() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, session_id, filename, language, lines, symbol_count, file_type, origin, github_meta, created_at, updated_at, github_pushed_at, edited, content, repo_path, base_sha
               FROM session_files WHERE session_id = ? ORDER BY created_at ASC""",
            (session_id,)
        ).fetchall()]
    try:
        from services.repo_apply import annotate_disk_state
        annotated = annotate_disk_state(session_id, rows)
    except Exception:
        annotated = rows
        for item in annotated:
            item["disk_state"] = None
    for item in annotated:
        item.pop("content", None)
    return annotated


@router.get("/{session_id}/files/")
@router.put("/{session_id}/files/")
@router.delete("/{session_id}/files/")
def _reject_empty_file_id(session_id: str, request: Request):
    """Fail loudly when a client sends an EMPTY file id.

    `/chat/{sid}/files/{file_id}` with an empty `file_id` collapses to
    `/chat/{sid}/files/`. Without this route FastAPI's `redirect_slashes`
    answers with `307 -> /chat/{sid}/files`, i.e. the *list* endpoint, so a
    broken apply looks like a success and returns a JSON array instead of a
    file. That is exactly how session d021ff07 lost two QA-clean edits: the
    request log shows `GET /api/chat/<sid>/files/ 307` and never a PUT.

    Returning 400 here turns that silent misroute into a visible error.
    """
    require_session_access_from_request(session_id, request)
    logger.warning(
        f"[session_files] Rejected request with EMPTY file id "
        f"(session={session_id[:8]}) — client bug: the caller built a URL from "
        f"an empty file_id."
    )
    raise HTTPException(
        status_code=400,
        detail="Missing file id — this file has no session_files row yet. "
               "Re-upload the file and try again.",
    )


@router.get("/{session_id}/files/{file_id}")
def get_session_file(session_id: str, file_id: str, request: Request):
    """Get a specific file's full content."""
    require_session_access_from_request(session_id, request)
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    return dict(row)


@router.put("/{session_id}/files/{file_id}")
def update_session_file(session_id: str, file_id: str, body: dict, request: Request):
    """Update file content after applying a change.

    Uses optimistic concurrency: the UPDATE includes AND updated_at = ?
    so a concurrent write between our SELECT and UPDATE is detected and
    returns 409 instead of silently overwriting the other change.

    Before overwriting, the CURRENT content is snapshotted into
    session_file_versions so it is never lost — the file can always be
    restored to this exact point via the version history, not just the
    single most-recent edit.
    """
    require_session_access_from_request(session_id, request)
    new_content = body.get("content", "")
    label = body.get("label") or "Edit applied"
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT content, filename, updated_at FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        ).fetchone()
        if not row:
            _dlog("session_file_update_not_found", session_id=session_id[:8] if session_id else None, file_id=file_id)
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

        prev_lines = len(prev_content.splitlines()) if prev_content else 0
        try:
            prev_smap = parser.parse(prev_content or "", filename)
            prev_symbol_count = len(prev_smap.symbols)
        except Exception:
            prev_symbol_count = 0

        # Snapshot BEFORE the overwrite — this is what makes every edit
        # reversible, not just the last one.
        _snapshot_version(conn, session_id, file_id, prev_content, prev_lines, prev_symbol_count, label=label)

        result = conn.execute(
            "UPDATE session_files SET content = ?, previous_content = ?, lines = ?, symbol_count = ?, edited = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND session_id = ? AND updated_at = ?",
            (new_content, prev_content, lines, symbol_count, file_id, session_id, row_updated_at)
        )
        if result.rowcount == 0:
            _dlog("session_file_update_conflict", session_id=session_id[:8] if session_id else None, file_id=file_id)
            raise HTTPException(status_code=409, detail="File was modified concurrently — please retry with fresh content")
        conn.commit()
    written_to_disk = False
    try:
        from services.repo_apply import maybe_auto_apply
        auto = maybe_auto_apply(session_id, file_id) or {}
        written_to_disk = bool(auto.get("ran") and auto.get("applied"))
    except Exception:
        written_to_disk = False
    return {"id": file_id, "lines": lines, "symbol_count": symbol_count, "ok": True, "written_to_disk": written_to_disk}


def _restore_content(conn, session_id: str, file_id: str, target_content: str,
                      current_content: str, filename: str, row_updated_at, restore_label: str):
    """Shared core: snapshot the current content, then overwrite with target_content.

    Used by both /undo (restores the most recent version) and
    /versions/{version_id}/restore (restores an arbitrary past version).
    Restoring is itself non-destructive — the content being replaced is
    snapshotted first, so restoring is always reversible too.
    """
    lines = len(target_content.splitlines())
    try:
        smap = parser.parse(target_content, filename)
        symbol_count = len(smap.symbols)
    except Exception:
        symbol_count = 0

    current_lines = len(current_content.splitlines()) if current_content else 0
    try:
        cur_smap = parser.parse(current_content or "", filename)
        current_symbol_count = len(cur_smap.symbols)
    except Exception:
        current_symbol_count = 0

    _snapshot_version(conn, session_id, file_id, current_content, current_lines,
                       current_symbol_count, label=restore_label)

    result = conn.execute(
        "UPDATE session_files SET content = ?, previous_content = ?, lines = ?, symbol_count = ?, edited = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND session_id = ? AND updated_at = ?",
        (target_content, current_content, lines, symbol_count, file_id, session_id, row_updated_at)
    )
    if result.rowcount == 0:
        _dlog("session_file_restore_conflict", session_id=session_id[:8] if session_id else None, file_id=file_id)
        raise HTTPException(status_code=409, detail="File was modified concurrently — restore cancelled")
    return lines, symbol_count


# Newest versions kept when a list is long. Numbers stay their real
# positions, so a version number from a shorter list still means the same row.
_HISTORY_LIST_CAP = 50
_HISTORY_CONTENT_LINES = 800
_HISTORY_DIFF_LINES = 400
_HISTORY_VIEWS = ("list", "diff")


def _history_row_get(row, key, idx):
    return row[key] if hasattr(row, "__getitem__") and not isinstance(row, tuple) else row[idx]


def _history_lookup(conn, session_id, filename, dlog):
    """The session file this name means, plus its current content.

    Exact name, then case-insensitive, then a single fuzzy match. More than
    one fuzzy match is a question, not a guess. Returns an error dict when
    the name does not resolve.
    """
    rows = conn.execute(
        "SELECT id, filename, content FROM session_files WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    file_map = {}
    contents = {}
    for row in rows:
        name = _history_row_get(row, "filename", 1)
        file_id = _history_row_get(row, "id", 0)
        file_map[name] = file_id
        contents[file_id] = _history_row_get(row, "content", 2) or ""

    file_id = file_map.get(filename)
    if file_id is None and isinstance(filename, str):
        by_lower = {name.lower(): found for name, found in file_map.items()}
        file_id = by_lower.get(filename.lower())
    if file_id is None and isinstance(filename, str):
        # Substring fuzzy match — same tolerance rule the live
        # <file_request> resolver uses (pipeline.py _fr_cands).
        candidates = [
            name for name in file_map
            if filename.lower() in name.lower() or name.lower() in filename.lower()
        ]
        if len(candidates) == 1:
            file_id = file_map[candidates[0]]
        elif len(candidates) > 1:
            dlog("agent_history_ambiguous", session_id=session_id,
                 filename=filename, candidates=candidates[:5])
            return {"found": False, "message":
                    f"'{filename}' is ambiguous — did you mean one of: "
                    f"{', '.join(candidates[:5])}? Use the exact filename."}
    if file_id is None:
        dlog("agent_history_not_found", session_id=session_id, filename=filename)
        return {"found": False, "message":
                f"No file named '{filename}' exists in this session "
                f"(current or historical). Check the filename."}
    return {"file_id": file_id, "content": contents.get(file_id) or ""}


def _history_version_index(spec, count, filename):
    """(index, error). index is into the oldest-first list. spec is required."""
    if isinstance(spec, bool):
        text = ""
    elif isinstance(spec, int):
        text = str(spec)
    else:
        text = str(spec or "").strip().lower()
    if text == "first":
        return 0, ""
    if text == "last":
        return count - 1, ""
    if text.isdigit() and int(text) > 0:
        number = int(text)
        if number > count:
            return None, (
                f"'{filename}' has {count} saved version(s). "
                f"Version {number} is outside that range. Use view list to see them."
            )
        return number - 1, ""
    shown = str(spec).strip()[:40] if spec is not None else ""
    return None, f"Version '{shown}' is not a number, first, or last."


def _history_version_text(row, number):
    """One saved version, capped, under the historical banner.

    Version 1 keeps the original-file banner, so a call with no extra fields
    and version "first" stay the same result.
    """
    content = _history_row_get(row, "content", 1) or ""
    label = _history_row_get(row, "label", 3)
    created_at = _history_row_get(row, "created_at", 4)
    lines = content.splitlines()
    truncated = len(lines) > _HISTORY_CONTENT_LINES
    content_out = (
        "\n".join(lines[:_HISTORY_CONTENT_LINES])
        + f"\n... [TRUNCATED \u2014 {len(lines) - _HISTORY_CONTENT_LINES} more lines. "
          f"Use query= to search for a specific part instead.]"
    ) if truncated else content
    if number == 1:
        banner = (
            f"\u26a0\ufe0f HISTORICAL CONTENT \u2014 ORIGINAL version from "
            f"{created_at}, label \"{label}\". This is NOT the current file "
            f"\u2014 it is the state before ALL edits made in this session. "
            f"Do not use as ground truth for what exists now."
        )
    else:
        banner = (
            f"\u26a0\xfe0f HISTORICAL CONTENT \u2014 version {number}, the file as it was "
            f"before the edit labelled \"{label}\" at {created_at}. This is NOT the "
            f"current file. Do not use as ground truth for what exists now."
        )
    return f"{banner}\n```\n{content_out}\n```", len(lines), truncated


def _history_list_text(filename, rows):
    total = len(rows)
    start = max(0, total - _HISTORY_LIST_CAP)
    if start:
        header = (
            f"HISTORICAL versions of '{filename}'. Not the current file. "
            f"Showing the newest {_HISTORY_LIST_CAP} of {total}. "
            f"{start} older version(s) were left out. "
            f"Numbers are their real positions, counting from 1."
        )
    else:
        header = (
            f"HISTORICAL versions of '{filename}'. Not the current file. "
            f"{total} saved version(s), oldest first."
        )
    lines = [header]
    for offset, row in enumerate(rows[start:]):
        label = _history_row_get(row, "label", 3) or "Edit"
        created_at = _history_row_get(row, "created_at", 4)
        stored = _history_row_get(row, "lines", 2)
        if not isinstance(stored, int) or isinstance(stored, bool):
            stored = len((_history_row_get(row, "content", 1) or "").splitlines())
        lines.append(
            f"{start + offset + 1}. before \"{label}\" at {created_at} — {stored} lines"
        )
    return "\n".join(lines)


def _history_diff_text(left_no, left_row, right_name, right_lines):
    left_content = _history_row_get(left_row, "content", 1) or ""
    label = _history_row_get(left_row, "label", 3) or "Edit"
    created_at = _history_row_get(left_row, "created_at", 4)
    header = (
        f"\u26a0\ufe0f HISTORICAL CONTENT — Version {left_no}, before '{label}' "
        f"at {created_at}, compared with {right_name}. "
        f"This is NOT the current file."
    )
    diff = "\n".join(difflib.unified_diff(
        left_content.splitlines(),
        right_lines,
        fromfile=f"version {left_no}",
        tofile=right_name,
        lineterm="",
    ))
    if not diff.strip():
        body = "No differences."
    else:
        diff_lines = diff.splitlines()
        if len(diff_lines) > _HISTORY_DIFF_LINES:
            body = (
                "\n".join(diff_lines[:_HISTORY_DIFF_LINES])
                + f"\nOutput cut at {_HISTORY_DIFF_LINES} lines."
            )
        else:
            body = diff
    return f"{header}\n{body}"


def search_file_version_history(session_id: str, filename: str, query: str = None,
                                 dlog=None, view=None, version=None,
                                 compare_to=None) -> dict:
    """Read-only lookup into session_file_versions for the history tool.

    Strictly scoped to (session_id, filename) — same isolation boundary as
    every other session_file_versions query in this file. Never raises: any
    failure degrades to a clear {"found": False, "message": ...} dict so a
    history-lookup bug can never crash the agent loop it's called from.

    A call with only filename, and an optional query, behaves as before:
    no query returns the oldest version in full, and a query searches every
    saved version. view "list" numbers the versions. view "diff" compares
    one of them with the current file, or with compare_to. version is a
    number, "first", or "last".

    A keyword search returns one match per version, oldest first, capped at
    5 matches and about 6000 characters. Each hit names its version number.
    """
    _d = dlog or (lambda *a, **k: None)

    try:
        with get_db_ctx() as conn:
            found = _history_lookup(conn, session_id, filename, _d)
            if found.get("found") is False:
                return found
            file_id = found["file_id"]
            current = found["content"]
            # id breaks ties from rows written before created_at had
            # microseconds, so a version number does not move between calls.
            vrows = conn.execute(
                "SELECT id, content, lines, label, created_at FROM session_file_versions "
                "WHERE file_id = ? AND session_id = ? ORDER BY created_at ASC, id ASC",
                (file_id, session_id)
            ).fetchall()

        if not vrows:
            _d("agent_history_no_versions", session_id=session_id,
               filename=filename, file_id=file_id)
            return {"found": False, "message":
                    f"'{filename}' has never been edited in this session — "
                    f"the CURRENT file IS the original, unchanged version."}

        view_name = str(view).strip().lower() if isinstance(view, str) and view.strip() else ""
        if view_name and view_name not in _HISTORY_VIEWS:
            return {"found": False, "message":
                    f"Unknown history view '{view}'. Use list or diff."}

        if view_name == "list":
            text = _history_list_text(filename, vrows)
            _d("agent_history_results", session_id=session_id, filename=filename,
               view="list", versions=len(vrows))
            return {"found": True, "content": text}

        wants_diff = view_name == "diff" or compare_to not in (None, "")
        has_version = version not in (None, "")
        if wants_diff:
            if not has_version:
                return {"found": False, "message":
                        f"A diff of '{filename}' needs version. Use last for the file "
                        f"right before the most recent edit, or view list to see the numbers."}
            index, error = _history_version_index(version, len(vrows), filename)
            if error:
                return {"found": False, "message": error}
            left = vrows[index]
            if compare_to in (None, ""):
                right_name = "the current file"
                right_lines = (current or "").splitlines()
            else:
                other, other_error = _history_version_index(compare_to, len(vrows), filename)
                if other_error:
                    return {"found": False, "message": other_error}
                if other == index:
                    label = _history_row_get(left, "label", 3) or "Edit"
                    return {"found": True, "content":
                            f"Version {index + 1}, before '{label}', compared with itself "
                            f"has no differences."}
                right = vrows[other]
                right_label = _history_row_get(right, "label", 3) or "Edit"
                right_at = _history_row_get(right, "created_at", 4)
                right_name = f"version {other + 1}, before '{right_label}' at {right_at}"
                right_lines = (_history_row_get(right, "content", 1) or "").splitlines()
            text = _history_diff_text(index + 1, left, right_name, right_lines)
            _d("agent_history_results", session_id=session_id, filename=filename,
               view="diff", version=index + 1)
            return {"found": True, "content": text}

        if has_version:
            index, error = _history_version_index(version, len(vrows), filename)
            if error:
                return {"found": False, "message": error}
        elif query:
            index = None
        else:
            index = 0

        if index is not None:
            text, line_count, truncated = _history_version_text(vrows[index], index + 1)
            _d("agent_history_results", session_id=session_id, filename=filename,
               query=None, version=index + 1, lines=line_count, truncated=truncated)
            return {"found": True, "content": text}

        needle = query.lower()
        matches = []
        total_chars = 0
        max_matches = 5
        max_total_chars = 6000
        context_lines = 40
        for number, row in enumerate(vrows, start=1):
            if len(matches) >= max_matches or total_chars >= max_total_chars:
                break
            content = _history_row_get(row, "content", 1) or ""
            label = _history_row_get(row, "label", 3)
            created_at = _history_row_get(row, "created_at", 4)
            lines = content.splitlines()
            for i, line in enumerate(lines):
                if needle in line.lower():
                    lo = max(0, i - context_lines)
                    hi = min(len(lines), i + context_lines + 1)
                    snippet = "\n".join(lines[lo:hi])
                    banner = (
                        f"\u26a0\ufe0f HISTORICAL CONTENT \u2014 version {number} from "
                        f"{created_at}, label \"{label}\". This is NOT the "
                        f"current file. Do not use as ground truth for what "
                        f"exists now.")
                    matches.append(
                        f"{banner}\n(lines {lo + 1}-{hi} of that version)\n"
                        f"```\n{snippet}\n```")
                    total_chars += len(matches[-1])
                    break  # one match per version keeps results small
        if not matches:
            _d("agent_history_query_no_match", session_id=session_id,
               filename=filename, query=query, versions_checked=len(vrows))
            return {"found": False, "message":
                    f"Searched {len(vrows)} historical version(s) of "
                    f"'{filename}' for \"{query}\" \u2014 no match found."}
        _d("agent_history_results", session_id=session_id, filename=filename,
           query=query, matches=len(matches),
           result_chars=sum(len(m) for m in matches))
        return {"found": True, "matches": matches}
    except Exception as e:
        _d("agent_history_error", session_id=session_id, filename=filename,
           error=str(e)[:300])
        return {"found": False, "message":
                f"Could not search file history for '{filename}': {str(e)[:150]}"}


@router.get("/{session_id}/files/{file_id}/versions")
def list_session_file_versions(session_id: str, file_id: str, request: Request):
    """List all saved versions of a file, newest first.

    Backed by session_file_versions — a real, unbounded, append-only
    history (not the old single-slot previous_content column), so every
    past state of the file is browsable and restorable, not just the last.
    """
    require_session_access_from_request(session_id, request)
    with get_db_ctx() as conn:
        file_row = conn.execute(
            "SELECT updated_at FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        ).fetchone()
        if not file_row:
            raise HTTPException(status_code=404, detail="File not found")

        rows = conn.execute(
            "SELECT id, lines, symbol_count, label, created_at FROM session_file_versions "
            "WHERE file_id = ? AND session_id = ? ORDER BY created_at DESC",
            (file_id, session_id)
        ).fetchall()

    versions = []
    for r in rows:
        if hasattr(r, "__getitem__") and not isinstance(r, tuple):
            versions.append({
                "id": r["id"], "lines": r["lines"], "symbol_count": r["symbol_count"],
                "label": r["label"], "created_at": str(r["created_at"]),
            })
        else:
            versions.append({
                "id": r[0], "lines": r[1], "symbol_count": r[2],
                "label": r[3], "created_at": str(r[4]),
            })
    _dlog("session_file_versions_listed", session_id=session_id[:8] if session_id else None,
          file_id=file_id, count=len(versions))
    return versions


@router.post("/{session_id}/files/{file_id}/versions/{version_id}/restore")
def restore_session_file_version(session_id: str, file_id: str, version_id: str,
                                 request: Request):
    """Restore a file to an arbitrary past version by id.

    Optimistic concurrency: AND updated_at = ? prevents restore from
    silently overwriting a concurrent edit made while the History panel
    was open.
    """
    require_session_access_from_request(session_id, request)
    with get_db_ctx() as conn:
        file_row = conn.execute(
            "SELECT content, filename, updated_at FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        ).fetchone()
        if not file_row:
            raise HTTPException(status_code=404, detail="File not found")
        if hasattr(file_row, "__getitem__"):
            current_content = file_row["content"]
            filename = file_row["filename"]
            row_updated_at = file_row["updated_at"]
        else:
            current_content, filename, row_updated_at = file_row[0], file_row[1], file_row[2]

        version_row = conn.execute(
            "SELECT content FROM session_file_versions WHERE id = ? AND file_id = ? AND session_id = ?",
            (version_id, file_id, session_id)
        ).fetchone()
        if not version_row:
            _dlog("session_file_version_not_found", session_id=session_id[:8] if session_id else None,
                  file_id=file_id, version_id=version_id)
            raise HTTPException(status_code=404, detail="Version not found")
        target_content = version_row["content"] if hasattr(version_row, "__getitem__") else version_row[0]

        lines, symbol_count = _restore_content(
            conn, session_id, file_id, target_content, current_content, filename,
            row_updated_at, restore_label="Before restore"
        )
        conn.commit()
    disk = "skipped"
    try:
        from services.repo_apply import sync_undo_to_disk
        disk = sync_undo_to_disk(session_id, file_id, current_content)
    except Exception:
        disk = "skipped"
    _dlog("session_file_version_restored", session_id=session_id[:8] if session_id else None,
          file_id=file_id, version_id=version_id, lines=lines)
    return {"id": file_id, "content": target_content, "lines": lines, "symbol_count": symbol_count, "ok": True, "disk": disk}


@router.post("/{session_id}/files/{file_id}/undo")
def undo_session_file(session_id: str, file_id: str, request: Request):
    """Undo — restore the file to its most recent saved version.

    Now backed by session_file_versions instead of a single previous_content
    slot, so this always targets the true last checkpoint even after many
    edits. The undo itself is snapshotted too, so it can be reversed by
    restoring a later version from the History panel if needed.
    """
    require_session_access_from_request(session_id, request)
    with get_db_ctx() as conn:
        file_row = conn.execute(
            "SELECT content, filename, updated_at FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        ).fetchone()
        if not file_row:
            raise HTTPException(status_code=404, detail="File not found")
        if hasattr(file_row, "__getitem__"):
            current_content = file_row["content"]
            filename = file_row["filename"]
            row_updated_at = file_row["updated_at"]
        else:
            current_content, filename, row_updated_at = file_row[0], file_row[1], file_row[2]

        latest_version = conn.execute(
            "SELECT id, content FROM session_file_versions WHERE file_id = ? AND session_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (file_id, session_id)
        ).fetchone()
        if not latest_version:
            _dlog("session_file_undo_no_history", session_id=session_id[:8] if session_id else None, file_id=file_id)
            raise HTTPException(status_code=400, detail="No previous version to restore")

        if hasattr(latest_version, "__getitem__"):
            target_content = latest_version["content"]
        else:
            target_content = latest_version[1]

        lines, symbol_count = _restore_content(
            conn, session_id, file_id, target_content, current_content, filename,
            row_updated_at, restore_label="Before undo"
        )
        conn.commit()
    disk = "skipped"
    try:
        from services.repo_apply import sync_undo_to_disk
        disk = sync_undo_to_disk(session_id, file_id, current_content)
    except Exception:
        disk = "skipped"
    _dlog("session_file_undo_ok", session_id=session_id[:8] if session_id else None, file_id=file_id, lines=lines)
    return {"id": file_id, "content": target_content, "lines": lines, "symbol_count": symbol_count, "ok": True, "disk": disk}


@router.delete("/{session_id}/files/{file_id}")
def delete_session_file(session_id: str, file_id: str, request: Request):
    """Remove a file from a session."""
    require_session_access_from_request(session_id, request)
    with get_db_ctx() as conn:
        conn.execute(
            "DELETE FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id)
        )
        conn.commit()
    return {"ok": True}


@router.post("/{session_id}/files/apply")
def apply_repo_files(session_id: str, request: Request, body: dict = None):
    """Write staged session files to the open folder. Drift returns a conflict."""
    require_session_access_from_request(session_id, request)
    body = body or {}
    file_ids = body.get("file_ids")
    if file_ids is not None and not isinstance(file_ids, list):
        raise HTTPException(status_code=400, detail="file_ids must be a list")
    from services.repo_apply import apply_files
    user_id = getattr(request.state, "user_id", "") or ""
    return apply_files(session_id, user_id, file_ids, force=bool(body.get("force")))


@router.post("/{session_id}/files/import-folder")
def import_folder(session_id: str, request: Request, body: dict = None):
    """Bulk-import every code file from a local folder into this session,
    so Chat/Edit/Agent can see an entire project at once (like opening a
    folder in Cursor), instead of uploading files one at a time.

    Reuses the exact same sandboxing, ignore-lists, and size limits as the
    existing single-file upload path and the local file browser — nothing
    new is introduced, this only adds a bulk caller on top:
      - Path must resolve inside the configured workspace_path (same
        `_safe_path` boundary check as the file browser in files.py).
      - Same IGNORED_DIRS as the file browser (node_modules, venv, .git,
        dist, build, __pycache__, etc. are never imported).
      - Only recognized code/text extensions (CODE_EXTENSIONS) are
        imported — binaries/images/unknown types are skipped.
      - Same per-file (1MB) and per-session (30MB) byte limits as the
        single upload path (middleware/file_validator.py).
      - Files with edited=1 (you or the AI already changed them in this
        session) are never overwritten — in-session edits always win.
      - Hard cap of MAX_IMPORT_FILES per call so a mistaken huge directory
        can't hang the request.
    """
    require_session_access_from_request(session_id, request)
    from routers.files import IGNORED_DIRS, IGNORED_EXTS, _get_workspace_root, _safe_path
    from services.repositories import repository_for_session, resolve_inside
    from services.repo_apply import content_sha

    MAX_IMPORT_FILES = 500

    body = body or {}
    requested_path = (body.get("path") or "").strip()
    session_repo = repository_for_session(session_id)
    repo_root = ""
    if session_repo and session_repo.get("root_path") and not session_repo.get("missing"):
        repo_root = os.path.realpath(session_repo["root_path"])
        workspace = repo_root
        target = resolve_inside(repo_root, requested_path) if requested_path else Path(repo_root)
    else:
        workspace = _get_workspace_root()
        target = _safe_path(requested_path) if requested_path else Path(workspace)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {target}")

    # ── Walk & collect candidate files ────────────────────────────────────
    candidates: list[Path] = []
    truncated = False
    for root, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        for fname in sorted(files):
            ext = Path(fname).suffix.lower()
            if ext in IGNORED_EXTS or ext not in CODE_EXTENSIONS:
                continue
            candidates.append(Path(root) / fname)
            if len(candidates) >= MAX_IMPORT_FILES:
                truncated = True
                break
        if truncated:
            break

    imported, skipped_edited, skipped_too_large, skipped_session_cap, failed = [], [], [], [], []

    with get_db_ctx() as conn:
        session_total = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM session_files WHERE session_id = ?",
            (session_id,)
        ).fetchone()[0]
        existing_rows = {}
        for r in conn.execute(
            "SELECT filename, id, edited FROM session_files WHERE session_id = ?", (session_id,)
        ).fetchall():
            fname = r["filename"] if hasattr(r, "__getitem__") else r[0]
            fid = r["id"] if hasattr(r, "__getitem__") else r[1]
            fedited = r["edited"] if hasattr(r, "__getitem__") else r[2]
            existing_rows[fname] = (fid, fedited)

        for path in candidates:
            try:
                rel_name = str(path.relative_to(workspace)).replace(os.sep, "/")
            except ValueError:
                rel_name = path.name

            try:
                raw_bytes = path.read_bytes()
            except Exception as e:
                failed.append({"filename": rel_name, "reason": str(e)})
                continue

            if rel_name in existing_rows and existing_rows[rel_name][1]:
                skipped_edited.append(rel_name)
                continue

            try:
                validate_file_size(rel_name, len(raw_bytes))
            except HTTPException:
                skipped_too_large.append(rel_name)
                continue

            try:
                validate_session_total(session_total, len(raw_bytes))
            except HTTPException:
                skipped_session_cap.append(rel_name)
                continue

            try:
                content = raw_bytes.decode("utf-8", errors="replace")
            except Exception:
                content = raw_bytes.decode("latin-1", errors="replace")
            content = _sanitize_for_postgres(content)

            lines = len(content.splitlines())
            try:
                smap = parser.parse(content, rel_name)
                symbol_count = len(smap.symbols)
            except Exception:
                symbol_count = 0

            language = _get_language(rel_name)
            session_total += len(raw_bytes)

            tracked_path = rel_name if repo_root else None
            tracked_sha = content_sha(content) if repo_root else None
            if rel_name in existing_rows:
                file_id = existing_rows[rel_name][0]
                if repo_root:
                    conn.execute(
                        "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ?, "
                        "file_type = 'code', repo_path = ?, base_sha = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (content, language, lines, symbol_count, tracked_path, tracked_sha, file_id)
                    )
                else:
                    conn.execute(
                        "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ?, "
                        "file_type = 'code', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (content, language, lines, symbol_count, file_id)
                    )
            else:
                file_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO session_files (id, session_id, filename, content, language, lines, "
                    "symbol_count, file_type, origin, repo_path, base_sha, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'code', 'uploaded', ?, ?, CURRENT_TIMESTAMP)",
                    (file_id, session_id, rel_name, content, language, lines, symbol_count, tracked_path, tracked_sha)
                )
            imported.append(rel_name)

        conn.commit()

    logger.warning(
        f"[import-folder] session={session_id[:8]} path={target} "
        f"imported={len(imported)} skipped_edited={len(skipped_edited)} "
        f"skipped_too_large={len(skipped_too_large)} skipped_session_cap={len(skipped_session_cap)} "
        f"failed={len(failed)} candidates_seen={len(candidates)} truncated={truncated}"
    )

    return {
        "imported_count": len(imported),
        "imported": imported,
        "skipped_edited": skipped_edited,
        "skipped_too_large": skipped_too_large,
        "skipped_session_cap": skipped_session_cap,
        "failed": failed,
        "truncated": truncated,
        "folder": str(target),
    }


@router.get("/{session_id}/files/{file_id}/preview")
def preview_session_file(session_id: str, file_id: str, request: Request):
    """Serve file content for live preview with correct Content-Type."""
    require_session_access_from_request(session_id, request)
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
def preview_bundle(session_id: str, file_id: str, request: Request, body: dict = None):
    """Resolve the import graph for a TSX/JSX/TS/JS file across the session.

    Returns a Sandpack-ready file map so the live preview renders with all of
    its real local imports (components + CSS) instead of empty stubs, and with
    every bare npm dependency declared so the bundler can install it.

    Body (optional): {"content": "<exact source to preview>"}. When omitted, the
    stored content is used. This lets the frontend preview the *modified* version
    of a file without first persisting it.
    """
    require_session_access_from_request(session_id, request)
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
