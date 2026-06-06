"""Vercel integration router — token-based, per-user encrypted storage."""
from typing import Optional
import requests as _req
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth_utils import encrypt_api_key, decrypt_api_key
from database import get_user_api_key, set_user_api_key

VERCEL_API = "https://api.vercel.com"
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_user_id(request: Request) -> str:
    return getattr(request.state, "user_id", None)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get_token(user_id: str) -> str:
    encrypted = get_user_api_key(user_id, "vercel")
    if not encrypted:
        raise HTTPException(
            status_code=404,
            detail="Vercel not connected. Add your token in Settings → Vercel.",
        )
    return decrypt_api_key(encrypted)


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def vercel_status(request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    encrypted = get_user_api_key(user_id, "vercel")
    if not encrypted:
        return {"connected": False}
    try:
        token = decrypt_api_key(encrypted)
        res = _req.get(f"{VERCEL_API}/v2/user", headers=_headers(token), timeout=10)
        if res.status_code == 200:
            u = res.json().get("user", {})
            return {
                "connected": True,
                "username": u.get("username") or u.get("name", ""),
                "email": u.get("email", ""),
                "avatar_url": u.get("avatar", ""),
            }
        return {"connected": False}
    except Exception:
        return {"connected": False}


# ── Connect / Disconnect ──────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    token: str


@router.post("/connect")
def vercel_connect(body: ConnectRequest, request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tok = body.token.strip()
    try:
        res = _req.get(f"{VERCEL_API}/v2/user", headers=_headers(tok), timeout=10)
        if res.status_code == 401:
            raise HTTPException(
                status_code=401,
                detail="Invalid token. Make sure it has the correct permissions.",
            )
        res.raise_for_status()
        u = res.json().get("user", {})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection failed: {e}")

    set_user_api_key(user_id, "vercel", encrypt_api_key(tok))
    return {
        "ok": True,
        "username": u.get("username") or u.get("name", ""),
        "email": u.get("email", ""),
        "avatar_url": u.get("avatar", ""),
    }


@router.delete("/disconnect")
def vercel_disconnect(request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    set_user_api_key(user_id, "vercel", "")
    return {"ok": True}


# ── Projects ──────────────────────────────────────────────────────────────────

@router.get("/projects")
def list_projects(request: Request):
    user_id = _get_user_id(request)
    token = _get_token(user_id)
    res = _req.get(f"{VERCEL_API}/v9/projects", headers=_headers(token), timeout=15)
    res.raise_for_status()
    projects = []
    for p in res.json().get("projects", []):
        latest = (p.get("latestDeployments") or [{}])[0]
        projects.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "framework": p.get("framework"),
            "updated_at": p.get("updatedAt"),
            "latest_state": latest.get("readyState") or latest.get("state"),
            "latest_url": latest.get("url"),
            "latest_created": latest.get("createdAt"),
        })
    return {"projects": projects}


# ── Deployments ───────────────────────────────────────────────────────────────

@router.get("/deployments")
def list_deployments(
    request: Request,
    project_id: Optional[str] = None,
    limit: int = 20,
):
    user_id = _get_user_id(request)
    token = _get_token(user_id)
    params: dict = {"limit": limit}
    if project_id:
        params["projectId"] = project_id
    res = _req.get(
        f"{VERCEL_API}/v6/deployments",
        headers=_headers(token),
        params=params,
        timeout=15,
    )
    res.raise_for_status()
    deployments = []
    for d in res.json().get("deployments", []):
        deployments.append({
            "id": d.get("uid"),
            "url": d.get("url"),
            "name": d.get("name"),
            "state": d.get("state"),
            "created_at": d.get("created"),
            "ready_at": d.get("ready"),
            "target": d.get("target"),
            "creator": (d.get("creator") or {}).get("username", ""),
            "meta": d.get("meta", {}),
        })
    return {"deployments": deployments}


@router.get("/deployments/{deployment_id}")
def get_deployment(deployment_id: str, request: Request):
    user_id = _get_user_id(request)
    token = _get_token(user_id)
    res = _req.get(
        f"{VERCEL_API}/v13/deployments/{deployment_id}",
        headers=_headers(token),
        timeout=15,
    )
    res.raise_for_status()
    d = res.json()
    return {
        "id": d.get("id") or d.get("uid"),
        "url": d.get("url"),
        "name": d.get("name"),
        "state": d.get("readyState") or d.get("state"),
        "created_at": d.get("createdAt"),
        "ready_at": d.get("readyAt"),
        "target": d.get("target"),
        "error": d.get("errorMessage"),
        "git_source": d.get("gitSource"),
    }


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/deployments/{deployment_id}/logs")
def get_deployment_logs(
    deployment_id: str,
    request: Request,
    limit: int = 200,
):
    user_id = _get_user_id(request)
    token = _get_token(user_id)
    res = _req.get(
        f"{VERCEL_API}/v3/deployments/{deployment_id}/events",
        headers=_headers(token),
        params={"limit": limit, "direction": "forward"},
        timeout=20,
    )
    res.raise_for_status()
    raw = res.json()
    events = raw if isinstance(raw, list) else raw.get("events", [])
    logs = []
    for ev in events:
        payload = ev.get("payload", {})
        text = payload.get("text", "")
        if text:
            logs.append({
                "created": ev.get("created"),
                "type": payload.get("type", "stdout"),
                "text": text,
            })
    return {"logs": logs, "deployment_id": deployment_id}
