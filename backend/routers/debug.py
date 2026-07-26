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
from database import get_db_ctx, _dlog
from auth_utils import decode_token

router = APIRouter(prefix="/api/debug", tags=["debug"])

# ── Client-event ingest limits (enforced server-side) ─────────────────────────
_CLIENT_EVENT_MAX_BODY_BYTES = 4 * 1024   # cap serialized body at 4 KB
_CLIENT_EVENT_MAX_KEYS = 30               # cap number of keys in `data`
_CLIENT_EVENT_MAX_STR = 500               # truncate any string value/key to this
_CLIENT_EVENT_PREFIX = "client_"          # make browser events unmistakable


def _sanitize_client_data(data) -> dict:
    """Bound a client-supplied `data` object: keep only a dict, cap key count,
    truncate long string keys/values, and coerce non-primitive values to a
    truncated string. Never trusts client size claims."""
    if not isinstance(data, dict):
        return {"_nonobject": str(data)[:_CLIENT_EVENT_MAX_STR]}
    out = {}
    for i, (k, v) in enumerate(data.items()):
        if i >= _CLIENT_EVENT_MAX_KEYS:
            out["_truncated_keys"] = True
            break
        key = str(k)[:_CLIENT_EVENT_MAX_STR]
        if isinstance(v, str):
            out[key] = v[:_CLIENT_EVENT_MAX_STR]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[key] = v
        else:
            out[key] = str(v)[:_CLIENT_EVENT_MAX_STR]
    return out


def _require_admin(request: Request):
    """Require actual admin role — not just 'any logged-in user'."""
    if not getattr(request.state, "is_admin", False):
        _dlog("debug_admin_check_failed", user_id=getattr(request.state, "user_id", ""))
        raise HTTPException(status_code=403, detail="Admin access required")


@router.post("/client-event")
async def client_event(request: Request):
    """Ingest a single browser-side event into the EXPORTABLE debug log.

    Lets normal (non-admin) authenticated users report their own front-end
    failures — upload retries, paste/TEXT-path errors — so browser-side
    problems finally reach the log the user can download. This closes the gap
    where client failures were unprovable.

    Security / abuse controls:
      • Requires an authenticated user (the global auth middleware populates
        request.state.user_id). NOT admin — reading/clearing still is.
      • user_id is stamped from request.state, NEVER from the client body.
      • Serialized body capped at a few KB → 413 (logged). Key count capped,
        long strings truncated.
      • No exemption from the general rate limit (applied in middleware) so it
        can't become an amplification vector.
    """
    user_id = getattr(request.state, "user_id", "") or ""

    # Enforce a hard body-size cap server-side (don't trust any client claim).
    try:
        raw = await request.body()
    except Exception:
        raw = b""
    if len(raw) > _CLIENT_EVENT_MAX_BODY_BYTES:
        try:
            from debug_events import emit as _emit
            _emit(
                "client_event_rejected_too_large",
                user_id=user_id,
                body_bytes=len(raw),
                limit=_CLIENT_EVENT_MAX_BODY_BYTES,
            )
        except Exception:
            pass
        raise HTTPException(status_code=413, detail="Client event body too large")

    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    event_name = str(body.get("event", "unknown"))[:_CLIENT_EVENT_MAX_STR]
    data = _sanitize_client_data(body.get("data", {}))

    # Prefix so client events are unmistakable; stamp the SERVER-known user_id.
    try:
        from debug_events import emit as _emit
        _emit(
            f"{_CLIENT_EVENT_PREFIX}{event_name}",
            user_id=user_id,
            session_id=str(body.get("session_id", ""))[:_CLIENT_EVENT_MAX_STR],
            data=data,
        )
    except Exception:
        pass  # ingest is best-effort; never fail the client's report call

    return JSONResponse({"ok": True})


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
        with get_db_ctx() as conn:
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
        _dlog("debug_get_pipeline_log_failed", session_id=session_id, user_id=user_id, error=str(e))
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
        with get_db_ctx() as conn:
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

        lines = [row["data"] for row in rows]
        content = "\n".join(lines)

        fname = f"surgical_debug{'_' + session_id[:8] if session_id else ''}{'_u' + str(user_id) if user_id else ''}.jsonl"
        return Response(
            content=content,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename={fname}"},
        )
    except Exception as e:
        _dlog("debug_download_pipeline_log_failed", session_id=session_id, user_id=user_id, error=str(e))
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
        with get_db_ctx() as conn:
            if older_than_days:
                import datetime
                cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=older_than_days)).isoformat()
                conn.execute("DELETE FROM debug_events WHERE created_at < ?", (cutoff,))
            else:
                conn.execute("DELETE FROM debug_events")
            conn.commit()
        _dlog("debug_pipeline_log_cleared", older_than_days=older_than_days,
              cleared_by=getattr(request.state, "user_id", ""))
        return JSONResponse({"cleared": True, "scope": f"older_than_{older_than_days}_days" if older_than_days else "all"})
    except Exception as e:
        _dlog("debug_clear_pipeline_log_failed", older_than_days=older_than_days, error=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)
