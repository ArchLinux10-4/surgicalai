"""Unified deploy-watch — poll Vercel and Railway deployment status."""
from __future__ import annotations

import json
import os
from typing import Optional

import requests as _req
from fastapi import APIRouter, HTTPException, Request

from crypto_utils import decrypt_api_key
from database import get_user_api_key

VERCEL_API = "https://api.vercel.com"
RAILWAY_GQL = "https://backboard.railway.com/graphql/v2"

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


# ── Event parsing ─────────────────────────────────────────────────────────────

_ERROR_KEYWORDS = (
    "error", "failed", "cannot find", "ts2", "ts4", "ts1",
    "expected", "is not assignable", "module not found",
    "cannot resolve", "type error", "syntax error",
    "exitcode", "exit code",
)


def _parse_events(response: _req.Response) -> list:
    """Handle both JSON array and NDJSON from Vercel events API.

    Vercel's /v3/deployments/{id}/events returns NDJSON (one JSON object per
    line), NOT a JSON array.  Calling response.json() on NDJSON raises a
    JSONDecodeError that was previously swallowed, leaving error_lines empty.
    """
    content_type = response.headers.get("content-type", "")

    # Try JSON array / object first
    if "application/json" in content_type:
        try:
            raw = response.json()
            if isinstance(raw, list):
                return raw
            return raw.get("events", raw.get("data", []))
        except Exception:
            pass

    # NDJSON fallback — one JSON object per line
    events: list = []
    for line in response.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return events


def _extract_error_lines(events: list) -> list:
    lines: list = []
    for ev in events:
        # Standard events format: {payload: {text: "..."}}
        payload = ev.get("payload") or {}
        text = payload.get("text", "").strip()
        # Some API versions put text at top level
        if not text:
            text = ev.get("text", "").strip()
        if not text:
            continue
        for sub in text.splitlines():
            sub = sub.strip()
            if sub and any(k in sub.lower() for k in _ERROR_KEYWORDS):
                lines.append(sub)

    # Deduplicate preserving order
    seen: set = set()
    deduped: list = []
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            deduped.append(ln)
    return deduped[:80]


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
        "dashboard_url": f"https://vercel.com/deployments/{dep_id}" if dep_id else "",
    }

    if state in ("ERROR", "FAILED"):
        # Primary: events endpoint (NDJSON stream)
        try:
            log_res = _req.get(
                f"{VERCEL_API}/v3/deployments/{dep_id}/events",
                headers=headers,
                params={"limit": 500, "direction": "forward"},
                timeout=20,
            )
            if log_res.status_code == 200:
                events = _parse_events(log_res)
                result["error_lines"] = _extract_error_lines(events)
        except Exception:
            pass

        # Fallback: deployment-level errorMessage
        if not result["error_lines"]:
            try:
                dep_res = _req.get(
                    f"{VERCEL_API}/v13/deployments/{dep_id}",
                    headers=headers,
                    timeout=10,
                )
                if dep_res.status_code == 200:
                    dep_data = dep_res.json()
                    err_msg = (
                        dep_data.get("errorMessage")
                        or (dep_data.get("error") or {}).get("message", "")
                    )
                    if err_msg:
                        result["error_lines"] = [err_msg]
            except Exception:
                pass

    return result


# ── Railway watch ─────────────────────────────────────────────────────────────

_RAILWAY_QUERY = """
{
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
"""


@router.get("/railway")
def watch_railway(request: Request, project_id: Optional[str] = None):
    """Return latest Railway deployment status."""
    _uid(request)  # auth gate only

    # Use per-user DB token (mirrors Vercel pattern)
    uid = getattr(request.state, "user_id", None)
    token = ""
    if uid:
        try:
            from crypto_utils import decrypt_api_key
            from database import get_user_api_key
            enc = get_user_api_key(uid, "railway")
            if enc:
                token = decrypt_api_key(enc)
        except Exception:
            pass
    # Fall back to env var for backward compat
    if not token:
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
        .get("projects", {})
        .get("edges", [])
    )

    # If project_id provided, find that specific project; otherwise pick the
    # project with the most recent deployment (removes old hardcoded name filter).
    best = None
    best_ts = ""
    for edge in projects:
        node = edge.get("node", {})
        pid = node.get("id", "")
        dep_edges = node.get("deployments", {}).get("edges", [])
        if not dep_edges:
            continue
        dep = dep_edges[0]["node"]
        if project_id and pid == project_id:
            best = (node, dep)
            break
        ts = dep.get("createdAt", "")
        if ts > best_ts:
            best_ts = ts
            best = (node, dep)

    if not best:
        return {"found": False, "state": "no_deployments"}

    node, dep = best
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
