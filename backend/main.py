"""
SurgicalAI — Local AI coding assistant.
FastAPI app entry point.
"""
import os
import sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from database import init_db
from routers import settings, chat, files, surgical, git, context

app = FastAPI(
    title="SurgicalAI",
    description="Local AI coding assistant with surgical precision",
    version="1.1.0"
)

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(files.router, prefix="/api/files", tags=["files"])
app.include_router(surgical.router, prefix="/api/surgical", tags=["surgical"])
app.include_router(git.router, prefix="/api/git", tags=["git"])
app.include_router(context.router, prefix="/api/context", tags=["context"])


@app.on_event("startup")
async def startup():
    init_db()
    print("🚀 SurgicalAI backend running")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.1.0"}


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
