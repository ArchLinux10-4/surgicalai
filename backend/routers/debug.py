"""
Debug router — exposes structured pipeline debug logs from the database.

GET  /api/debug/pipeline-log              → last N events (JSON)
GET  /api/debug/pipeline-log?session_id=X → filter by session
GET  /api/debug/pipeline-log?user_id=X    → filter by user
GET  /api/debug/pipeline-log/download     → JSONL download
DELETE /api/debug/pipeline-log            → clear the log (or entries older than N days)
"""
import json
from typing import Optional
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse, Response
from fastapi import HTTPException
from database import get_db
from auth_utils import decode_token

router = APIRouter(prefix="/api/debug", tags=["debug"])


def _require_admin(request: Request):
    """Require actual admin role — not just 'any logged-in user'."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/pipeline-log")
async def get_pipeline_log(
    request: Request,
    last: int = Query(500, ge=1, le=5000),
    session_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
):
    """Return the last `last` log events from the database, optionally filtered."""
    _require_admin(request)

    try:
        conn = get_db()

        # Build query with optional filters
        conditions = []
        params = []
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if user_id:
            conditions.append("user_id = ?")
            params.append(str(user_id))

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        # Get total count
        total_row = conn.execute(f"SELECT COUNT(*) as cnt FROM debug_events{where}", params).fetchone()
        total = total_row["cnt"] if total_row else 0

        # Get last N events ordered by created_at
        rows = conn.execute(
            f"SELECT data FROM debug_events{where} ORDER BY created_at DESC LIMIT ?",
            params + [last],
        ).fetchall()
        conn.close()

        # Parse JSON data column back to dicts
        events = []
        for row in reversed(rows):  # reverse to get chronological order
            try:
                events.append(json.loads(row["data"]))
            except Exception:
                events.append({"raw": row["data"]})

        return JSONResponse({
            "events": events,
            "total": total,
            "filtered": total,
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
    """Download the log as JSONL from the database. Supports session_id/user_id filters.
    Also accepts ?token= for browser direct-link access without Authorization header.
    """
    # Primary auth: middleware-populated state
    is_admin = getattr(request.state, "is_admin", False)
    # Fallback: ?token= query param (browser direct-link)
    if not is_admin and token:
        try:
            payload = decode_token(token)
            is_admin = bool(payload.get("is_admin", False))
        except Exception:
            is_admin = False
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        conn = get_db()

        conditions = []
        params = []
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if user_id:
            conditions.append("user_id = ?")
            params.append(str(user_id))

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            f"SELECT data FROM debug_events{where} ORDER BY created_at ASC", params
        ).fetchall()
        conn.close()

        lines = [row["data"] for row in rows]
        content = "\n".join(lines)

        fname = f"surgical_debug{'_' + session_id[:8] if session_id else ''}{'_u' + str(user_id) if user_id else ''}.jsonl"
        return Response(
            content=content,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename={fname}"},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/pipeline-log")
async def clear_pipeline_log(
    request: Request,
    older_than_days: Optional[int] = Query(None, ge=1, le=365),
):
    """Clear debug log entries.
    - No params: wipe everything
    - ?older_than_days=7: only expire entries older than N days
    """
    _require_admin(request)
    try:
        conn = get_db()
        if older_than_days:
            import datetime
            cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=older_than_days)).isoformat()
            conn.execute("DELETE FROM debug_events WHERE created_at < ?", (cutoff,))
        else:
            conn.execute("DELETE FROM debug_events")
        conn.commit()
        conn.close()
        return JSONResponse({"cleared": True, "scope": f"older_than_{older_than_days}_days" if older_than_days else "all"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
