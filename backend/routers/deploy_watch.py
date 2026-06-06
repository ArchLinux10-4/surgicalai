"""Unified deploy-watch — poll Vercel and Railway deployment status."""
from __future__ import annotations

import os
from typing import Optional

import requests as _req
from fastapi import APIRouter, HTTPException, Request

from crypto_utils import decrypt_api_key
from database import get_user_api_key

VERCEL_API = "https://api.vercel.com"
RAILWAY_GQL = "https://api.railway.app/graphql/v2"

router = APIRouter()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _uid(request: Request) -> str:
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return uid


def _vercel_token(user_id: str) -> Optional[str]:
    encrypted = get_user_api_key(user_id, "vercel")
    if not encrypted:
        return None
    return decrypt_api_key(encrypted)


def _vercel_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Error extraction ──────────────────────────────────────────────────────────

_ERROR_KEYWORDS = (
    "error", "failed", "cannot find", "ts2", "ts4", "ts1",
    "expected", "is not assignable", "module not found",
    "cannot resolve", "type error", "syntax error",
)


def _extract_error_lines(events: list) -> list:
    lines = []
    for ev in events:
        text = (ev.get("payload") or {}).get("text", "").strip()
        if text and any(k in text.lower() for k in _ERROR_KEYWORDS):
            lines.append(text)
    return lines[:80]


# ── Vercel watch ──────────────────────────────────────────────────────────────

@router.get("/vercel")
def watch_vercel(
    request: Request,
    project_id: Optional[str] = None,
):
    """Return the latest Vercel deployment state; include error lines if failed."""
    uid = _uid(request)
    token = _vercel_token(uid)
    if not token:
        return {"found": False, "state": "no_token"}

    headers = _vercel_headers(token)
    params: dict = {"limit": 1}
    if project_id:
        params["projectId"] = project_id

    try:
        res = _req.get(
            f"{VERCEL_API}/v6/deployments",
            headers=headers,
            params=params,
            timeout=15,
        )
    except Exception as e:
        return {"found": False, "state": "api_error", "detail": str(e)}

    if res.status_code != 200:
        return {"found": False, "state": "api_error", "detail": res.text[:200]}

    deployments = res.json().get("deployments", [])
    if not deployments:
        return {"found": False, "state": "no_deployments"}

    d = deployments[0]
    dep_id = d.get("uid") or d.get("id", "")
    state = (d.get("state") or "BUILDING").upper()

    result: dict = {
        "found": True,
        "state": state,
        "deployment_id": dep_id,
        "url": d.get("url", ""),
        "created_at": d.get("created"),
        "error_lines": [],
    }

    if state in ("ERROR", "FAILED"):
        try:
            log_res = _req.get(
                f"{VERCEL_API}/v3/deployments/{dep_id}/events",
                headers=headers,
                params={"limit": 500, "direction": "forward"},
                timeout=20,
            )
            if log_res.status_code == 200:
                raw = log_res.json()
                events = raw if isinstance(raw, list) else raw.get("events", [])
                result["error_lines"] = _extract_error_lines(events)
        except Exception:
            pass

    return result


# ── Railway watch ─────────────────────────────────────────────────────────────

_RAILWAY_QUERY = """
{
  me {
    projects {
      edges {
        node {
          id
          name
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


@router.get("/railway")
def watch_railway(request: Request):
    """Return latest Railway deployment status for the surgicalai project."""
    _uid(request)  # auth gate only

    token = os.getenv("RAILWAY_API_TOKEN", "")
    if not token:
        return {"found": False, "state": "no_token"}

    try:
        res = _req.post(
            RAILWAY_GQL,
            json={"query": _RAILWAY_QUERY},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=15,
        )
        data = res.json()
    except Exception as e:
        return {"found": False, "state": "api_error", "error": str(e)}

    if "errors" in data:
        return {"found": False, "state": "api_error", "error": str(data["errors"][:1])}

    projects = (
        (data.get("data") or {})
        .get("me", {})
        .get("projects", {})
        .get("edges", [])
    )

    for edge in projects:
        node = edge.get("node", {})
        name = (node.get("name") or "").lower()
        if "surgical" not in name:
            continue
        dep_edges = node.get("deployments", {}).get("edges", [])
        if not dep_edges:
            continue
        dep = dep_edges[0]["node"]
        status = (dep.get("status") or "BUILDING").upper()
        proj_id = node.get("id", "")
        return {
            "found": True,
            "state": status,
            "deployment_id": dep.get("id", ""),
            "url": dep.get("staticUrl") or "",
            "created_at": dep.get("createdAt"),
            "error_lines": [],
            "dashboard_url": f"https://railway.com/project/{proj_id}",
        }

    return {"found": False, "state": "no_deployments"}
