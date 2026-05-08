"""Surgical analysis and apply router."""
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from models.schemas import (
    SurgicalAnalyzeRequest, SurgicalAnalyzeResponse,
    SurgicalApplyRequest, SurgicalApplyResponse,
    SurgicalChange
)
from database import get_setting, get_db
from services.pipeline import analyze_and_plan, analyze_and_plan_stream
from services.surgical_editor import apply_changes_to_file

router = APIRouter()


@router.post("/analyze", response_model=SurgicalAnalyzeResponse)
def analyze(req: SurgicalAnalyzeRequest):
    """Run the Architect + Surgeon pipeline on a file."""
    if not get_setting("openai_api_key"):
        raise HTTPException(status_code=401, detail="OpenAI API key not set.")

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
    if not get_setting("openai_api_key"):
        raise HTTPException(status_code=401, detail="API key not set.")

    workspace = get_setting("workspace_path", "")
    conn = get_db()
    memory_row = conn.execute(
        "SELECT content FROM project_memory WHERE workspace_path = ? LIMIT 1", (workspace,)
    ).fetchone() if workspace else None
    project_memory = memory_row["content"] if memory_row else None
    conn.close()

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
def apply(req: SurgicalApplyRequest):
    """Apply approved changes to a file."""
    try:
        result = apply_changes_to_file(
            file_path=req.file_path,
            changes=req.changes,
            change_ids=[req.change_id] if req.change_id else None,
            file_content=req.file_content
        )

        # Record in change history
        conn = get_db()
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
        conn.close()

        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply-all", response_model=SurgicalApplyResponse)
def apply_all(req: SurgicalApplyRequest):
    """Apply all changes in a batch."""
    try:
        result = apply_changes_to_file(
            file_path=req.file_path,
            changes=req.changes,
            change_ids=None,  # Apply all
            file_content=req.file_content
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
def get_history(file_path: str = None, limit: int = 50):
    """Get surgical change history."""
    conn = get_db()
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
    conn.close()
    return [dict(r) for r in rows]


@router.get("/qa-log")
def get_qa_log(session_id: str = None, limit: int = 100):
    """Admin: view QA log entries. Proof that QA ran on every edit."""
    conn = get_db()
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
    conn.close()
    return [dict(r) for r in rows]


@router.get("/compliance-log")
def get_compliance_log(session_id: str = None, limit: int = 50):
    """Admin: view pipeline compliance records. Proves all required steps ran."""
    conn = get_db()
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
    conn.close()
    return [dict(r) for r in rows]
