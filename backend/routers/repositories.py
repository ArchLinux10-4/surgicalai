"""Open Folder API. Local installs only.

Every route 404s when USE_POSTGRES is set, which is the hosted signal the
rest of the app already uses. A local install that points DATABASE_URL at
Postgres is treated as hosted and does not get this API.
"""
import os
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services.folder_picker import open_directory_dialog
from services.repositories import (
    list_directories,
    list_repositories,
    open_repository,
    preview_path,
    remove_repository,
    require_root,
    update_repository,
)
from services.session_auth import require_session_access_from_request

router = APIRouter()


def _guard_local_only():
    from database import USE_POSTGRES
    if USE_POSTGRES:
        raise HTTPException(status_code=404, detail="Not available on hosted deployments.")


def _user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", "") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


class OpenBody(BaseModel):
    path: str
    name: str | None = None


class PatchBody(BaseModel):
    name: str | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    unlink_github: bool = False
    auto_apply: bool | None = None
    restore: bool = False


class StageBody(BaseModel):
    session_id: str
    path: str


@router.get("")
def list_repos(request: Request, include_removed: bool = False):
    _guard_local_only()
    return list_repositories(_user_id(request), include_removed=include_removed)


@router.get("/preview")
def preview(request: Request, path: str):
    _guard_local_only()
    return preview_path(_user_id(request), path)


@router.get("/fs")
def browse_fs(request: Request, path: str = ""):
    _guard_local_only()
    _user_id(request)
    return list_directories(path)


@router.post("/browse")
def browse_native(request: Request):
    """Open the OS folder picker and return the path. Does not register it."""
    _guard_local_only()
    _user_id(request)
    result: dict = {}
    dialog = threading.Thread(
        target=open_directory_dialog,
        args=(result, "Select a repository folder"),
        daemon=True,
    )
    dialog.start()
    dialog.join(timeout=120)
    if dialog.is_alive():
        root = result.get("root")
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        raise HTTPException(status_code=504, detail="Folder picker timed out")
    if result.get("error"):
        raise HTTPException(
            status_code=500,
            detail="Could not open a folder picker on this machine. Type the path instead.",
        )
    return {"path": result.get("path") or ""}


@router.post("/open")
def open_folder(body: OpenBody, request: Request):
    _guard_local_only()
    return open_repository(_user_id(request), body.path, body.name)


@router.patch("/{repo_id}")
def patch_repo(repo_id: str, body: PatchBody, request: Request):
    _guard_local_only()
    return update_repository(_user_id(request), repo_id, body.model_dump(exclude_unset=True))


@router.delete("/{repo_id}")
def delete_repo(repo_id: str, request: Request):
    _guard_local_only()
    return remove_repository(_user_id(request), repo_id)


@router.get("/{repo_id}/status")
def repo_status(repo_id: str, request: Request):
    _guard_local_only()
    user_id = _user_id(request)
    from services.repositories import get_owned, git_status_text
    row = get_owned(user_id, repo_id, include_removed=True)
    if not row:
        raise HTTPException(status_code=404, detail="Repository not found")
    root = row.get("root_path") or ""
    missing = not root or not os.path.isdir(root)
    return {
        "id": repo_id,
        "missing": missing,
        "status": "" if missing else git_status_text(root),
    }


@router.get("/{repo_id}/tree")
def repo_tree(repo_id: str, request: Request):
    _guard_local_only()
    user_id = _user_id(request)
    root = require_root(user_id, repo_id)
    from pathlib import Path
    from routers.files import _build_tree
    return _build_tree(Path(root), root)


@router.post("/{repo_id}/stage")
def stage_file(repo_id: str, body: StageBody, request: Request):
    _guard_local_only()
    user_id = _user_id(request)
    require_session_access_from_request(body.session_id, request)
    from services.repo_apply import stage_path
    staged = stage_path(body.session_id, user_id, repo_id, body.path)
    staged.pop("content", None)
    return staged
