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
from database import get_user_api_key, set_user_api_key, _dlog

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

_Q_VERIFY = "{ projects { edges { node { id } } } }"

_Q_PROJECTS = """
{
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
        data = _gql(token, _Q_VERIFY)
        if data.get("projects") is not None:
            return {"connected": True, "name": "", "email": ""}
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
        data = _gql(tok, _Q_VERIFY)
        if data.get("projects") is None:
            raise HTTPException(status_code=401, detail="Invalid Railway token.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Connection failed: {e}")

    set_user_api_key(user_id, "railway", encrypt_api_key(tok))
    return {
        "ok": True,
        "name": "",
        "email": "",
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
    raw_projects = (data.get("projects") or {}).get("edges", [])
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
    return {"projects": projects, "user_name": "", "user_email": ""}


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


# ── Deployment logs (build + runtime) ─────────────────────────────────────────
# Schema verified against Railway's live GraphQL API (introspection, 2026-07-05):
#   buildLogs(deploymentId: String!, limit: Int, ...) -> [Log!]!
#   deploymentLogs(deploymentId: String!, limit: Int, ...) -> [Log!]!
#   Log fields: timestamp, message, severity, tags, attributes

_Q_BUILD_LOGS = """
query($deploymentId: String!, $limit: Int) {
  buildLogs(deploymentId: $deploymentId, limit: $limit) {
    timestamp
    message
    severity
  }
}
"""

_Q_DEPLOY_LOGS = """
query($deploymentId: String!, $limit: Int) {
  deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
    timestamp
    message
    severity
  }
}
"""


@router.get("/deployments/{deployment_id}/logs")
def get_deployment_logs(
    deployment_id: str,
    request: Request,
    kind: str = "build",
    limit: int = 200,
):
    """Fetch build or runtime logs for a Railway deployment.

    kind=build  -> buildLogs (compile/deploy phase; where build failures live)
    kind=deploy -> deploymentLogs (runtime logs after the service started)
    """
    user_id = _get_user_id(request)
    _dlog("railway_logs_request", deployment_id=deployment_id, kind=kind,
          limit=limit, has_user=bool(user_id))
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if kind not in ("build", "deploy"):
        _dlog("railway_logs_bad_kind", kind=kind)
        raise HTTPException(status_code=400, detail="kind must be 'build' or 'deploy'")

    # Clamp limit to a sane range so a bad caller can't request huge payloads.
    if limit < 1:
        limit = 1
    if limit > 500:
        _dlog("railway_logs_limit_clamped", requested=limit)
        limit = 500

    token = _get_token(user_id)

    query = _Q_BUILD_LOGS if kind == "build" else _Q_DEPLOY_LOGS
    field = "buildLogs" if kind == "build" else "deploymentLogs"
    try:
        data = _gql(token, query, {"deploymentId": deployment_id, "limit": limit})
    except HTTPException as he:
        _dlog("railway_logs_gql_error", deployment_id=deployment_id, kind=kind,
              detail=str(he.detail))
        raise
    except Exception as e:
        _dlog("railway_logs_failed", deployment_id=deployment_id, kind=kind,
              error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

    raw_logs = data.get(field) or []
    logs = []
    for entry in raw_logs:
        if not isinstance(entry, dict):
            continue
        logs.append({
            "timestamp": entry.get("timestamp", ""),
            "message": entry.get("message", ""),
            "severity": entry.get("severity", ""),
        })
    _dlog("railway_logs_ok", deployment_id=deployment_id, kind=kind,
          count=len(logs))
    return {"deployment_id": deployment_id, "kind": kind, "logs": logs}
