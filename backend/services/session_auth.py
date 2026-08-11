"""Session ownership checks for HTTP routes (IDOR prevention).

Policy mirrors list_sessions visibility in routers/chat.py:
  WHERE s.user_id = ? OR s.user_id IS NULL

- Missing session → 404
- Empty/missing request user_id → 401
- session.user_id set and different from caller → 403
- session.user_id IS NULL → allowed (legacy rows listed to every authed user)
- session.user_id == caller → allowed
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from database import get_db_ctx


def get_request_user_id(request: Request) -> str:
    return getattr(request.state, "user_id", None) or ""


def require_session_access(session_id: str, user_id: str) -> dict:
    """Raise if user_id may not access session_id. Returns session row dict."""
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT id, user_id, title FROM chat_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    session = dict(row)
    owner = session.get("user_id") or ""
    if owner and owner != user_id:
        raise HTTPException(status_code=403, detail="Not your session")
    return session


def require_session_access_from_request(session_id: str, request: Request) -> dict:
    return require_session_access(session_id, get_request_user_id(request))


def require_credit_pause_access(pause: dict, user_id: str) -> dict:
    """Gate credit-pause mutate/read by session ownership + pause.user_id when set."""
    if not pause:
        raise HTTPException(status_code=404, detail="Credit pause not found")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session_id = pause.get("session_id") or ""
    if session_id:
        require_session_access(session_id, user_id)

    pause_owner = pause.get("user_id") or ""
    if pause_owner and pause_owner != user_id:
        raise HTTPException(status_code=403, detail="Not your credit pause")
    return pause
