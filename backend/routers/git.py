"""Git router."""
from fastapi import APIRouter, HTTPException
from models.schemas import GitStatus, GitCommitRequest, GitDiffRequest
from services.git_service import get_status, get_diff, stage_file, commit, get_log

router = APIRouter()


@router.get("/status")
def git_status(repo_path: str):
    try:
        return get_status(repo_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/diff")
def git_diff(repo_path: str, file_path: str = None):
    try:
        return {"diff": get_diff(repo_path, file_path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/commit")
def git_commit(req: GitCommitRequest):
    ok, msg = commit(req.repo_path, req.message, req.files)
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return {"ok": True, "message": msg}


@router.get("/log")
def git_log(repo_path: str, limit: int = 20):
    try:
        return get_log(repo_path, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
