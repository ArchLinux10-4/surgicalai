"""Git router."""
from fastapi import APIRouter, HTTPException, Request
from models.schemas import GitStatus, GitCommitRequest, GitDiffRequest
from services.git_service import get_status, get_diff, stage_file, commit, get_log

router = APIRouter()


def _allowed(repo_path: str, request: Request) -> str:
    from services.repositories import assert_git_path_allowed
    user_id = getattr(request.state, "user_id", "") or ""
    return assert_git_path_allowed(user_id, repo_path)


@router.get("/status")
def git_status(repo_path: str, request: Request):
    repo_path = _allowed(repo_path, request)
    try:
        return get_status(repo_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/diff")
def git_diff(repo_path: str, request: Request, file_path: str = None):
    repo_path = _allowed(repo_path, request)
    if file_path:
        from services.repositories import resolve_inside
        resolve_inside(repo_path, file_path)
    try:
        return {"diff": get_diff(repo_path, file_path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/commit")
def git_commit(req: GitCommitRequest, request: Request):
    repo_path = _allowed(req.repo_path, request)
    ok, msg = commit(repo_path, req.message, req.files)
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return {"ok": True, "message": msg}


@router.get("/log")
def git_log(repo_path: str, request: Request, limit: int = 20):
    repo_path = _allowed(repo_path, request)
    try:
        return get_log(repo_path, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
