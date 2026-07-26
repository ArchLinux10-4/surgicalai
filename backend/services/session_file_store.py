"""
Single source of truth for persisting file content into `session_files`.

WHY THIS MODULE EXISTS
----------------------
Every apply path in the product is keyed on a `session_files` row `id`. The
frontend applies an edit by calling::

    GET  /api/chat/{session_id}/files/{file_id}
    PUT  /api/chat/{session_id}/files/{file_id}

If a producer hands the pipeline a *synthetic* file dict with no ``id``, the
smart result carries ``file_id: ""``. The URL then collapses to
``/api/chat/{sid}/files/`` which FastAPI 307-redirects to the **list** route,
so the edit can never be applied and no error is ever surfaced.

PROVEN OCCURRENCE (session d021ff07, 2026-07-26)
------------------------------------------------
``UserManagementModal.jsx`` was not in the session and could not be fetched
from GitHub (``gh_session_register_error`` x8, HTTP 404 — the file genuinely
is not in the repo). The pipeline paused, the user pasted the file
(``agent_filereq_pause_provided``, content_len=51183), and the run produced two
QA-clean edits (``resolution_summary`` resolved_count=2,
``smart_result_emitted``). Those edits could never be applied: the request log
shows only ``GET /api/chat/<sid>/files/ 307 Temporary Redirect`` and never a
``PUT``. Only 5 files were in that session, so rate limiting was not involved.

Anything that introduces file content into a session MUST route through
``register_session_file`` so a real row (and therefore a real ``id``) exists.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _safe_dlog(dlog: Optional[Callable], event: str, **kw) -> None:
    """Best-effort structured debug log — never raise from logging."""
    if dlog is None:
        return
    try:
        dlog(event, **kw)
    except Exception:  # noqa: BLE001
        pass


def session_content_bytes(conn, session_id: str) -> int:
    """Total stored content length for a session.

    Uses the ``idx_session_files_session`` index added in ``database.py`` so
    this stays a bounded index-driven aggregate instead of a full table scan.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM session_files "
        "WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0] or 0)
    except Exception:  # noqa: BLE001
        return 0


def resolve_session_file_id(session_id: str, filename: str) -> Optional[str]:
    """Return the `session_files.id` for (session_id, filename), or None.

    Used as a recovery path so a producer that forgot to persist content can
    still yield a usable ``file_id`` if the row exists under that name.
    """
    if not session_id or not filename:
        return None
    try:
        from database import get_db_ctx
        with get_db_ctx() as conn:
            row = conn.execute(
                "SELECT id FROM session_files "
                "WHERE session_id = ? AND filename = ?",
                (session_id, filename),
            ).fetchone()
        if not row:
            return None
        return row["id"] if hasattr(row, "__getitem__") else row[0]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[session_file_store] resolve_session_file_id failed "
            "(session=%s filename=%r): %s", str(session_id)[:8], filename, e)
        return None


def register_session_file(
    session_id: str,
    filename: str,
    content: str,
    *,
    origin: str = "uploaded",
    file_type: Optional[str] = None,
    language: Optional[str] = None,
    dlog: Optional[Callable] = None,
) -> Optional[dict]:
    """Upsert `content` as a real `session_files` row and return its entry.

    Returns a dict in the same shape the pipeline expects for a session-file
    entry — crucially including ``id`` — or ``None`` when the content could
    not be persisted (size limits, DB failure). Never raises.

    Size limits are the same ones the HTTP upload routes enforce, so a pasted
    file cannot bypass the per-file or per-session cap.
    """
    if not session_id or not filename or content is None:
        _safe_dlog(dlog, "session_file_register_skipped",
                   session_id=session_id, filename=filename,
                   reason="missing_args")
        return None

    # Reuse the exact classification/sanitisation the upload routes use.
    # Imported lazily: `routers` imports `services`, so a module-level import
    # here would create a cycle.
    try:
        from routers.session_files import (
            _get_file_type, _get_language, _sanitize_for_postgres,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[session_file_store] helper import failed: %s", e)
        _safe_dlog(dlog, "session_file_register_failed",
                   session_id=session_id, filename=filename,
                   error=f"helper_import: {e}")
        return None

    filename = _sanitize_for_postgres(filename)
    content = _sanitize_for_postgres(content)
    file_type = file_type or _get_file_type(filename)
    language = language or _get_language(filename)

    payload_bytes = len(content.encode("utf-8", errors="replace"))

    # ── Size limits (same rules as the upload endpoints) ──────────────────
    try:
        from middleware.file_validator import (
            validate_file_size, validate_session_total,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[session_file_store] validator import failed: %s", e)
        _safe_dlog(dlog, "session_file_register_failed",
                   session_id=session_id, filename=filename,
                   error=f"validator_import: {e}")
        return None

    try:
        from database import get_db_ctx
    except Exception as e:  # noqa: BLE001
        logger.warning("[session_file_store] database import failed: %s", e)
        return None

    try:
        validate_file_size(filename, payload_bytes)
    except Exception as e:  # noqa: BLE001  (HTTPException from the validator)
        _safe_dlog(dlog, "session_file_register_too_large",
                   session_id=session_id, filename=filename,
                   bytes=payload_bytes, error=str(getattr(e, "detail", e)))
        return None

    lines = len(content.splitlines())
    symbol_count = 0
    try:
        from services.ast_parser import ASTParser
        symbol_count = len(ASTParser().parse(content, filename).symbols)
    except Exception:  # noqa: BLE001 — non-code files parse to nothing
        symbol_count = 0

    try:
        with get_db_ctx() as conn:
            existing = conn.execute(
                "SELECT id FROM session_files "
                "WHERE session_id = ? AND filename = ?",
                (session_id, filename),
            ).fetchone()

            if existing:
                file_id = (existing["id"] if hasattr(existing, "__getitem__")
                           else existing[0])
                conn.execute(
                    "UPDATE session_files SET content = ?, language = ?, "
                    "lines = ?, symbol_count = ?, file_type = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (content, language, lines, symbol_count, file_type,
                     file_id),
                )
                _safe_dlog(dlog, "session_file_register_updated",
                           session_id=session_id, filename=filename,
                           file_id=file_id, bytes=payload_bytes)
            else:
                # Only a NEW row grows the session, so the cap is checked here.
                try:
                    validate_session_total(
                        session_content_bytes(conn, session_id), payload_bytes)
                except Exception as e:  # noqa: BLE001
                    _safe_dlog(dlog, "session_file_register_session_full",
                               session_id=session_id, filename=filename,
                               bytes=payload_bytes,
                               error=str(getattr(e, "detail", e)))
                    return None

                file_id = str(_uuid.uuid4())
                conn.execute(
                    "INSERT INTO session_files (id, session_id, filename, "
                    "content, language, lines, symbol_count, file_type, "
                    "origin, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (file_id, session_id, filename, content, language, lines,
                     symbol_count, file_type, origin),
                )
                _safe_dlog(dlog, "session_file_register_inserted",
                           session_id=session_id, filename=filename,
                           file_id=file_id, bytes=payload_bytes)
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[session_file_store] persist failed (session=%s filename=%r): %s",
            str(session_id)[:8], filename, e)
        _safe_dlog(dlog, "session_file_register_failed",
                   session_id=session_id, filename=filename, error=str(e)[:300])
        return None

    return {
        "id": file_id,
        "session_id": session_id,
        "filename": filename,
        "content": content,
        "file_type": file_type,
        "language": language,
        "lines": lines,
        "symbol_count": symbol_count,
        "origin": origin,
    }
