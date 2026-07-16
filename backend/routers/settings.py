"""Settings & API key management router — per-user encrypted key storage."""
import httpx
import threading
from fastapi import APIRouter, HTTPException, Request
from models.schemas import SettingsUpdate, SettingsResponse
from database import get_all_settings, set_setting, get_setting, set_user_api_key, get_user_api_key, _dlog, USE_POSTGRES
from crypto_utils import encrypt_api_key, decrypt_api_key

# Settings that control server-side infrastructure (not just per-user UI
# preferences) — changing these can affect every user on the instance, so
# they require admin ON HOSTED (Postgres/Railway) instances. workspace_path
# in particular is the trust root for the entire file browser sandbox (see
# routers/files.py:_safe_path) — a non-admin able to repoint it on a shared
# hosted instance could read/write anywhere the process can.
#
# On a local/SQLite install this restriction is unnecessary friction with no
# security benefit: the install is inherently single-user (one person, one
# machine, one folder) by the nature of how it's run, so the gate is skipped
# there. See browse_directory() below for the matching rationale.
_ADMIN_ONLY_SETTINGS = {"workspace_path"}

router = APIRouter()


def _get_user_id(request: Request) -> str:
    """Extract user_id from JWT middleware. Returns empty string if unauthenticated."""
    return getattr(request.state, "user_id", "") or ""


def _resolve_api_key(user_id: str, key_type: str) -> str:
    """Get the decrypted API key for a user. Per-user only — no global fallback."""
    if user_id:
        encrypted = get_user_api_key(user_id, key_type)
        if encrypted:
            try:
                return decrypt_api_key(encrypted)
            except Exception:
                _dlog("api_key_decrypt_failed", user_id=user_id, key_type=key_type)
    return ""


@router.get("", response_model=SettingsResponse)
def get_settings(request: Request):
    s = get_all_settings()
    user_id = _get_user_id(request)

    # Per-user keys only — no global/shared keys
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
        is_hosted=USE_POSTGRES,
    )


@router.post("")
def update_settings(req: SettingsUpdate, request: Request):
    updates = req.model_dump(exclude_none=True)
    user_id = _get_user_id(request)
    is_admin = getattr(request.state, "is_admin", False)

    # Admin-only gating only matters on hosted (multi-tenant) instances —
    # workspace_path is the trust root for the file browser sandbox and a
    # non-admin repointing it there could read/write anywhere the shared
    # process can reach. A local/SQLite install is inherently single-user
    # (one person, one machine, one folder), so there's no multi-tenant risk
    # and this restriction would just be friction with no security benefit.
    if USE_POSTGRES and not is_admin:
        for admin_key in _ADMIN_ONLY_SETTINGS.intersection(updates.keys()):
            current_val = get_setting(admin_key, "")
            if str(updates[admin_key]) != current_val:
                _dlog("settings_admin_only_rejected", user_id=user_id, key=admin_key,
                       submitted=str(updates[admin_key]), current=current_val)
                raise HTTPException(
                    status_code=403,
                    detail=f"Admin access required to change: {admin_key}"
                )
            # Value unchanged — silently drop it so we don't re-write
            del updates[admin_key]

    for key, val in updates.items():
        if key == "workspace_path":
            old_val = get_setting("workspace_path", "")
            _dlog("settings_workspace_path_changed", user_id=user_id, old_value=old_val, new_value=str(val))
        set_setting(key, str(val))

    _dlog("settings_updated", user_id=user_id, keys=list(updates.keys()))
    return {"ok": True, "updated": list(updates.keys())}


