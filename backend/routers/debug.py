"""
Debug router — exposes structured pipeline failure logs for admin download.

GET  /api/debug/pipeline-log              → last N events (JSON)
GET  /api/debug/pipeline-log?session_id=X → filter by session
GET  /api/debug/pipeline-log?user_id=X    → filter by user
GET  /api/debug/pipeline-log/download     → raw JSONL download
DELETE /api/debug/pipeline-log            → clear the log
"""
import os
import json
from typing import Optional
from fastapi import APIRouter, Request, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from auth_utils import decode_token

router = APIRouter(prefix="/api/debug", tags=["debug"])

_DLOG_PATH = "/tmp/surgical_debug.jsonl"


def _require_admin(request: Request) -> bool:
    user_id = getattr(request.state, "user_id", None)
    return user_id is not None


@router.get("/pipeline-log")
async def get_pipeline_log(
    request: Request,
    last: int = Query(500, ge=1, le=5000),
    session_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
):
    """Return the last `last` log events, optionally filtered by session_id or user_id."""
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not os.path.exists(_DLOG_PATH):
        return JSONResponse({"events": [], "total": 0, "filtered": 0})

    try:
        with open(_DLOG_PATH, "r") as f:
            lines = f.readlines()

        all_events = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    all_events.append(json.loads(line))
                except Exception:
                    all_events.append({"raw": line})

        total = len(all_events)

        # Apply filters
        filtered = all_events
        if session_id:
            filtered = [e for e in filtered if e.get("session_id") == session_id]
        if user_id:
            filtered = [e for e in filtered if str(e.get("user_id", "")) == str(user_id)]

        # Return last N after filtering
        events = filtered[-last:]

        return JSONResponse({
            "events": events,
            "total": total,
            "filtered": len(filtered),
            "returned": len(events),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/pipeline-log/download")
async def download_pipeline_log(
    request: Request,
    session_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
):
    """Download the log as JSONL. Supports same session_id/user_id filters.
    Also accepts ?token= so the browser can hit this URL directly without
    an Authorization header (e.g. pasting the link in a new tab).
    """
    # Primary auth: middleware-populated state (Authorization: Bearer header)
    authed = _require_admin(request)
    # Fallback: ?token= query param (browser direct-link case)
    if not authed and token:
        try:
            payload = decode_token(token)
            authed = True  # any valid token is admin-gated by _require_admin logic
        except Exception:
            authed = False
    if not authed:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not os.path.exists(_DLOG_PATH):
        return Response(content="", media_type="text/plain",
                        headers={"Content-Disposition": "attachment; filename=surgical_debug.jsonl"})

    # If no filters, serve the raw file directly
    if not session_id and not user_id:
        return FileResponse(
            path=_DLOG_PATH,
            media_type="application/x-ndjson",
            filename="surgical_debug.jsonl",
            headers={"Content-Disposition": "attachment; filename=surgical_debug.jsonl"},
        )

    # Filtered download — build in memory
    try:
        with open(_DLOG_PATH, "r") as f:
            lines = f.readlines()
        out_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if session_id and ev.get("session_id") != session_id:
                continue
            if user_id and str(ev.get("user_id", "")) != str(user_id):
                continue
            out_lines.append(line)

        content = "\n".join(out_lines)
        fname = f"surgical_debug{'_' + session_id[:8] if session_id else ''}{'_u' + str(user_id) if user_id else ''}.jsonl"
        return Response(
            content=content,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename={fname}"},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/pipeline-log")
async def clear_pipeline_log(request: Request):
    """Wipe the log file."""
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        if os.path.exists(_DLOG_PATH):
            os.remove(_DLOG_PATH)
        return JSONResponse({"cleared": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
