"""
DataLab router — spreadsheet/CSV power-house HTTP surface.

Fully gated behind the DATALAB_ENABLED flag: every endpoint 404s when the flag
is off, so an inert deploy is indistinguishable from today. None of the code-
surgery routes are touched.

Endpoints (mounted at /api/datalab):
  GET  /enabled                          -> {enabled: bool}
  POST /{session_id}/transform           -> run a NL transform, persist result
  GET  /{session_id}/download/{file_id}  -> download a spreadsheet's bytes
  GET  /{session_id}/versions/{file_id}  -> list version history
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import io

from database import get_db
from services.datalab.config import datalab_enabled
from services.datalab import store, persist
from services.datalab.loader import load_workbook, LoadError
from services.datalab.transform import run_transform
from services.session_auth import require_session_access_from_request

logger = logging.getLogger("datalab")
router = APIRouter()


def _guard():
    if not datalab_enabled():
        raise HTTPException(status_code=404, detail="DataLab is not enabled")


def _verify_file_in_session(session_id: str, file_id: str):
    """Prevent IDOR: a file's bytes/versions are only reachable through the
    session that owns it. Mirrors the WHERE id=? AND session_id=? scoping used
    everywhere in session_files.py."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM session_files WHERE id = ? AND session_id = ?",
            (file_id, session_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="File not found in this session")
    finally:
        conn.close()


@router.get("/enabled")
def enabled():
    return {"enabled": datalab_enabled()}


@router.post("/{session_id}/transform")
def transform_file(session_id: str, body: dict, request: Request):
    _guard()
    require_session_access_from_request(session_id, request)
    file_id = body.get("file_id")
    prompt = (body.get("prompt") or "").strip()
    if not file_id or not prompt:
        raise HTTPException(status_code=400, detail="file_id and prompt are required")

    _verify_file_in_session(session_id, file_id)

    artifact_id = store.artifact_id_for_file(file_id)
    if not artifact_id:
        raise HTTPException(
            status_code=404,
            detail="No spreadsheet data found for this file. Re-upload the file.",
        )
    raw = store.get_artifact_bytes(artifact_id)
    meta = store.get_artifact_meta(artifact_id) or {}
    filename = meta.get("filename", "workbook.xlsx")

    try:
        wb = load_workbook(raw, filename, meta.get("mime", ""))
    except LoadError as e:
        raise HTTPException(status_code=422, detail=f"Could not read spreadsheet: {e}")

    user_id = getattr(request.state, "user_id", "") or ""
    delim = wb.sheets[0].delimiter or "," if wb.sheets else ","

    try:
        result = run_transform(wb, prompt, user_id=user_id)
    except Exception as e:  # API/config errors surface cleanly
        logger.warning(f"[datalab] transform error: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail=str(e))

    if not result.ok:
        # Hard gate: nothing ships. Return the trail for visibility.
        return {
            "ok": False,
            "error": result.error,
            "attempts": result.attempts,
            "trail": result.trail,
            "qa": _qa_dict(result.qa),
        }

    desc = persist.persist_result(
        session_id=session_id,
        source_file_id=file_id,
        source_filename=filename,
        source_kind=wb.kind,
        source_delimiter=delim,
        columns=result.columns,
        rows=result.rows,
        transform_sql=result.sql or "",
        sheet_name=(wb.sheets[0].name if wb.sheets else "Result"),
    )
    return {
        "ok": True,
        "file": desc,
        "qa": _qa_dict(result.qa),
        "sql": result.sql,
        "attempts": result.attempts,
        "trail": result.trail,
    }


@router.get("/{session_id}/download/{file_id}")
def download_file(session_id: str, file_id: str, request: Request):
    _guard()
    require_session_access_from_request(session_id, request)
    _verify_file_in_session(session_id, file_id)
    artifact_id = store.artifact_id_for_file(file_id)
    if not artifact_id:
        raise HTTPException(status_code=404, detail="No data for this file")
    raw = store.get_artifact_bytes(artifact_id)
    meta = store.get_artifact_meta(artifact_id) or {}
    filename = meta.get("filename", "download.xlsx")
    mime = meta.get("mime") or "application/octet-stream"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(io.BytesIO(raw), media_type=mime, headers=headers)


@router.get("/{session_id}/versions/{file_id}")
def versions(session_id: str, file_id: str, request: Request):
    _guard()
    require_session_access_from_request(session_id, request)
    _verify_file_in_session(session_id, file_id)
    return {"versions": store.list_versions(file_id)}


def _qa_dict(qa):
    if qa is None:
        return None
    return {
        "passed": qa.passed,
        "score": qa.score,
        "verdict": qa.verdict,
        "issues": qa.issues,
        "warnings": qa.warnings,
        "diff": qa.diff,
    }
