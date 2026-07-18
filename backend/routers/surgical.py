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
from services.pipeline import analyze_and_plan, analyze_and_plan_stream, _dlog
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
    # Same idempotency check as the partial-failure path in
    # surgical_editor.apply_changes_to_file: if this change's new_code is
    # already present in the content we were given, it was almost certainly
    # already applied by an earlier request (e.g. via a different diff card,
    # or a stale change.id from a re-emitted message) — not a real failure
    # that needs an AI rescue attempt. Split these out up front so we never
    # burn a rescue call on something that's already done, and so the caller
    # gets a structured response (not a raised error) whenever every change
    # in this batch turns out to already be applied.
    _content_for_check = req.file_content or ""
    _to_rescue: list = []
    _already_applied: list = []
    for c in wanted:
        _sym = (getattr(c.symbol, "full_path", None)
                or getattr(c.symbol, "name", "?")) if c.symbol else "?"
        _is_already = bool((c.new_code or "").strip()) and (c.new_code or "") in _content_for_check
        _entry = {"change_id": c.id, "symbol": _sym, "reason": str(error),
                  "already_applied": _is_already}
        (_already_applied if _is_already else _to_rescue).append(_entry)

    if not _to_rescue:
        # Everything in this batch was already applied elsewhere — this is
        # not a failure at all, just a stale/duplicate apply request.
        return SurgicalApplyResponse(
            file_path=req.file_path,
            new_content=_content_for_check,
            applied_count=0,
            backup_path=None,
            cloud_mode=True,
            modified_content=_content_for_check,
            failed_count=len(_already_applied),
            failed_changes=_already_applied,
        )

    user_id = getattr(request.state, "user_id", None) or ""
    new_content, rescued, still_failed = rescue_failed_changes(
        file_path=req.file_path,
        file_content=req.file_content,
        all_changes=req.changes,
        failed_changes=_to_rescue,
        user_id=user_id,
    )
    if not rescued:
        if _already_applied:
            # Some changes in this batch really did fail and couldn't be
            # rescued, but others were already applied — return a structured
            # response so the frontend can still auto-clear the already-done
            # ones instead of leaving the whole batch stuck on a hard error.
            return SurgicalApplyResponse(
                file_path=req.file_path,
                new_content=_content_for_check,
                applied_count=0,
                backup_path=None,
                cloud_mode=True,
                modified_content=_content_for_check,
                failed_count=len(_to_rescue) + len(_already_applied),
                failed_changes=_to_rescue + _already_applied,
            )
        raise HTTPException(status_code=409, detail=str(error))
    return SurgicalApplyResponse(
        file_path=req.file_path,
        new_content=new_content,
        applied_count=len(rescued),
        backup_path=None,
        cloud_mode=True,
        modified_content=new_content,
        failed_count=len(still_failed) + len(_already_applied),
        failed_changes=still_failed + _already_applied,
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
    # The Apply button click was structurally invisible in the debug trace
    # before this _dlog call — see correction-round filter investigation.
    # req.session_id is optional (older frontend builds / callers may omit
    # it), so this degrades gracefully rather than erroring.
    _dlog("change_apply_requested", endpoint="apply",
          session_id=req.session_id, file_path=req.file_path,
          change_id=req.change_id, change_count=len(req.changes))
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

        _dlog("change_apply_result", endpoint="apply",
              session_id=req.session_id, file_path=req.file_path,
              applied_count=result.applied_count,
              failed_count=result.failed_count,
              rescued_count=result.rescued_count,
              already_applied_count=sum(
                  1 for f in (result.failed_changes or []) if f.get("already_applied")))
        return result
    except HTTPException:
        raise
    except FileNotFoundError as e:
        _dlog("change_apply_result", endpoint="apply", session_id=req.session_id,
              file_path=req.file_path, error="file_not_found", detail=str(e))
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        _dlog("change_apply_result", endpoint="apply", session_id=req.session_id,
              file_path=req.file_path, error="value_error", detail=str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        _dlog("change_apply_result", endpoint="apply", session_id=req.session_id,
              file_path=req.file_path, error="unhandled", detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply-all", response_model=SurgicalApplyResponse)
def apply_all(req: SurgicalApplyRequest, request: Request):
    """Apply all changes in a batch, with an AI rescue tier for failures."""
    _dlog("change_apply_requested", endpoint="apply-all",
          session_id=req.session_id, file_path=req.file_path,
          change_count=len(req.changes))
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
        _dlog("change_apply_result", endpoint="apply-all",
              session_id=req.session_id, file_path=req.file_path,
              applied_count=result.applied_count,
              failed_count=result.failed_count,
              rescued_count=result.rescued_count,
              already_applied_count=sum(
                  1 for f in (result.failed_changes or []) if f.get("already_applied")))
        return result
    except HTTPException:
        raise
    except FileNotFoundError as e:
        _dlog("change_apply_result", endpoint="apply-all", session_id=req.session_id,
              file_path=req.file_path, error="file_not_found", detail=str(e))
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        _dlog("change_apply_result", endpoint="apply-all", session_id=req.session_id,
              file_path=req.file_path, error="unhandled", detail=str(e))
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
