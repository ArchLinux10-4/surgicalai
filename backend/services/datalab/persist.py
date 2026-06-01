"""
DataLab persistence — save a transform result as a new versioned file.

Two things are written, atomically from the caller's perspective:
  1. datalab_artifacts row  — the real output BYTES (round-trippable, downloadable)
  2. session_files row       — the visible File-Drawer entry (origin='edited'),
                               content = markdown preview, file_type csv/excel

The session_files insert mirrors routers/session_files.py exactly so edited
files behave identically to uploads in every UI surface.
"""
from __future__ import annotations

import uuid
from typing import List, Optional

from database import get_db
from . import store
from .writer import write_result, result_to_markdown, versioned_name


def _sanitize_for_postgres(s: str) -> str:
    """Strip NUL bytes (Postgres TEXT cannot store them)."""
    return s.replace("\x00", "") if s else s


def persist_result(
    *,
    session_id: str,
    source_file_id: str,
    source_filename: str,
    source_kind: str,
    source_delimiter: str,
    columns: List[str],
    rows: List[List[str]],
    transform_sql: str = "",
    origin: str = "edited",
    sheet_name: str = "Result",
) -> dict:
    """Write bytes + artifact + session_files row. Returns a descriptor dict."""
    data, ext, mime, file_type = write_result(
        columns, rows, source_kind, source_delimiter, sheet_name
    )
    out_filename, version = versioned_name(source_filename, ext)
    preview = _sanitize_for_postgres(result_to_markdown(columns, rows, out_filename))

    # 2. visible session_files row (created first so its id backs the artifact)
    file_id = str(uuid.uuid4())

    # 1. raw bytes artifact — keyed 1:1 to the NEW session_files row id so any
    #    version can be downloaded or re-edited directly.
    artifact_id = store.save_artifact(
        session_id=session_id,
        filename=out_filename,
        raw=data,
        origin=origin,
        mime=mime,
        file_id=file_id,
        version=1,
        parent_id=source_file_id,
        transform_sql=transform_sql,
    )
    lines = preview.count("\n") + 1
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, language, "
            "lines, symbol_count, file_type, origin, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (file_id, session_id, out_filename, preview, "spreadsheet",
             lines, 0, file_type, origin),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "file_id": file_id,
        "artifact_id": artifact_id,
        "filename": out_filename,
        "file_type": file_type,
        "mime": mime,
        "version": version,
        "origin": origin,
        "byte_size": len(data),
        "row_count": len(rows),
        "column_count": len(columns),
    }


def register_upload_artifact(
    *,
    session_id: str,
    source_file_id: str,
    filename: str,
    raw: bytes,
    mime: str = "",
) -> Optional[str]:
    """
    Persist the ORIGINAL uploaded bytes so the workbook can be round-tripped.
    Best-effort: returns artifact id, or None if it could not be stored
    (e.g. too large) — never raises into the upload path.
    """
    try:
        return store.save_artifact(
            session_id=session_id,
            filename=filename,
            raw=raw,
            origin="uploaded",
            mime=mime,
            file_id=source_file_id,
            version=1,
        )
    except Exception:
        return None
