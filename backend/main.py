"""
SurgicalAI — Local AI coding assistant.
FastAPI app entry point.
"""
import os
import sys
from pathlib import Path
import re as _re
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from database import init_db
from auth_utils import decode_token
from routers import settings, chat, files, surgical, git, context, session_files, github as github_router
from routers import auth as auth_router

app = FastAPI(
    title="SurgicalAI",
    description="Local AI coding assistant with surgical precision",
    version="3.4.0"
)

# CORS — allow local dev + any cloud origins set via ALLOWED_ORIGINS env var
_cors_extra = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
_cors_origins = ["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"] + _cors_extra
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── JWT Auth Middleware ───────────────────────────────────────────────────────
# Paths that do NOT require a token
_OPEN_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/setup",
    "/api/auth/setup-required",
}

def _get_cors_headers(request: Request) -> dict:
    """
    Auth middleware runs OUTSIDE CORSMiddleware (added later = outermost).
    So 401 responses need CORS headers added manually, otherwise the browser
    sees a network error instead of a clean 401.
    """
    origin = request.headers.get("origin", "")
    if not origin:
        return {}
    extra = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    allowed_list = ["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"] + extra
    if origin in allowed_list or _re.match(r"https://.*\.vercel\.app", origin):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }
    return {}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Always pass: CORS preflight, static frontend, open API paths
    if request.method == "OPTIONS":
        return await call_next(request)
    if not path.startswith("/api/"):
        return await call_next(request)
    if path in _OPEN_PATHS:
        return await call_next(request)

    # Extract Bearer token
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()

    if not token:
        return JSONResponse(
            status_code=401,
            content={"detail": "Not authenticated"},
            headers=_get_cors_headers(request),
        )

    try:
        payload = decode_token(token)
        request.state.user_id = payload["sub"]
        request.state.username = payload.get("username", "")
        request.state.is_admin = bool(payload.get("is_admin", False))
    except Exception:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or expired token"},
            headers=_get_cors_headers(request),
        )

    return await call_next(request)


# ─── Routers ──────────────────────────────────────────────────────────────────
app.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(files.router, prefix="/api/files", tags=["files"])
app.include_router(surgical.router, prefix="/api/surgical", tags=["surgical"])
app.include_router(git.router, prefix="/api/git", tags=["git"])
app.include_router(context.router, prefix="/api/context", tags=["context"])
app.include_router(session_files.router, prefix="/api/chat", tags=["session-files"])
app.include_router(github_router.router, prefix="/api/github", tags=["github"])


@app.on_event("startup")
async def startup():
    init_db()
    print("🚀 SurgicalAI backend running")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "3.7.1"}


# Serve React frontend (built files)
FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        index = FRONTEND_DIST / "index.html"
        return FileResponse(str(index))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
