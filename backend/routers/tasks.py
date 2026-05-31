"""Agentic tasks router — list tasks and request cancellation.

Cancellation is DB-backed: these endpoints only set a flag. The running
execution loop (in the chat smart-stream) polls the flag and stops cleanly,
then writes a system message back into the conversation so Claude is aware.
"""
from fastapi import APIRouter, Request
from services import task_planner

router = APIRouter()


@router.get("")
def get_tasks(request: Request, session_id: str, run_id: str = None):
    """List tasks for a session (optionally scoped to a single run)."""
    return task_planner.list_tasks(session_id, run_id)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    """Request cancellation of a single task."""
    ok = task_planner.request_cancel(task_id)
    return {"ok": ok, "task_id": task_id}


@router.post("/cancel-all")
def cancel_all(body: dict, request: Request):
    """Request cancellation of every non-terminal task in a session/run."""
    session_id = body.get("session_id")
    run_id = body.get("run_id")
    if not session_id:
        return {"ok": False, "cancelled": 0, "detail": "session_id required"}
    n = task_planner.request_cancel_all(session_id, run_id)
    return {"ok": True, "cancelled": n}
