"""
Project memory, prompt templates, impact analysis, multi-file surgical.
"""
import uuid
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from models.schemas import (
    ProjectMemory,
    PromptTemplate, PromptTemplateCreate,
    MultiFileAnalyzeRequest, ImpactAnalysisResponse
)
from database import get_db_ctx, get_setting, GLOBAL_MEMORY_KEY, _dlog
from services.pipeline import run_impact_analysis, analyze_multi_file
from data.memory_presets import MEMORY_PRESETS

router = APIRouter()

# ─── Project Memory ───────────────────────────────────────────────────────────

@router.get("/memory")
def get_memory(session_id: str = None, workspace_path: str = None):
    key = session_id or workspace_path
    if not key:
        raise HTTPException(status_code=422, detail="session_id or workspace_path required")
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM project_memory WHERE workspace_path = ? ORDER BY updated_at DESC LIMIT 1",
            (key,)
        ).fetchone()
    if not row:
        return {"workspace_path": key, "content": "", "id": None}
    return dict(row)

@router.post("/memory")
def save_memory(req: ProjectMemory):
    key = req.session_id or req.workspace_path or ""
    with get_db_ctx() as conn:
        existing = conn.execute(
            "SELECT id FROM project_memory WHERE workspace_path = ?",
            (key,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE project_memory SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE workspace_path = ?",
                (req.content, key)
            )
        else:
            conn.execute(
                "INSERT INTO project_memory (id, workspace_path, content) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), key, req.content)
            )
        conn.commit()
    return {"ok": True}

# ─── Per-user memory (private; not the team global note) ──────────────────────

def _memory_user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", "") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


@router.get("/memory/user")
def get_user_memory(request: Request):
    user_id = _memory_user_id(request)
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT content, enabled FROM user_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"content": "", "enabled": True}
    return {"content": row["content"] or "", "enabled": bool(row["enabled"])}


@router.delete("/memory/user")
def clear_user_memory(request: Request):
    user_id = _memory_user_id(request)
    with get_db_ctx() as conn:
        conn.execute(
            "UPDATE user_memory SET content = '', updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
    return {"ok": True}


@router.post("/memory/user/enabled")
async def set_user_memory_enabled(request: Request):
    user_id = _memory_user_id(request)
    payload = await request.json()
    enabled = 1 if bool(payload.get("enabled")) else 0
    with get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO user_memory (user_id, content, enabled, updated_at) "
            "VALUES (?, '', ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled, "
            "updated_at = CURRENT_TIMESTAMP",
            (user_id, enabled),
        )
        conn.commit()
    return {"ok": True, "enabled": bool(enabled)}


# ─── Global Project Memory (team-wide, injected into every prompt) ────────────

@router.get("/memory/global")
def get_global_memory():
    """Team-wide conventions injected into every prompt, every session, every user."""
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM project_memory WHERE workspace_path = ? ORDER BY updated_at DESC LIMIT 1",
            (GLOBAL_MEMORY_KEY,)
        ).fetchone()
    if not row:
        return {"workspace_path": GLOBAL_MEMORY_KEY, "content": "", "id": None}
    return dict(row)

@router.post("/memory/global")
def save_global_memory(req: ProjectMemory):
    with get_db_ctx() as conn:
        existing = conn.execute(
            "SELECT id FROM project_memory WHERE workspace_path = ?",
            (GLOBAL_MEMORY_KEY,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE project_memory SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE workspace_path = ?",
                (req.content, GLOBAL_MEMORY_KEY)
            )
        else:
            conn.execute(
                "INSERT INTO project_memory (id, workspace_path, content) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), GLOBAL_MEMORY_KEY, req.content)
            )
        conn.commit()
    return {"ok": True}

@router.get("/memory/presets")
def get_memory_presets():
    """Curated, shared library of convention presets every developer can pick from."""
    return MEMORY_PRESETS

# ─── Prompt Templates ─────────────────────────────────────────────────────────

@router.get("/templates")
def get_templates():
    with get_db_ctx() as conn:
        rows = conn.execute("SELECT * FROM prompt_templates ORDER BY name ASC").fetchall()
    return [dict(r) for r in rows]

@router.post("/templates")
def create_template(req: PromptTemplateCreate):
    template_id = str(uuid.uuid4())
    with get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO prompt_templates (id, name, prompt, mode) VALUES (?, ?, ?, ?)",
            (template_id, req.name, req.prompt, req.mode)
        )
        conn.commit()
    return {"id": template_id, "ok": True}

@router.delete("/templates/{template_id}")
def delete_template(template_id: str):
    with get_db_ctx() as conn:
        conn.execute("DELETE FROM prompt_templates WHERE id = ?", (template_id,))
        conn.commit()
    return {"ok": True}

# ─── Impact Analysis ──────────────────────────────────────────────────────────

@router.get("/impact")
def get_impact(symbol_path: str, file_path: str, workspace_path: str = None):
    if not get_setting("openai_api_key") and get_setting("ollama_enabled") != "true":
        raise HTTPException(status_code=401, detail="API key not set.")
    try:
        result = run_impact_analysis(symbol_path, file_path, "", workspace_path)
        return result
    except Exception as e:
        _dlog("context_impact_analysis_failed", symbol_path=symbol_path, file_path=file_path, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

# ─── Multi-file Surgical ──────────────────────────────────────────────────────

@router.post("/multi-analyze")
def multi_analyze(req: MultiFileAnalyzeRequest):
    if not get_setting("openai_api_key"):
        raise HTTPException(status_code=401, detail="API key not set.")
    try:
        result_by_file, summary = analyze_multi_file(
            req.file_paths, req.file_contents, req.request, req.session_id
        )
        return {
            "session_id": req.session_id or str(uuid.uuid4()),
            "files_analyzed": len(result_by_file),
            "changes_by_file": {fp: r.model_dump() for fp, r in result_by_file.items()},
            "overall_summary": summary
        }
    except Exception as e:
        _dlog("context_multi_analyze_failed", session_id=req.session_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
