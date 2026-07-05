"""deploy_status.py — server-side executor for the check_deploy natural tag.

Lets the model answer "did my deploy go through?" / "why did the build fail?"
in plain chat. Read-only: reuses the SAME token storage and API calls as the
proven Vercel/Railway routers (routers/vercel.py, routers/railway.py) — no
new API clients, no new credentials, no writes anywhere.

Behavior:
  - Checks every provider the user has connected (Vercel and/or Railway).
  - Reports the latest deployment per provider/project: status, commit sha
    (when the platform exposes it), timestamp, and URL — so the model can
    self-verify it is looking at the deploy of the commit it just pushed.
  - ONLY on a failed deployment does it fetch logs, and only a short tail
    (last _LOG_TAIL lines) so a huge build log can never blow up the context.
  - Neither provider connected -> friendly "connect in Settings" message.

Every function degrades gracefully: this module NEVER raises into the
streaming loop — it always returns a string.
"""
from datetime import datetime, timezone
from typing import Callable, Optional

_LOG_TAIL = 50          # max log lines returned for a failed deploy
_MAX_RAILWAY_PROJECTS = 5  # sanity cap; users rarely have more

# Terminal-failure states per platform (verified against live router data:
# Vercel readyState docs: QUEUED/BUILDING/INITIALIZING/READY/ERROR/CANCELED;
# Railway DeploymentStatus enum includes FAILED and CRASHED).
_VERCEL_FAILED = ("ERROR",)
_RAILWAY_FAILED = ("FAILED", "CRASHED")


def _safe_dlog(dlog: Optional[Callable], event: str, **kw) -> None:
    if dlog is None:
        return
    try:
        dlog(event, **kw)
    except Exception:
        pass


