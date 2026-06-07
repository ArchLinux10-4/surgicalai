"""Railway integration router — token-based, per-user encrypted storage.

Mirrors the Vercel router pattern exactly:
  POST /api/railway/connect    — validate + store token
  DELETE /api/railway/disconnect
  GET  /api/railway/status
  GET  /api/railway/projects
  GET  /api/railway/projects/{project_id}/deployments
"""
from typing import Optional
import requests as _req
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from crypto_utils import encrypt_api_key, decrypt_api_key
from database import get_user_api_key, set_user_api_key

RAILWAY_GQL = "https://backboard.railway.com/graphql/v2"
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_user_id(request: Request) -> Optional[str]:
    return getattr(request.state, "user_id", None)


def _gql_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _gql(token: str, query: str, variables: Optional[dict] = None) -> dict:
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    res = _req.post(
        RAILWAY_GQL,
        json=payload,
        headers=_gql_headers(token),
        timeout=15,
    )
    res.raise_for_status()
    data = res.json()
    if "errors" in data:
        msgs = "; ".join(e.get("message", str(e)) for e in data["errors"])
        raise HTTPException(status_code=400, detail=f"Railway API error: {msgs}")
    return data.get("data", {})


def _get_token(user_id: str) -> str:
    encrypted = get_user_api_key(user_id, "railway")
    if not encrypted:
        raise HTTPException(
            status_code=404,
            detail="Railway not connected. Add your token in Settings → Railway.",
        )
    return decrypt_api_key(encrypted)


# ── GraphQL queries ───────────────────────────────────────────────────────────

_Q_ME = "{ me { id name email } }"

_Q_PROJECTS = """
{
  me {
    id
    name
    email
    projects {
      edges {
        node {
          id
          name
          description
          createdAt
          updatedAt
          services {
            edges {
              node {
                id
                name
              }
            }
          }
          deployments(first: 1) {
            edges {
              node {
                id
                status
                createdAt
                staticUrl
              }
            }
          }
        }
      }
    }
  }
}
"""

_Q_PROJECT_DEPLOYMENTS = """
query($projectId: String!) {
  project(id: $projectId) {
    id
    name
    deployments(first: 15) {
      edges {
        node {
          id
          status
          createdAt
          updatedAt
          staticUrl
          service {
            id
            name
          }
          environment {
            id
            name
          }
        }
      }
    }
  }
}
"""


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def railway_status(request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    encrypted = get_user_api_key(user_id, "railway")
    if not encrypted:
        return {"connected": False}
    try:
        token = decrypt_api_key(encrypted)
        data = _gql(token, _Q_ME)
        me = data.get("me", {})
        if me.get("id"):
            return {
                "connected": True,
                "name": me.get("name", ""),
                "email": me.get("email", ""),
            }
        return {"connected": False}
    except Exception:
        return {"connected": False}


# ── Connect / Disconnect ──────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    token: str


@router.post("/connect")
def railway_connect(body: ConnectRequest, request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tok = body.token.strip()
    try:
        data = _gql(tok, _Q_ME)
        me = data.get("me", {})
        if not me.get("id"):
            raise HTTPException(status_code=401, detail="Invalid token — could not fetch user info.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection failed: {e}")

    set_user_api_key(user_id, "railway", encrypt_api_key(tok))
    return {
        "ok": True,
        "name": me.get("name", ""),
        "email": me.get("email", ""),
    }


@router.delete("/disconnect")
def railway_disconnect(request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    set_user_api_key(user_id, "railway", "")
    return {"ok": True}


# ── Projects ──────────────────────────────────────────────────────────────────

@router.get("/projects")
def list_projects(request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = _get_token(user_id)
    data = _gql(token, _Q_PROJECTS)
    me = data.get("me", {})
    raw_projects = (me.get("projects") or {}).get("edges", [])
    projects = []
    for edge in raw_projects:
        node = edge.get("node", {})
        dep_edges = (node.get("deployments") or {}).get("edges", [])
        latest_dep = dep_edges[0]["node"] if dep_edges else {}
        services = [
            {"id": s["node"]["id"], "name": s["node"]["name"]}
            for s in (node.get("services") or {}).get("edges", [])
        ]
        projects.append({
            "id": node.get("id"),
            "name": node.get("name"),
            "description": node.get("description", ""),
            "created_at": node.get("createdAt"),
            "updated_at": node.get("updatedAt"),
            "services": services,
            "latest_status": latest_dep.get("status", ""),
            "latest_deployment_id": latest_dep.get("id", ""),
            "latest_url": latest_dep.get("staticUrl", ""),
            "latest_created": latest_dep.get("createdAt", ""),
        })
    return {"projects": projects, "user_name": me.get("name", ""), "user_email": me.get("email", "")}


# ── Deployments for a project ─────────────────────────────────────────────────

@router.get("/projects/{project_id}/deployments")
def list_project_deployments(project_id: str, request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = _get_token(user_id)
    try:
        data = _gql(token, _Q_PROJECT_DEPLOYMENTS, {"projectId": project_id})
        project = data.get("project", {})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    dep_edges = (project.get("deployments") or {}).get("edges", [])
    deployments = []
    for edge in dep_edges:
        n = edge.get("node", {})
        svc = n.get("service") or {}
        env = n.get("environment") or {}
        deployments.append({
            "id": n.get("id"),
            "status": n.get("status", ""),
            "created_at": n.get("createdAt"),
            "updated_at": n.get("updatedAt"),
            "url": n.get("staticUrl", ""),
            "service_id": svc.get("id", ""),
            "service_name": svc.get("name", ""),
            "environment_id": env.get("id", ""),
            "environment_name": env.get("name", "production"),
        })
    return {
        "project_id": project_id,
        "project_name": project.get("name", ""),
        "deployments": deployments,
    }
