"""Settings & API key management router — per-user encrypted key storage."""
import httpx
from fastapi import APIRouter, HTTPException, Request
from models.schemas import SettingsUpdate, SettingsResponse
from database import get_all_settings, set_setting, get_setting, set_user_api_key, get_user_api_key, _dlog
from crypto_utils import encrypt_api_key, decrypt_api_key

# Settings that control server-side infrastructure (not just per-user UI
# preferences) — changing these can affect every user on the instance, so
# they require admin. workspace_path in particular is the trust root for
# the entire file browser sandbox (see routers/files.py:_safe_path) — a
# non-admin able to repoint it could read/write anywhere the process can.
_ADMIN_ONLY_SETTINGS = {"workspace_path"}

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
        architect_model=s.get("architect_model", "claude-sonnet-5"),
        surgeon_model=s.get("surgeon_model", "claude-sonnet-5"),
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
def update_settings(req: SettingsUpdate, request: Request):
    updates = req.model_dump(exclude_none=True)
    user_id = _get_user_id(request)
    is_admin = getattr(request.state, "is_admin", False)

    attempted_admin_only = _ADMIN_ONLY_SETTINGS.intersection(updates.keys())
    if attempted_admin_only and not is_admin:
        _dlog("settings_admin_only_rejected", user_id=user_id, keys=list(attempted_admin_only))
        raise HTTPException(
            status_code=403,
            detail=f"Admin access required to change: {', '.join(sorted(attempted_admin_only))}"
        )

    for key, val in updates.items():
        if key == "workspace_path":
            old_val = get_setting("workspace_path", "")
            _dlog("settings_workspace_path_changed", user_id=user_id, old_value=old_val, new_value=str(val))
        set_setting(key, str(val))

    _dlog("settings_updated", user_id=user_id, keys=list(updates.keys()))
    return {"ok": True, "updated": list(updates.keys())}


@router.get("/models")
def get_available_models(request: Request):
    """Return only Claude models — SurgicalAI is optimised exclusively for Claude API."""
    user_id = _get_user_id(request)

    # SurgicalAI runs exclusively on Claude. All other model families are hidden.
    # GPT, Gemini, and Ollama models are preserved in comments for future re-enabling.

    # Verified against the live Anthropic key via /v1 probe — only IDs the
    # account actually accepts are listed (others 404 at request time).
    claude_models = [
        {"id": "claude-fable-5", "name": "Claude Fable 5", "role": "architect",
         "description": "Most capable model — ⚠️ Premium pricing ($10/$50 per M tokens)", "provider": "anthropic"},
        {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "role": "architect",
         "description": "Complex agentic coding — powerful for multi-file work", "provider": "anthropic"},
        {"id": "claude-sonnet-5", "name": "Claude Sonnet 5", "role": "architect",
         "description": "Best speed + intelligence balance — recommended for most tasks", "provider": "anthropic"},
        {"id": "claude-opus-4-7", "name": "Claude Opus 4.7", "role": "architect",
         "description": "High-capability Opus — strong for large, multi-step changes", "provider": "anthropic"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "role": "architect",
         "description": "Fast, intelligent — proven and reliable", "provider": "anthropic"},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "role": "architect",
         "description": "Capable Opus — great for complex, multi-file changes", "provider": "anthropic"},
        {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5", "role": "architect",
         "description": "Fast and capable — balanced speed and quality", "provider": "anthropic"},
        {"id": "claude-opus-4-5", "name": "Claude Opus 4.5", "role": "architect",
         "description": "Opus 4.5 — deep reasoning for involved edits", "provider": "anthropic"},
        {"id": "claude-opus-4-1", "name": "Claude Opus 4.1", "role": "architect",
         "description": "Earlier Opus — solid for complex changes", "provider": "anthropic"},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "role": "architect",
         "description": "Fastest Claude — great for quick edits and simple changes", "provider": "anthropic"},
    ]

    # OpenAI models — only shown when an OpenAI key is configured
    openai_models = []
    has_openai = bool(_resolve_api_key(user_id, "openai"))
    if has_openai:
        openai_models = [
            {"id": "gpt-5.5", "name": "GPT-5.5", "role": "architect",
             "description": "Latest GPT — powerful general-purpose model", "provider": "openai"},
            {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "role": "architect",
             "description": "Frontier reasoning — hardest problems, 1M context", "provider": "openai"},
            {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra", "role": "architect",
             "description": "Balanced reasoning — 2× cheaper than Sol, 1M context", "provider": "openai"},
            {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna", "role": "architect",
             "description": "Fast reasoning — 5× cheaper than Sol, 1M context", "provider": "openai"},
        ]

    return {
        "models": claude_models + openai_models,
        "pipeline_modes": [
            {"id": "auto", "name": "Auto", "description": "SurgicalAI natural pipeline (recommended)"},
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



# gemini-status endpoint defined below after verify-gemini-key

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
