"""Open Folder API. Local installs only.

Every route 404s when USE_POSTGRES is set, which is the hosted signal the
rest of the app already uses. A local install that points DATABASE_URL at
Postgres is treated as hosted and does not get this API.
"""
import logging
import os
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services.folder_picker import NO_NATIVE_PICKER_MESSAGE, native_picker_available, open_directory_dialog
from services.repositories import (
    list_directories,
    list_repositories,
    open_repository,
    preview_path,
    remove_repository,
    require_root,
    update_repository,
    validate_directory,
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


class BrowseBody(BaseModel):
    path: str = ""


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


def _checked_picker_path(path: str) -> str:
    """realpath of a chosen folder. Cancel stays ''. A path this process cannot
    see (Docker without a mount) is a 400 the UI can show."""
    if not path:
        return ""
    try:
        return validate_directory(path)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{path} isn't visible to SurgicalAI. "
                    "A Docker install only sees folders under the mounted home directory."
                ),
            ) from exc
        raise


@router.post("/browse")
def browse_native(body: BrowseBody, request: Request):
    """Open the OS folder picker and return the path. Does not register it."""
    _guard_local_only()
    _user_id(request)
    if not native_picker_available():
        raise HTTPException(status_code=501, detail=NO_NATIVE_PICKER_MESSAGE)
    default_path = body.path.strip()
    if not default_path or not os.path.isdir(default_path):
        default_path = ""
    else:
        default_path = os.path.realpath(default_path)
    result: dict = {}
    dialog = threading.Thread(
        target=open_directory_dialog,
        args=(result, "Select a repository folder", default_path),
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
        logging.getLogger(__name__).warning("folder picker failed: %s", result["error"])
        raise HTTPException(
            status_code=500,
            detail=f"Could not open a folder picker on this machine ({result['error'][:200]}). Type the path instead.",
        )
    return {"path": _checked_picker_path(result.get("path") or "")}


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
    from services.repositories import detect_git, get_owned, git_status_text
    row = get_owned(user_id, repo_id, include_removed=True)
    if not row:
        raise HTTPException(status_code=404, detail="Repository not found")
    root = row.get("root_path") or ""
    missing = not root or not os.path.isdir(root)
    git = detect_git(root) if not missing else {}
    return {
        "id": repo_id,
        "missing": missing,
        "status": "" if missing else git_status_text(root),
        "branch": git.get("branch") or "",
        "dirty": bool(git.get("dirty")),
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