def _open_directory_dialog(result: dict) -> None:
    """Run on a background thread — tkinter's askdirectory() blocks until the
    user closes the dialog, so it must never run on the FastAPI event loop."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        # Expose the root window immediately so the caller can force-close it
        # if this thread times out (browse_directory joins with a timeout) —
        # otherwise a stray Tk window would linger on the host machine forever.
        result["root"] = root
        path = filedialog.askdirectory(title="Select SurgicalAI workspace folder")
        root.destroy()
        result["path"] = path or ""
    except Exception as e:
        result["error"] = str(e)


@router.post("/browse-directory")
def browse_directory(request: Request):
    """Open a native OS folder picker on the machine running this backend and
    return the absolute path the user selected.

    Only meaningful when SurgicalAI's backend runs locally on the user's own
    machine (the intended install target) — it requires a GUI/display session
    and Python's stdlib `tkinter`. If either is unavailable (e.g. a headless
    server), this fails clearly and the user falls back to typing the path.

    Admin-gated ONLY on hosted (Postgres) instances — workspace_path is the
    trust root for the entire file browser sandbox (see routers/files.py:_safe_path)
    there, so only admins may set it on a shared instance. A local/SQLite install
    is single-user by nature (one person, one machine, one folder), so there's
    no multi-tenant risk and any authenticated user may browse.
    """
    is_admin = getattr(request.state, "is_admin", False)
    user_id = _get_user_id(request)
    if USE_POSTGRES and not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required to browse for a workspace directory")

    result: dict = {}
    dialog_thread = threading.Thread(target=_open_directory_dialog, args=(result,), daemon=True)
    dialog_thread.start()
    dialog_thread.join(timeout=120)

    if dialog_thread.is_alive():
        # The user didn't respond in time — force-close the dialog's Tk window
        # so it doesn't linger indefinitely on the host machine. tkinter isn't
        # officially thread-safe, but destroying the root from here is the
        # only practical way to reclaim it: the dialog thread is stuck inside
        # a blocking native call and cannot check for cancellation itself.
        root = result.get("root")
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        _dlog("settings_browse_directory_timeout", user_id=user_id)
        raise HTTPException(status_code=504, detail="Directory picker timed out — please type the path manually")

    if "error" in result:
        _dlog("settings_browse_directory_failed", user_id=user_id, error=result["error"])
        raise HTTPException(
            status_code=500,
            detail="Could not open a folder picker on this machine (no display or tkinter unavailable). "
                   "Please type the path manually instead."
        )

    path = result.get("path", "")
    _dlog("settings_browse_directory_picked", user_id=user_id, path=path)
    return {"path": path}


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
         "description": "Most capable model — ⚠️ Premium pricing ($10/$50 per M tokens)", "provider": "anthropic", "cost": 4},
        {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "role": "architect",
         "description": "Complex agentic coding — powerful for multi-file work", "provider": "anthropic", "cost": 4},
        {"id": "claude-sonnet-5", "name": "Claude Sonnet 5", "role": "architect",
         "description": "Best speed + intelligence balance — recommended for most tasks", "provider": "anthropic", "cost": 2},
        {"id": "claude-opus-4-7", "name": "Claude Opus 4.7", "role": "architect",
         "description": "High-capability Opus — strong for large, multi-step changes", "provider": "anthropic", "cost": 3},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "role": "architect",
         "description": "Fast, intelligent — proven and reliable", "provider": "anthropic", "cost": 2},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "role": "architect",
         "description": "Capable Opus — great for complex, multi-file changes", "provider": "anthropic", "cost": 3},
        {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5", "role": "architect",
         "description": "Fast and capable — balanced speed and quality", "provider": "anthropic", "cost": 2},
        {"id": "claude-opus-4-5", "name": "Claude Opus 4.5", "role": "architect",
         "description": "Opus 4.5 — deep reasoning for involved edits", "provider": "anthropic", "cost": 3},
        {"id": "claude-opus-4-1", "name": "Claude Opus 4.1", "role": "architect",
         "description": "Earlier Opus — solid for complex changes", "provider": "anthropic", "cost": 3},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "role": "architect",
         "description": "Fastest Claude — great for quick edits and simple changes", "provider": "anthropic", "cost": 1},
    ]

    # OpenAI models — only shown when an OpenAI key is configured
    openai_models = []
    has_openai = bool(_resolve_api_key(user_id, "openai"))
    if has_openai:
        openai_models = [
            {"id": "gpt-5.5", "name": "GPT-5.5", "role": "architect",
             "description": "Latest GPT — powerful general-purpose model", "provider": "openai", "cost": 2},
            {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "role": "architect",
             "description": "Frontier reasoning — hardest problems, 1M context", "provider": "openai", "cost": 3},
            {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra", "role": "architect",
             "description": "Balanced reasoning — 2× cheaper than Sol, 1M context", "provider": "openai", "cost": 2},
            {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna", "role": "architect",
             "description": "Fast reasoning — 5× cheaper than Sol, 1M context", "provider": "openai", "cost": 1},
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
        _dlog("api_key_deleted", user_id=user_id, key_type="openai")
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
        # Per-user only — no global write
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
