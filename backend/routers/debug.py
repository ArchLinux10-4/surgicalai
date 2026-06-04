"""
Debug router — exposes structured pipeline failure logs for agent download.
GET /api/debug/pipeline-log?lines=200
Returns last N lines of /tmp/pipeline_failures.jsonl as JSON array.
Auth is enforced by the global JWT middleware in main.py (request.state.user_id).
"""
import os
import json
from fastapi import APIRouter, Request, Query

router = APIRouter()

LOG_PATH = "/tmp/pipeline_failures.jsonl"


@router.get("/pipeline-log")
async def get_pipeline_log(
    request: Request,
    lines: int = Query(default=200, ge=1, le=2000),
):
    """Return the last N structured pipeline failure events."""
    if not os.path.exists(LOG_PATH):
        return {"events": [], "message": "No pipeline failures logged yet."}

    try:
        with open(LOG_PATH, "r") as f:
            all_lines = f.readlines()

        recent = all_lines[-lines:]
        events = []
        for line in recent:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    events.append({"raw": line})

        return {"events": events, "total_in_file": len(all_lines)}
    except Exception as e:
        return {"events": [], "error": str(e)}


@router.delete("/pipeline-log")
async def clear_pipeline_log(request: Request):
    """Clear the pipeline failure log."""
    try:
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)
        return {"cleared": True}
    except Exception as e:
        return {"cleared": False, "error": str(e)}
