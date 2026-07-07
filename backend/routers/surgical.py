"""Surgical analysis and apply router."""
import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from models.schemas import (
    SurgicalAnalyzeRequest, SurgicalAnalyzeResponse,
    SurgicalApplyRequest, SurgicalApplyResponse,
    SurgicalChange
)
from database import get_setting, get_db_ctx
from services.pipeline import analyze_and_plan, analyze_and_plan_stream
from services.surgical_editor import apply_changes_to_file
from services.edit_rescue import rescue_failed_changes

router = APIRouter()


def _attempt_rescue(req: SurgicalApplyRequest, request: Request,
                    result: SurgicalApplyResponse) -> SurgicalApplyResponse:
    """AI rescue tier: try to recover changes the deterministic engine failed.

    Runs only when there are failures AND we have current content to work on.
    Validated strictly (unique exact-match search) — see services/edit_rescue.
    """
    if not result.failed_changes or result.modified_content is None:
        return result
    user_id = getattr(request.state, "user_id", None) or ""
    new_content, rescued, still_failed = rescue_failed_changes(
        file_path=req.file_path,
        file_content=result.modified_content,
        all_changes=req.changes,
        failed_changes=result.failed_changes,
        user_id=user_id,
    )
    if rescued:
        result.modified_content = new_content
        result.new_content = new_content
        result.applied_count += len(rescued)
        result.rescued_count = len(rescued)
        result.rescued_changes = rescued
        # Disk mode: the engine already wrote the pre-rescue content — persist
        # the rescued content too so disk and response never diverge.
        if not result.cloud_mode:
            try:
                with open(req.file_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
            except OSError:
                pass
    result.failed_changes = still_failed
    result.failed_count = len(still_failed)
    return result


def _rescue_after_total_failure(req: SurgicalApplyRequest, request: Request,
                                error: ValueError,
                                change_ids=None) -> SurgicalApplyResponse:
    """Every change failed (engine raised). Last-ditch AI rescue from the
    original content. Re-raises the original error if nothing is recovered."""
    if req.file_content is None:
        raise HTTPException(status_code=409, detail=str(error))
    wanted = [c for c in req.changes
              if change_ids is None or c.id in change_ids]
    failed = [{"change_id": c.id,
               "symbol": (getattr(c.symbol, "full_path", None)
                          or getattr(c.symbol, "name", "?")) if c.symbol else "?",
               "reason": str(error)} for c in wanted]
    user_id = getattr(request.state, "user_id", None) or ""
    new_content, rescued, still_failed = rescue_failed_changes(
        file_path=req.file_path,
        file_content=req.file_content,
        all_changes=req.changes,
        failed_changes=failed,
        user_id=user_id,
    )
    if not rescued:
        raise HTTPException(status_code=409, detail=str(error))
    return SurgicalApplyResponse(
        file_path=req.file_path,
        new_content=new_content,
        applied_count=len(rescued),
        backup_path=None,
        cloud_mode=True,
        modified_content=new_content,
        failed_count=len(still_failed),
        failed_changes=still_failed,
        rescued_count=len(rescued),
        rescued_changes=rescued,
    )


def _any_ai_key_configured() -> bool:
    """Return True if at least one AI provider API key is set."""
    return bool(
        get_setting("openai_api_key")
        or get_setting("anthropic_api_key")
        or get_setting("gemini_api_key")
    )


@router.post("/analyze", response_model=SurgicalAnalyzeResponse)
def analyze(req: SurgicalAnalyzeRequest):
    """Run the Architect + Surgeon pipeline on a file."""
    if not _any_ai_key_configured():
        raise HTTPException(status_code=401, detail="No AI API key configured. Go to Settings to add your OpenAI, Anthropic, or Gemini key.")

    try:
        result = analyze_and_plan(
            file_path=req.file_path,
            file_content=req.file_content,
            user_request=req.request,
            session_id=req.session_id
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {str(e)}")


@router.post("/analyze-stream")
async def analyze_stream(req: SurgicalAnalyzeRequest):
    """Streaming surgical analysis with progress updates."""
    if not _any_ai_key_configured():
        raise HTTPException(status_code=401, detail="No AI API key configured. Go to Settings to add your OpenAI, Anthropic, or Gemini key.")

    workspace = get_setting("workspace_path", "")
    with get_db_ctx() as conn:
        memory_row = conn.execute(
            "SELECT content FROM project_memory WHERE workspace_path = ? LIMIT 1", (workspace,)
        ).fetchone() if workspace else None
        project_memory = memory_row["content"] if memory_row else None

    async def generate():
        async for chunk in analyze_and_plan_stream(
            req.file_path, req.file_content, req.request, req.session_id, project_memory
        ):
            yield chunk

    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@router.post("/apply", response_model=SurgicalApplyResponse)
def apply(req: SurgicalApplyRequest, request: Request):
    """Apply approved changes to a file."""
    try:
        try:
            result = apply_changes_to_file(
                file_path=req.file_path,
                changes=req.changes,
                change_ids=[req.change_id] if req.change_id else None,
                file_content=req.file_content
            )
            result = _attempt_rescue(req, request, result)
        except ValueError as ve:
            result = _rescue_after_total_failure(
                req, request, ve,
                change_ids=[req.change_id] if req.change_id else None)

        # Record in change history
        with get_db_ctx() as conn:
            for change in req.changes:
                if change.id == req.change_id:
                    conn.execute(
                        """INSERT OR REPLACE INTO change_history
                           (id, file_path, symbol_path, original_code, new_code, applied)
                           VALUES (?, ?, ?, ?, ?, 1)""",
                        (change.id, req.file_path, change.symbol.full_path,
                         change.original_code, change.new_code)
                    )
            conn.commit()

        return result
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply-all", response_model=SurgicalApplyResponse)
def apply_all(req: SurgicalApplyRequest, request: Request):
    """Apply all changes in a batch, with an AI rescue tier for failures."""
    try:
        try:
            result = apply_changes_to_file(
                file_path=req.file_path,
                changes=req.changes,
                change_ids=None,  # Apply all
                file_content=req.file_content
            )
            # Partial failures → AI rescue on the post-apply content
            result = _attempt_rescue(req, request, result)
        except ValueError as ve:
            # Total failure → AI rescue from the original content
            result = _rescue_after_total_failure(req, request, ve)
        return result
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
def get_history(file_path: str = None, limit: int = 50):
    """Get surgical change history."""
    with get_db_ctx() as conn:
        if file_path:
            rows = conn.execute(
                "SELECT * FROM change_history WHERE file_path = ? ORDER BY created_at DESC LIMIT ?",
                (file_path, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM change_history ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


@router.get("/qa-log")
def get_qa_log(session_id: str = None, limit: int = 100):
    """Admin: view QA log entries. Proof that QA ran on every edit."""
    with get_db_ctx() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM qa_log WHERE session_id = ? ORDER BY ran_at DESC LIMIT ?",
                (session_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM qa_log ORDER BY ran_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


@router.get("/compliance-log")
def get_compliance_log(session_id: str = None, limit: int = 50):
    """Admin: view pipeline compliance records. Proves all required steps ran."""
    with get_db_ctx() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM compliance_log WHERE session_id = ? ORDER BY ran_at DESC LIMIT ?",
                (session_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM compliance_log ORDER BY ran_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


@router.post("/applied/{session_id}/{change_id}")
def mark_change_applied(session_id: str, change_id: str):
    """Persist that a user applied a change — survives page refresh."""
    with get_db_ctx() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS applied_changes
               (session_id TEXT NOT NULL, change_id TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (session_id, change_id))"""
        )
        conn.execute(
            "INSERT OR IGNORE INTO applied_changes (session_id, change_id) VALUES (?, ?)",
            (session_id, change_id)
        )
        conn.commit()
    return {"ok": True}


@router.delete("/applied/{session_id}/{change_id}")
def unmark_change_applied(session_id: str, change_id: str):
    """Remove applied state (undo support)."""
    with get_db_ctx() as conn:
        conn.execute(
            "DELETE FROM applied_changes WHERE session_id = ? AND change_id = ?",
            (session_id, change_id)
        )
        conn.commit()
    return {"ok": True}


@router.get("/applied/{session_id}")
def get_applied_changes(session_id: str):
    """Return all applied change IDs for a session — used on page load."""
    with get_db_ctx() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS applied_changes
               (session_id TEXT NOT NULL, change_id TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (session_id, change_id))"""
        )
        rows = conn.execute(
            "SELECT change_id FROM applied_changes WHERE session_id = ?",
            (session_id,)
        ).fetchall()
        return {"applied_ids": [r["change_id"] for r in rows]}
