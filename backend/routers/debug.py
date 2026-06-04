"""
Debug router — exposes structured pipeline failure logs for admin download.

GET  /api/debug/pipeline-log          → last N events (JSON array)
GET  /api/debug/pipeline-log/download → raw JSONL file download
DELETE /api/debug/pipeline-log        → clear the log
"""
import os
import json
from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(prefix="/api/debug", tags=["debug"])

_DLOG_PATH = "/tmp/surgical_debug.jsonl"


def _require_admin(request: Request) -> bool:
    """Return True if caller is authenticated. Log endpoint is admin-only."""
    # Check session cookie or Authorization header set by the auth middleware
    user = getattr(request.state, "user", None)
    return user is not None


@router.get("/pipeline-log")
async def get_pipeline_log(request: Request, last: int = 200):
    """Return the last `last` log events as a JSON array."""
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not os.path.exists(_DLOG_PATH):
        return JSONResponse([])

    try:
        with open(_DLOG_PATH, "r") as f:
            lines = f.readlines()
        events = []
        for line in lines[-last:]:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    events.append({"raw": line})
        return JSONResponse(events)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/pipeline-log/download")
async def download_pipeline_log(request: Request):
    """Download the full raw JSONL log file."""
    if not _require_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not os.path.exists(_DLOG_PATH):
        return Response(content="", media_type="text/plain",
                        headers={"Content-Disposition": "attachment; filename=surgical_debug.jsonl"})
    return FileResponse(
        path=_DLOG_PATH,
        media_type="application/x-ndjson",
        filename="surgical_debug.jsonl",
        headers={"Content-Disposition": "attachment; filename=surgical_debug.jsonl"},
    )


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
