"""Server-side task-run router (v2.0).

Thin HTTP layer over services.task_runner. The runner is feature-flagged
(`server_task_runner` setting / SERVER_TASK_RUNNER env, default OFF); when
disabled, /start answers {ok: false, mode: "disabled"} and the frontend
falls back to the existing browser-driven task queue unchanged.
"""
from fastapi import APIRouter, Request

from services.pipeline import _dlog
from services import task_runner

router = APIRouter()


@router.post("/start")
async def start_run(body: dict, request: Request):
    """Start (or resume) server-side execution of a planned run.

    Body: { session_id, run_id }
    """
    session_id = (body.get("session_id") or "").strip()
    run_id = (body.get("run_id") or "").strip()
    user_id = getattr(request.state, "user_id", "") or ""
    if not session_id or not run_id:
        _dlog("runs_start_bad_request", session_id=session_id, run_id=run_id,
              user_id=user_id)
        return {"ok": False, "mode": "bad_request",
                "detail": "session_id and run_id are required"}
    try:
        result = await task_runner.start_run(session_id, run_id, user_id)
        _dlog("runs_start_result", session_id=session_id, run_id=run_id,
              user_id=user_id, **{k: v for k, v in result.items() if k != "ok"},
              ok=result.get("ok"))
        return result
    except Exception as ex:
        # The endpoint must never 500 — the client treats any non-ok response
        # as "fall back to the browser-driven queue", which always works.
        _dlog("runs_start_error", session_id=session_id, run_id=run_id,
              user_id=user_id, error=str(ex)[:300])
        return {"ok": False, "mode": "error", "detail": str(ex)[:200]}


@router.get("/status")
def get_status(request: Request, run_id: str, session_id: str = ""):
    """Live supervisor state for a run (registry-backed; task rows themselves
    are served by GET /tasks, which the UI already polls)."""
    try:
        status = task_runner.run_status(run_id)
        status["enabled"] = task_runner.server_runner_enabled()
        return status
    except Exception as ex:
        _dlog("runs_status_error", run_id=run_id, session_id=session_id,
              error=str(ex)[:200])
        return {"active": False, "run_id": run_id, "error": str(ex)[:200]}