def _fmt_epoch_ms(ms) -> str:
    """Vercel timestamps are epoch milliseconds. Never raises."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ms or "?")


def _decrypted_token(user_id: str, provider: str,
                     dlog: Optional[Callable]) -> Optional[str]:
    """Return the user's decrypted token for 'vercel'/'railway', or None.
    Never raises (unlike the routers' _get_token, which raises HTTPException)."""
    try:
        from database import get_user_api_key
        from crypto_utils import decrypt_api_key
        encrypted = get_user_api_key(user_id, provider)
        if not encrypted:
            _safe_dlog(dlog, "check_deploy_not_connected",
                       user_id=user_id, provider=provider)
            return None
        return decrypt_api_key(encrypted)
    except Exception as e:
        _safe_dlog(dlog, "check_deploy_token_error",
                   user_id=user_id, provider=provider, error=str(e))
        return None


# ── Vercel ────────────────────────────────────────────────────────────────────

def _vercel_log_tail(token: str, deployment_id: str,
                     dlog: Optional[Callable]) -> str:
    """Tail of build events for one Vercel deployment. Same endpoint as
    routers/vercel.py get_deployment_logs (v3 events). Never raises."""
    try:
        import requests as _req
        res = _req.get(
            f"https://api.vercel.com/v3/deployments/{deployment_id}/events",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            params={"limit": 200, "direction": "forward"},
            timeout=20,
        )
        res.raise_for_status()
        raw = res.json()
        events = raw if isinstance(raw, list) else raw.get("events", [])
        lines = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            text = (ev.get("payload") or {}).get("text", "")
            if text:
                lines.append(text.rstrip())
        tail = lines[-_LOG_TAIL:]
        _safe_dlog(dlog, "check_deploy_vercel_logs_ok",
                   deployment_id=deployment_id,
                   total_lines=len(lines), tail_lines=len(tail))
        return "\n".join(tail) if tail else "(no log output returned)"
    except Exception as e:
        _safe_dlog(dlog, "check_deploy_vercel_logs_failed",
                   deployment_id=deployment_id, error=str(e))
        return f"(could not fetch Vercel logs: {e})"


def _vercel_section(user_id: str, dlog: Optional[Callable]) -> Optional[str]:
    """Latest Vercel deployment summary, or None when not connected.
    Same endpoint as routers/vercel.py list_deployments (v6). Never raises."""
    token = _decrypted_token(user_id, "vercel", dlog)
    if not token:
        return None
    try:
        import requests as _req
        res = _req.get(
            "https://api.vercel.com/v6/deployments",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            params={"limit": 5},
            timeout=15,
        )
        res.raise_for_status()
        deployments = res.json().get("deployments", [])
        _safe_dlog(dlog, "check_deploy_vercel_list_ok",
                   user_id=user_id, count=len(deployments))
        if not deployments:
            return "VERCEL: connected, but no deployments found."

        d = deployments[0]  # newest first
        state = (d.get("state") or d.get("readyState") or "?").upper()
        dep_id = d.get("uid") or ""
        url = d.get("url") or ""
        created = _fmt_epoch_ms(d.get("created"))
        meta = d.get("meta") or {}
        sha = (meta.get("githubCommitSha") or "")[:10]
        commit_msg = (meta.get("githubCommitMessage") or "").splitlines()[0][:100] \
            if meta.get("githubCommitMessage") else ""
        _safe_dlog(dlog, "check_deploy_vercel_latest",
                   user_id=user_id, deployment_id=dep_id, state=state,
                   sha=sha, created=created)

        lines = [f"VERCEL — latest deployment ({d.get('name', '?')}):",
                 f"  status: {state}",
                 f"  commit: {sha or 'unknown'}"
                 + (f' — "{commit_msg}"' if commit_msg else ""),
                 f"  created: {created}",
                 f"  url: https://{url}" if url else "  url: unknown"]

        if state in _VERCEL_FAILED and dep_id:
            _safe_dlog(dlog, "check_deploy_vercel_fetching_logs",
                       user_id=user_id, deployment_id=dep_id)
            lines.append(f"  BUILD LOG TAIL (last {_LOG_TAIL} lines):")
            lines.append(_vercel_log_tail(token, dep_id, dlog))
        return "\n".join(lines)
    except Exception as e:
        _safe_dlog(dlog, "check_deploy_vercel_failed",
                   user_id=user_id, error=str(e))
        return f"VERCEL: connected, but the status check failed: {e}"


# ── Railway ───────────────────────────────────────────────────────────────────

def _railway_log_tail(token: str, deployment_id: str,
                      dlog: Optional[Callable]) -> str:
    """Build-log tail for one Railway deployment. Reuses the exact query
    shipped in routers/railway.py (schema verified against the live
    GraphQL API). Never raises."""
    try:
        from routers.railway import _gql, _Q_BUILD_LOGS
        data = _gql(token, _Q_BUILD_LOGS,
                    {"deploymentId": deployment_id, "limit": 500})
        raw_logs = data.get("buildLogs") or []
        lines = []
        for entry in raw_logs:
            if isinstance(entry, dict) and entry.get("message"):
                lines.append(entry["message"].rstrip())
        tail = lines[-_LOG_TAIL:]
        _safe_dlog(dlog, "check_deploy_railway_logs_ok",
                   deployment_id=deployment_id,
                   total_lines=len(lines), tail_lines=len(tail))
        return "\n".join(tail) if tail else "(no build log output returned)"
    except Exception as e:
        _safe_dlog(dlog, "check_deploy_railway_logs_failed",
                   deployment_id=deployment_id, error=str(e))
        return f"(could not fetch Railway build logs: {e})"


def _railway_section(user_id: str, dlog: Optional[Callable]) -> Optional[str]:
    """Latest Railway deployment per project, or None when not connected.
    Reuses routers/railway.py's proven _gql helper + projects query.
    Never raises."""
    token = _decrypted_token(user_id, "railway", dlog)
    if not token:
        return None
    try:
        from routers.railway import _gql, _Q_PROJECTS
        data = _gql(token, _Q_PROJECTS)
        edges = (data.get("projects") or {}).get("edges", [])
        _safe_dlog(dlog, "check_deploy_railway_projects_ok",
                   user_id=user_id, count=len(edges))
        if not edges:
            return "RAILWAY: connected, but no projects found."

        lines = ["RAILWAY — latest deployment per project:"]
        for edge in edges[:_MAX_RAILWAY_PROJECTS]:
            node = edge.get("node") or {}
            name = node.get("name", "?")
            dep_edges = (node.get("deployments") or {}).get("edges", [])
            if not dep_edges:
                lines.append(f"  {name}: no deployments")
                continue
            dep = dep_edges[0].get("node") or {}
            status = (dep.get("status") or "?").upper()
            dep_id = dep.get("id") or ""
            created = dep.get("createdAt") or "?"
            url = dep.get("staticUrl") or ""
            _safe_dlog(dlog, "check_deploy_railway_latest",
                       user_id=user_id, project=name,
                       deployment_id=dep_id, status=status, created=created)
            lines.append(f"  {name}: {status} (created {created})"
                         + (f" — https://{url}" if url else ""))
            if status in _RAILWAY_FAILED and dep_id:
                _safe_dlog(dlog, "check_deploy_railway_fetching_logs",
                           user_id=user_id, deployment_id=dep_id)
                lines.append(f"  BUILD LOG TAIL for {name} "
                             f"(last {_LOG_TAIL} lines):")
                lines.append(_railway_log_tail(token, dep_id, dlog))
        return "\n".join(lines)
    except Exception as e:
        _safe_dlog(dlog, "check_deploy_railway_failed",
                   user_id=user_id, error=str(e))
        return f"RAILWAY: connected, but the status check failed: {e}"


# ── Entry point (called from github_natural_tag.execute_github_request) ──────

def check_deploy_status(user_id: str, args: dict,
                        dlog: Optional[Callable] = None) -> str:
    """Execute one check_deploy request. Always returns a string; never raises.

    args (all optional):
      provider: "vercel" | "railway" | "both" (default "both")
    """
    try:
        provider = str((args or {}).get("provider", "both")).strip().lower()
        if provider not in ("vercel", "railway", "both"):
            _safe_dlog(dlog, "check_deploy_bad_provider",
                       user_id=user_id, provider=provider)
            provider = "both"
        _safe_dlog(dlog, "check_deploy_start",
                   user_id=user_id, provider=provider)

        sections = []
        if provider in ("vercel", "both"):
            s = _vercel_section(user_id, dlog)
            if s:
                sections.append(s)
        if provider in ("railway", "both"):
            s = _railway_section(user_id, dlog)
            if s:
                sections.append(s)

        if not sections:
            _safe_dlog(dlog, "check_deploy_none_connected", user_id=user_id)
            return ("No deploy platform is connected. The user can connect "
                    "Vercel or Railway in Settings to enable deploy status "
                    "checks in chat.")

        result = "\n\n".join(sections)
        _safe_dlog(dlog, "check_deploy_done",
                   user_id=user_id, sections=len(sections),
                   result_len=len(result))
        return result
    except Exception as e:
        _safe_dlog(dlog, "check_deploy_error", user_id=user_id, error=str(e))
        return f"[check_deploy failed: {e}]"
