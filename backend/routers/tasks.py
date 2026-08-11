"""Agentic tasks router — list tasks and request cancellation.

Cancellation is DB-backed: these endpoints only set a flag. The running
execution loop (in the chat smart-stream) polls the flag and stops cleanly,
then writes a system message back into the conversation so Claude is aware.
"""
from fastapi import APIRouter, HTTPException, Request
from database import get_db_ctx
from services import task_planner
from services.pipeline import _dlog
from services.session_auth import require_session_access_from_request, get_request_user_id

router = APIRouter()


@router.get("")
def get_tasks(request: Request, session_id: str, run_id: str = None):
    """List tasks for a session (optionally scoped to a single run)."""
    require_session_access_from_request(session_id, request)
    rows = task_planner.list_tasks(session_id, run_id)
    _dlog("tasks_router_list", session_id=session_id, run_id=run_id, count=len(rows))
    return rows


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    """Request cancellation of a single task."""
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT session_id FROM agent_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    session_id = row["session_id"] if hasattr(row, "keys") else row[0]
    require_session_access_from_request(session_id, request)
    ok = task_planner.request_cancel(task_id)
    _dlog("tasks_router_cancel", task_id=task_id, ok=ok,
          user_id=get_request_user_id(request))
    return {"ok": ok, "task_id": task_id}


@router.post("/cancel-all")
def cancel_all(body: dict, request: Request):
    """Request cancellation of every non-terminal task in a session/run."""
    session_id = body.get("session_id")
    run_id = body.get("run_id")
    if not session_id:
        _dlog("tasks_router_cancel_all_missing_session")
        return {"ok": False, "cancelled": 0, "detail": "session_id required"}
    require_session_access_from_request(session_id, request)
    n = task_planner.request_cancel_all(session_id, run_id)
    _dlog("tasks_router_cancel_all", session_id=session_id, run_id=run_id, cancelled=n)
    return {"ok": True, "cancelled": n}
