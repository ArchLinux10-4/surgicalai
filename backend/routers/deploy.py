"""
Deploy status router.
Polls Vercel + Railway APIs after a GitHub commit to show build status inside SurgicalAI.
Note: GitHub push already triggers deployments automatically — this just shows the STATUS.
Per-user encrypted tokens stored in user_api_keys (key_type='vercel_token' / 'railway_token').
"""
from fastapi import APIRouter, HTTPException, Request
import httpx

from database import get_user_api_key, set_user_api_key, get_setting, set_setting
from crypto_utils import encrypt_api_key, decrypt_api_key
from auth_utils import decode_token

router = APIRouter()


def _get_user_id(request: Request) -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        payload = decode_token(token)
        if payload:
            return str(payload.get("user_id", ""))
    return ""


def _get_token(user_id: str, key_type: str) -> str:
    if user_id:
        encrypted = get_user_api_key(user_id, key_type)
        if encrypted:
            try:
                return decrypt_api_key(encrypted)
            except Exception:
                pass
    return get_setting(f"{key_type}", "")


@router.get("/status")
def deploy_status(request: Request, repo: str = "", branch: str = "main"):
    """Get the latest deployment status from Vercel and Railway."""
    user_id = _get_user_id(request)
    vercel_token = _get_token(user_id, "vercel_token")
    railway_token = _get_token(user_id, "railway_token")

    results = {}

    # ── Vercel status ──
    if vercel_token:
        try:
            headers = {"Authorization": f"Bearer {vercel_token}"}
            params = {"limit": 3}
            if repo:
                # Try to extract project name from repo "owner/name"
                project_name = repo.split("/")[-1] if "/" in repo else repo
                params["projectId"] = project_name
            resp = httpx.get("https://api.vercel.com/v6/deployments", headers=headers,
                             params=params, timeout=10)
            if resp.status_code == 200:
                deployments = resp.json().get("deployments", [])
                if deployments:
                    d = deployments[0]
                    state_map = {
                        "READY": "success", "ERROR": "failed",
                        "BUILDING": "building", "QUEUED": "queued", "CANCELED": "canceled",
                    }
                    raw_state = d.get("state", "UNKNOWN")
                    results["vercel"] = {
                        "status": state_map.get(raw_state, "unknown"),
                        "raw_state": raw_state,
                        "url": f"https://{d.get('url', '')}" if d.get("url") else None,
                        "created_at": d.get("createdAt"),
                        "name": d.get("name", ""),
                    }
                else:
                    results["vercel"] = {"status": "no_deployments"}
        except Exception as e:
            results["vercel"] = {"status": "error", "message": str(e)[:100]}
    else:
        results["vercel"] = {"status": "not_configured"}

    # ── Railway status ──
    if railway_token:
        try:
            gql = """
            query {
              me {
                projects {
                  edges {
                    node {
                      id name
                      deployments(first: 1) {
                        edges {
                          node {
                            id status createdAt
                            environment { name }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }"""
            resp = httpx.post(
                "https://backboard.railway.app/graphql/v2",
                json={"query": gql},
                headers={"Authorization": f"Bearer {railway_token}",
                         "Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                projects = data.get("me", {}).get("projects", {}).get("edges", [])
                # Find matching project or use first
                target_project = None
                if repo:
                    proj_name = repo.split("/")[-1].lower() if "/" in repo else repo.lower()
                    for pe in projects:
                        if proj_name in pe["node"]["name"].lower():
                            target_project = pe["node"]
                            break
                if not target_project and projects:
                    target_project = projects[0]["node"]

                if target_project:
                    deps = target_project.get("deployments", {}).get("edges", [])
                    if deps:
                        d = deps[0]["node"]
                        state_map = {
                            "SUCCESS": "success", "FAILED": "failed",
                            "DEPLOYING": "building", "INITIALIZING": "queued",
                            "CRASHED": "failed", "REMOVED": "canceled",
                        }
                        raw_state = d.get("status", "UNKNOWN")
                        results["railway"] = {
                            "status": state_map.get(raw_state, "unknown"),
                            "raw_state": raw_state,
                            "project": target_project.get("name", ""),
                            "environment": d.get("environment", {}).get("name", ""),
                            "created_at": d.get("createdAt"),
                        }
                    else:
                        results["railway"] = {"status": "no_deployments"}
                else:
                    results["railway"] = {"status": "no_projects"}
        except Exception as e:
            results["railway"] = {"status": "error", "message": str(e)[:100]}
    else:
        results["railway"] = {"status": "not_configured"}

    return results


@router.post("/connect")
async def deploy_connect(body: dict, request: Request):
    """Save Vercel and/or Railway tokens."""
    user_id = _get_user_id(request)
    saved = []

    vercel_token = (body.get("vercel_token") or "").strip()
    if vercel_token:
        # Quick verify — list projects
        try:
            resp = httpx.get("https://api.vercel.com/v9/projects",
                             headers={"Authorization": f"Bearer {vercel_token}"}, timeout=8)
            if resp.status_code not in (200, 201):
                raise HTTPException(status_code=401, detail="Invalid Vercel token")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not verify Vercel token: {e}")
        if user_id:
            set_user_api_key(user_id, "vercel_token", encrypt_api_key(vercel_token))
        set_setting("vercel_token", vercel_token)
        saved.append("vercel")

    railway_token = (body.get("railway_token") or "").strip()
    if railway_token:
        try:
            resp = httpx.post(
                "https://backboard.railway.app/graphql/v2",
                json={"query": "{ me { id name } }"},
                headers={"Authorization": f"Bearer {railway_token}",
                         "Content-Type": "application/json"},
                timeout=8,
            )
            if resp.status_code != 200 or "errors" in resp.json():
                raise HTTPException(status_code=401, detail="Invalid Railway token")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not verify Railway token: {e}")
        if user_id:
            set_user_api_key(user_id, "railway_token", encrypt_api_key(railway_token))
        set_setting("railway_token", railway_token)
        saved.append("railway")

    if not saved:
        raise HTTPException(status_code=400, detail="No tokens provided")
    return {"ok": True, "saved": saved}


@router.get("/config")
def deploy_config(request: Request):
    """Return which deploy services are configured for this user."""
    user_id = _get_user_id(request)
    has_vercel = bool(_get_token(user_id, "vercel_token"))
    has_railway = bool(_get_token(user_id, "railway_token"))
    return {"vercel": has_vercel, "railway": has_railway}
