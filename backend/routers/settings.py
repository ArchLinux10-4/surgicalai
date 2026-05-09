"""Settings & API key management router — per-user encrypted key storage."""
import httpx
from fastapi import APIRouter, HTTPException, Request
from models.schemas import SettingsUpdate, SettingsResponse
from database import get_all_settings, set_setting, get_setting, set_user_api_key, get_user_api_key
from crypto_utils import encrypt_api_key, decrypt_api_key

router = APIRouter()


def _get_user_id(request: Request) -> str:
    """Extract user_id from JWT middleware. Returns empty string if unauthenticated."""
    return getattr(request.state, "user_id", "") or ""


def _resolve_api_key(user_id: str, key_type: str) -> str:
    """Get the decrypted API key for a user. Falls back to global settings for migration."""
    if user_id:
        encrypted = get_user_api_key(user_id, key_type)
        if encrypted:
            try:
                return decrypt_api_key(encrypted)
            except Exception:
                pass  # Corrupted? Fall through to global
    # Fallback: legacy global setting
    return get_setting(f"{key_type}_api_key", "")


@router.get("", response_model=SettingsResponse)
def get_settings(request: Request):
    s = get_all_settings()
    user_id = _get_user_id(request)

    # Check per-user keys first, then global
    has_openai = bool(_resolve_api_key(user_id, "openai"))
    has_anthropic = bool(_resolve_api_key(user_id, "anthropic"))

    return SettingsResponse(
        openai_api_key_set=has_openai,
        anthropic_api_key_set=has_anthropic,
        architect_model=s.get("architect_model", "gpt-5"),
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
def get_available_models(request: Request):
    """Return the list of supported models, including Claude if Anthropic key is set."""
    user_id = _get_user_id(request)

    openai_models = [
        {"id": "gpt-4.1", "name": "GPT-4.1", "role": "surgeon", "description": "Low hallucination — best for writing code"},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "role": "fast", "description": "Fast and cheap for simple tasks"},
        {"id": "gpt-4o", "name": "GPT-4o", "role": "architect", "description": "Strong reasoning for planning"},
        {"id": "o4-mini", "name": "o4-mini", "role": "architect", "description": "Reasoning model — good for architecture"},
        {"id": "gpt-5", "name": "GPT-5", "role": "architect", "description": "Most capable — best for complex architecture (no temperature control)", "no_temperature": True},
    ]

    claude_models = []
    if _resolve_api_key(user_id, "anthropic"):
        claude_models = [
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "role": "architect", "description": "Fast, intelligent — great Architect with visible thinking", "provider": "anthropic"},
            {"id": "claude-opus-4-7", "name": "Claude Opus 4.7", "role": "architect", "description": "Most capable Claude — deep reasoning with extended thinking", "provider": "anthropic"},
        ]

    gemini_models = []
    if _resolve_api_key(user_id, "gemini"):
        gemini_models = [
            {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "role": "architect", "description": "1M context window — best for huge files, with visible thinking", "provider": "gemini"},
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "role": "architect", "description": "Fast + affordable — great for large files with thinking", "provider": "gemini"},
            {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash", "role": "surgeon", "description": "Fastest Gemini — great for quick edits", "provider": "gemini"},
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
        "models": openai_models + claude_models + gemini_models + ollama_models,
        "pipeline_modes": [
            {"id": "auto", "name": "Auto Pipeline", "description": "Architect plans, Surgeon executes (recommended)"},
            {"id": "single", "name": "Single Model", "description": "Use one model for everything"},
        ]
    }


@router.delete("/api-key")
def clear_api_key(request: Request):
    user_id = _get_user_id(request)
    if user_id:
        set_user_api_key(user_id, "openai", "")
    set_setting("openai_api_key", "")
    return {"ok": True}


@router.post("/verify-key")
def verify_key(body: dict, request: Request):
    """Test OpenAI key, then encrypt + store per-user."""
    from openai import OpenAI, AuthenticationError
    key = body.get("key", "")
    if not key:
        raise HTTPException(status_code=400, detail="No key provided")
    try:
        client = OpenAI(api_key=key)
        client.models.list()
        # Store encrypted per-user
        user_id = _get_user_id(request)
        if user_id:
            encrypted = encrypt_api_key(key)
            set_user_api_key(user_id, "openai", encrypted)
        # Also store globally for pipeline access (legacy compat)
        set_setting("openai_api_key", key)
        return {"ok": True, "message": "API key verified, encrypted, and saved"}
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid API key")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/verify-anthropic-key")
def verify_anthropic_key(body: dict, request: Request):
    """Test Anthropic key, then encrypt + store per-user."""
    key = body.get("key", "")
    if not key:
        raise HTTPException(status_code=400, detail="No key provided")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16,
            messages=[{"role": "user", "content": "Hi"}],
        )
        # Store encrypted per-user
        user_id = _get_user_id(request)
        if user_id:
            encrypted = encrypt_api_key(key)
            set_user_api_key(user_id, "anthropic", encrypted)
        # Also store globally for pipeline access
        set_setting("anthropic_api_key", key)
        return {"ok": True, "message": "Anthropic key verified, encrypted, and saved"}
    except Exception as e:
        err_msg = str(e)
        if "authentication" in err_msg.lower() or "api key" in err_msg.lower() or "401" in err_msg:
            raise HTTPException(status_code=401, detail="Invalid Anthropic API key")
        raise HTTPException(status_code=500, detail=f"Verification failed: {err_msg}")



@router.get("/gemini-status")
def gemini_status(request: Request):
    """Check if user has Gemini API key configured."""
    user_id = get_current_user_id(request)
    from crypto_utils import _resolve_api_key
    key = _resolve_api_key(user_id, "gemini")
    return {"connected": bool(key)}

@router.post("/verify-gemini-key")
def verify_gemini_key(body: dict, request: Request):
    """Test Google Gemini API key, encrypt + store per-user."""
    key = body.get("key", "")
    if not key:
        raise HTTPException(status_code=400, detail="No key provided")
    try:
        import httpx
        # Quick test call using OpenAI-compat endpoint
        from openai import OpenAI
        gclient = OpenAI(
            api_key=key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        gclient.models.list()
        user_id = _get_user_id(request)
        if user_id:
            encrypted = encrypt_api_key(key)
            set_user_api_key(user_id, "gemini", encrypted)
        set_setting("gemini_api_key", key)
        return {"ok": True, "message": "Gemini API key verified and saved"}
    except Exception as e:
        err = str(e).lower()
        if "401" in str(e) or "invalid" in err or "api_key" in err:
            raise HTTPException(status_code=401, detail="Invalid Gemini API key")
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)[:200]}")


@router.get("/gemini-status")
def gemini_status(request: Request):
    """Check if Gemini API key is configured for this user."""
    user_id = _get_user_id(request)
    key = _resolve_api_key(user_id, "gemini")
    return {"configured": bool(key)}


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
