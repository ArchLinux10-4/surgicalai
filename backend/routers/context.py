"""
Context pinning, project memory, prompt templates, impact analysis, multi-file surgical.
"""
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from models.schemas import (
    PinRequest, PinnedContext, ProjectMemory,
    PromptTemplate, PromptTemplateCreate,
    MultiFileAnalyzeRequest, ImpactAnalysisResponse
)
from database import get_db, get_setting, GLOBAL_MEMORY_KEY
from services.pipeline import run_impact_analysis, analyze_multi_file
from data.memory_presets import MEMORY_PRESETS

router = APIRouter()

# ─── Context Pinning ──────────────────────────────────────────────────────────

@router.get("/pins")
def get_pins(session_id: str = None, workspace_path: str = None):
    key = session_id or workspace_path
    if not key:
        raise HTTPException(status_code=422, detail="session_id or workspace_path required")
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM pinned_context WHERE workspace_path = ? ORDER BY created_at DESC",
        (key,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@router.post("/pins")
def add_pin(req: PinRequest):
    # Read file content for the pin
    try:
        with open(req.file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Cannot read file: {e}")

    key = req.session_id or req.workspace_path or ""
    pin_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO pinned_context (id, workspace_path, file_path, symbol_path, label) VALUES (?, ?, ?, ?, ?)",
        (pin_id, key, req.file_path, req.symbol_path, req.label or req.file_path.split('/')[-1])
    )
    conn.commit()
    conn.close()
    return {"id": pin_id, "ok": True}

@router.delete("/pins/{pin_id}")
def remove_pin(pin_id: str):
    conn = get_db()
    conn.execute("DELETE FROM pinned_context WHERE id = ?", (pin_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ─── Project Memory ───────────────────────────────────────────────────────────

@router.get("/memory")
def get_memory(session_id: str = None, workspace_path: str = None):
    key = session_id or workspace_path
    if not key:
        raise HTTPException(status_code=422, detail="session_id or workspace_path required")
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM project_memory WHERE workspace_path = ? ORDER BY updated_at DESC LIMIT 1",
        (key,)
    ).fetchone()
    conn.close()
    if not row:
        return {"workspace_path": key, "content": "", "id": None}
    return dict(row)

@router.post("/memory")
def save_memory(req: ProjectMemory):
    key = req.session_id or req.workspace_path or ""
    conn = get_db()
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
    conn.close()
    return {"ok": True}

# ─── Global Project Memory (team-wide, injected into every prompt) ────────────

@router.get("/memory/global")
def get_global_memory():
    """Team-wide conventions injected into every prompt, every session, every user."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM project_memory WHERE workspace_path = ? ORDER BY updated_at DESC LIMIT 1",
        (GLOBAL_MEMORY_KEY,)
    ).fetchone()
    conn.close()
    if not row:
        return {"workspace_path": GLOBAL_MEMORY_KEY, "content": "", "id": None}
    return dict(row)

@router.post("/memory/global")
def save_global_memory(req: ProjectMemory):
    conn = get_db()
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
    conn.close()
    return {"ok": True}

@router.get("/memory/presets")
def get_memory_presets():
    """Curated, shared library of convention presets every developer can pick from."""
    return MEMORY_PRESETS

# ─── Prompt Templates ─────────────────────────────────────────────────────────

@router.get("/templates")
def get_templates():
    conn = get_db()
    rows = conn.execute("SELECT * FROM prompt_templates ORDER BY name ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@router.post("/templates")
def create_template(req: PromptTemplateCreate):
    template_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO prompt_templates (id, name, prompt, mode) VALUES (?, ?, ?, ?)",
        (template_id, req.name, req.prompt, req.mode)
    )
    conn.commit()
    conn.close()
    return {"id": template_id, "ok": True}

@router.delete("/templates/{template_id}")
def delete_template(template_id: str):
    conn = get_db()
    conn.execute("DELETE FROM prompt_templates WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()
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
        raise HTTPException(status_code=500, detail=str(e))
