"""Settings & API key management router."""
import httpx
from fastapi import APIRouter, HTTPException
from models.schemas import SettingsUpdate, SettingsResponse
from database import get_all_settings, set_setting, get_setting

router = APIRouter()


@router.get("", response_model=SettingsResponse)
def get_settings():
    s = get_all_settings()
    return SettingsResponse(
        openai_api_key_set=bool(s.get("openai_api_key", "")),
        architect_model=s.get("architect_model", "gpt-4.1"),
        surgeon_model=s.get("surgeon_model", "gpt-4.1"),
        temperature_architect=float(s.get("temperature_architect", "0.3")),
        temperature_surgeon=float(s.get("temperature_surgeon", "0.1")),
        confidence_threshold=int(s.get("confidence_threshold", "7")),
        auto_backup=s.get("auto_backup", "true").lower() == "true",
        theme=s.get("theme", "dark"),
        font_size=int(s.get("font_size", "14")),
        workspace_path=s.get("workspace_path", ""),
        ollama_enabled=s.get("ollama_enabled", "false").lower() == "true",
        ollama_base_url=s.get("ollama_base_url", "http://localhost:11434"),
        ollama_model=s.get("ollama_model", "qwen2.5-coder:7b"),
    )


@router.post("")
def update_settings(req: SettingsUpdate):
    updates = req.model_dump(exclude_none=True)
    for key, val in updates.items():
        set_setting(key, str(val))
    return {"ok": True, "updated": list(updates.keys())}


@router.get("/models")
def get_available_models():
    """Return the list of supported models, including Ollama if enabled."""
    openai_models = [
        {"id": "gpt-4.1", "name": "GPT-4.1", "role": "surgeon", "description": "Low hallucination — best for writing code"},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "role": "fast", "description": "Fast and cheap for simple tasks"},
        {"id": "gpt-4o", "name": "GPT-4o", "role": "architect", "description": "Strong reasoning for planning"},
        {"id": "o4-mini", "name": "o4-mini", "role": "architect", "description": "Reasoning model — good for architecture"},
        {"id": "gpt-5", "name": "GPT-5", "role": "architect", "description": "Most capable — best for complex architecture (no temperature control)", "no_temperature": True},
    ]

    ollama_models = []
    if get_setting("ollama_enabled", "false") == "true":
        try:
            base_url = get_setting("ollama_base_url", "http://localhost:11434")
            resp = httpx.get(f"{base_url}/api/tags", timeout=3)
            for m in resp.json().get("models", []):
                ollama_models.append({
                    "id": f"ollama:{m['name']}",
                    "name": f"🦙 {m['name']}",
                    "description": "Local Ollama model"
                })
        except Exception:
            pass

    return {
        "models": openai_models + ollama_models,
        "pipeline_modes": [
            {"id": "auto", "name": "Auto Pipeline", "description": "Architect plans, Surgeon executes (recommended)"},
            {"id": "single", "name": "Single Model", "description": "Use one model for everything"},
        ]
    }


@router.delete("/api-key")
def clear_api_key():
    set_setting("openai_api_key", "")
    return {"ok": True}


@router.post("/verify-key")
def verify_key(body: dict):
    """Test if the API key works."""
    from openai import OpenAI, AuthenticationError
    key = body.get("key", "")
    if not key:
        raise HTTPException(status_code=400, detail="No key provided")
    try:
        client = OpenAI(api_key=key)
        client.models.list()
        set_setting("openai_api_key", key)
        return {"ok": True, "message": "API key verified and saved"}
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/test-ollama")
def test_ollama(body: dict = None):
    """Test Ollama connectivity."""
    base_url = (body or {}).get("base_url") or get_setting("ollama_base_url", "http://localhost:11434")
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        return {"ok": True, "models": models, "message": f"Connected to Ollama at {base_url}"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Cannot connect to Ollama: {str(e)}")
