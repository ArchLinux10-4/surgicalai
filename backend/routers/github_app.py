"""
github_app.py — NEW GitHub App connection flow (additive, flag-gated).

This is a parallel, opt-in path alongside the existing PAT flow in
routers/github.py, which is completely untouched. Users can keep using the
battle-tested PAT flow forever; this only adds a second, more secure option.

Endpoints:
  GET    /api/github-app/config            — public app metadata (no secrets)
  GET    /api/github-app/install-url       — signed install link for this user
  GET    /api/github-app/callback          — GitHub redirects browser here after install
  GET    /api/github-app/status            — this user's connected installations
  POST   /api/github-app/permission-tier   — set read_only/read_comment/read_write
  DELETE /api/github-app/{installation_id} — forget a connected installation
  GET    /api/github-app/repos             — repos across all of this user's installations
  GET    /api/github-app/repos/{owner}/{repo}/branches
  GET    /api/github-app/repos/{owner}/{repo}/tree
  POST   /api/github-app/load              — load files into a session
  POST   /api/github-app/commit            — commit + optional PR

Multi-tenant: every DB row is scoped by user_id + installation_id. A user's
token can only ever reach the repos their own installation was granted.
"""
import base64
import json
import os
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from jose import JWTError, jwt

from database import (
    _dlog, get_db_ctx,
    save_github_app_installation, list_github_app_installations,
    get_github_app_installation, delete_github_app_installation,
    set_github_app_permission_tier,
)
from auth_utils import get_jwt_secret
from services.github_app_auth import (
    is_github_app_configured, get_app_slug, get_installation_client,
    get_installation_account_info, get_integration,
)

router = APIRouter()

_STATE_ALGO = "HS256"
_VALID_TIERS = {"read_only", "read_comment", "read_write"}

# Where to send the browser back to after a successful/failed install —
# the SPA route that shows the GitHub settings panel.
_FRONTEND_RETURN_URL = os.getenv("FRONTEND_URL", "https://surgicalai-alpha.vercel.app") + "/settings"


def _get_user_id(request: Request) -> str:
    return getattr(request.state, "user_id", "") or ""


def _make_install_state(user_id: str) -> str:
    """Short-lived signed token (15 min) carrying the user_id through
    GitHub's install redirect. GitHub's Setup URL callback is hit directly
    by the browser with no Authorization header, so we can't rely on our
    normal auth middleware there — this state token is how we know which
    user just installed the app."""
    from datetime import datetime, timedelta
    payload = {
        "purpose": "github_app_install",
        "sub": user_id,
        "exp": datetime.utcnow() + timedelta(minutes=15),
    }
    token = jwt.encode(payload, get_jwt_secret(), algorithm=_STATE_ALGO)
    _dlog("github_app_state_created", user_id=user_id)
    return token


def _verify_install_state(state: str) -> Optional[str]:
    """Returns user_id if the state token is valid and unexpired, else None.
    Never raises — a bad/expired state degrades to 'installation unlinked',
    not a crash."""
    try:
        payload = jwt.decode(state, get_jwt_secret(), algorithms=[_STATE_ALGO])
        if payload.get("purpose") != "github_app_install":
            _dlog("github_app_state_wrong_purpose", purpose=payload.get("purpose"))
            return None
        user_id = payload.get("sub", "")
        _dlog("github_app_state_verified", user_id=user_id)
        return user_id or None
    except JWTError as e:
        _dlog("github_app_state_invalid", error=str(e))
        return None
    except Exception as e:
        _dlog("github_app_state_verify_error", error=str(e))
        return None


# ── Config / discovery ───────────────────────────────────────────────────────

@router.get("/config")
def github_app_config():
    """Public metadata the frontend needs to render the 'Connect GitHub
    (recommended)' button. No secrets included."""
    configured = is_github_app_configured()
    slug = get_app_slug()
    _dlog("github_app_config_requested", configured=configured, slug=slug)
    return {"enabled": configured, "app_slug": slug}


@router.get("/install-url")
def github_app_install_url(request: Request):
    """Returns the GitHub-hosted install page URL, with a signed state
    token embedded so the callback knows which user this install belongs
    to. Frontend does a full browser redirect to this URL — no credentials
    are ever typed by the user."""
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not is_github_app_configured():
        _dlog("github_app_install_url_not_configured", user_id=user_id)
        raise HTTPException(status_code=503, detail="GitHub App is not configured on this server yet.")
    state = _make_install_state(user_id)
    slug = get_app_slug()
    url = f"https://github.com/apps/{slug}/installations/new?state={state}"
    _dlog("github_app_install_url_issued", user_id=user_id, slug=slug)
    return {"url": url}


@router.get("/setup")
@router.get("/callback")
def github_app_callback(installation_id: str = "", setup_action: str = "", state: str = ""):
    """GitHub redirects the user's browser here after they finish the
    install flow. The App's configured 'Setup URL' is /setup (confirmed from
    live production logs); /callback is kept as an additional alias for the
    same handler in case anything else ever points there. No auth header is
    available on this request — identity comes only from the signed state
    token, never from the normal Bearer-token auth middleware."""
    _dlog("github_app_callback_hit", installation_id=installation_id, setup_action=setup_action, has_state=bool(state))

    if not installation_id:
        _dlog("github_app_callback_missing_installation_id", setup_action=setup_action)
        return RedirectResponse(url=f"{_FRONTEND_RETURN_URL}?github_app=error&reason=missing_installation_id")

    user_id = _verify_install_state(state)
    if not user_id:
        # No usable identity for this browser hit. For non-install actions
        # (e.g. setup_action=update from GitHub's app settings page, which
        # can arrive with no state at all) this is a benign bounce, not an
        # error — the existing link, if any, is untouched.
        if setup_action and setup_action != "install":
            _dlog("github_app_callback_update_no_state", setup_action=setup_action,
                  installation_id=installation_id)
            return RedirectResponse(url=f"{_FRONTEND_RETURN_URL}?github_app=updated")
        _dlog("github_app_callback_bad_state", installation_id=installation_id)
        return RedirectResponse(url=f"{_FRONTEND_RETURN_URL}?github_app=error&reason=invalid_or_expired_state")

    # We have a verified user AND an installation_id — link them regardless
    # of setup_action. GitHub sends setup_action=update (not "install") when
    # the app is already installed on the account, which happens whenever a
    # user re-runs the connect flow — e.g. after a failed first attempt. The
    # DB write is a safe upsert keyed on (user_id, installation_id), so
    # re-linking an already-linked install is a no-op refresh.
    try:
        info = get_installation_account_info(installation_id)
        save_github_app_installation(
            user_id=user_id,
            installation_id=installation_id,
            account_login=info.get("account_login", "unknown"),
        )
        _dlog("github_app_installation_linked", user_id=user_id, installation_id=installation_id,
              account_login=info.get("account_login"), setup_action=setup_action)
        return RedirectResponse(url=f"{_FRONTEND_RETURN_URL}?github_app=connected")
    except Exception as e:
        _dlog("github_app_callback_link_failed", user_id=user_id, installation_id=installation_id, error=str(e))
        return RedirectResponse(url=f"{_FRONTEND_RETURN_URL}?github_app=error&reason=link_failed")


# ── Status / tier / disconnect ───────────────────────────────────────────────

@router.get("/status")
def github_app_status(request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    installations = list_github_app_installations(user_id)
    _dlog("github_app_status_requested", user_id=user_id, count=len(installations))
    return {"installations": installations}


class TierRequest(BaseModel):
    installation_id: str
    tier: str


@router.post("/permission-tier")
def github_app_set_tier(body: TierRequest, request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if body.tier not in _VALID_TIERS:
        _dlog("github_app_set_tier_invalid", user_id=user_id, tier=body.tier)
        raise HTTPException(status_code=400, detail=f"tier must be one of {sorted(_VALID_TIERS)}")
    row = get_github_app_installation(user_id, body.installation_id)
    if not row:
        _dlog("github_app_set_tier_not_found", user_id=user_id, installation_id=body.installation_id)
        raise HTTPException(status_code=404, detail="Installation not found for this user.")
    set_github_app_permission_tier(user_id, body.installation_id, body.tier)
    _dlog("github_app_set_tier_ok", user_id=user_id, installation_id=body.installation_id, tier=body.tier)
    return {"ok": True, "tier": body.tier}


@router.delete("/{installation_id}")
def github_app_disconnect(installation_id: str, request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    delete_github_app_installation(user_id, installation_id)
    _dlog("github_app_disconnected", user_id=user_id, installation_id=installation_id)
    return {"ok": True, "note": "Local link removed. To fully revoke, also uninstall the app from your GitHub account settings."}


# ── Helpers shared by browse/load/commit ─────────────────────────────────────

def _require_tier(user_id: str, installation_id: str, needed: str) -> dict:
    """Enforce the permission tier BEFORE any write call. needed is one of
    'read' or 'write'. Raises 403 if the stored tier doesn't allow it.
    Read is always allowed at any tier; write requires tier == read_write."""
    row = get_github_app_installation(user_id, installation_id)
    if not row:
        _dlog("github_app_tier_check_not_found", user_id=user_id, installation_id=installation_id)
        raise HTTPException(status_code=404, detail="Installation not connected for this user.")
    tier = row.get("permission_tier", "read_only")
    if needed == "write" and tier != "read_write":
        _dlog("github_app_tier_check_denied", user_id=user_id, installation_id=installation_id,
              tier=tier, needed=needed)
        raise HTTPException(
            status_code=403,
            detail=f"This installation is set to '{tier}'. Switch it to 'read_write' in Settings to allow commits/PRs."
        )
    _dlog("github_app_tier_check_ok", user_id=user_id, installation_id=installation_id, tier=tier, needed=needed)
    return row


def _client_for(user_id: str, installation_id: str, needed: str = "read"):
    _require_tier(user_id, installation_id, needed)
    return get_installation_client(installation_id)


# ── Repos / branches / tree ──────────────────────────────────────────────────

@router.get("/repos")
def list_repos(request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    installations = list_github_app_installations(user_id)
    all_repos = []
    for inst in installations:
        installation_id = inst["installation_id"]
        try:
            # PyGithub 2.3.0: a plain Github installation client has NO
            # get_installation method. GithubIntegration.get_app_installation
            # returns an Installation that auto-swaps to installation auth,
            # so .get_repos() is correctly scoped (verified vs live API).
            _client_for(user_id, installation_id, "read")  # permission check only
            installation = get_integration().get_app_installation(int(installation_id))
            for repo in installation.get_repos():
                all_repos.append({
                    "id": repo.id,
                    "name": repo.name,
                    "full_name": repo.full_name,
                    "owner": repo.owner.login,
                    "private": repo.private,
                    "description": repo.description or "",
                    "default_branch": repo.default_branch,
                    "installation_id": installation_id,
                })
        except Exception as e:
            _dlog("github_app_list_repos_failed", user_id=user_id, installation_id=installation_id, error=str(e))
            continue
    _dlog("github_app_list_repos_ok", user_id=user_id, total=len(all_repos))
    return {"repos": all_repos}


def _find_installation_for_repo(user_id: str, owner: str, repo: str) -> str:
    """A user may have multiple installations (e.g. personal + org). Find
    the one that actually grants access to this owner/repo."""
    installations = list_github_app_installations(user_id)
    for inst in installations:
        installation_id = inst["installation_id"]
        try:
            g = get_installation_client(installation_id)
            g.get_repo(f"{owner}/{repo}")
            return installation_id
        except Exception:
            continue
    _dlog("github_app_repo_not_found_in_any_installation", user_id=user_id, owner=owner, repo=repo)
    raise HTTPException(status_code=404, detail=f"{owner}/{repo} is not accessible via any of your connected GitHub App installations.")


@router.get("/repos/{owner}/{repo}/branches")
def list_branches(owner: str, repo: str, request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    installation_id = _find_installation_for_repo(user_id, owner, repo)
    g = _client_for(user_id, installation_id, "read")
    try:
        r = g.get_repo(f"{owner}/{repo}")
        branches = [b.name for b in r.get_branches()]
        _dlog("github_app_list_branches_ok", owner=owner, repo=repo, count=len(branches))
        return {"branches": branches, "default": r.default_branch}
    except Exception as e:
        _dlog("github_app_list_branches_failed", owner=owner, repo=repo, error=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/repos/{owner}/{repo}/tree")
def get_tree(owner: str, repo: str, request: Request, branch: str = "main", path: str = ""):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    installation_id = _find_installation_for_repo(user_id, owner, repo)
    g = _client_for(user_id, installation_id, "read")
    try:
        r = g.get_repo(f"{owner}/{repo}")
        contents = r.get_contents(path or "", ref=branch)
        if not isinstance(contents, list):
            contents = [contents]
        items = []
        for item in sorted(contents, key=lambda x: (0 if x.type == "dir" else 1, x.name.lower())):
            items.append({
                "name": item.name, "path": item.path, "type": item.type,
                "size": item.size or 0, "sha": item.sha,
            })
        _dlog("github_app_get_tree_ok", owner=owner, repo=repo, path=path, count=len(items))
        return {"items": items, "path": path}
    except Exception as e:
        _dlog("github_app_get_tree_failed", owner=owner, repo=repo, path=path, error=str(e))
        raise HTTPException(status_code=400, detail=str(e))


# ── Load files into session ──────────────────────────────────────────────────

class LoadFilesRequest(BaseModel):
    session_id: str
    owner: str
    repo: str
    branch: str
    paths: List[str]


@router.post("/load")
def load_files(body: LoadFilesRequest, request: Request):
    from services.ast_parser import ASTParser

    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    installation_id = _find_installation_for_repo(user_id, body.owner, body.repo)
    g = _client_for(user_id, installation_id, "read")
    parser = ASTParser()

    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "javascriptreact", ".tsx": "typescriptreact",
        ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
        ".cpp": "cpp", ".c": "c", ".h": "c", ".hpp": "cpp",
        ".html": "html", ".css": "css", ".json": "json",
        ".md": "markdown", ".sh": "bash", ".sql": "sql",
        ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
        ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
    }

    try:
        r = g.get_repo(f"{body.owner}/{body.repo}")
        loaded, errors = [], []
        for path in body.paths:
            try:
                content_file = r.get_contents(path, ref=body.branch)
                raw = base64.b64decode(content_file.content).decode("utf-8", errors="replace")
                filename = content_file.name
                ext = Path(filename).suffix.lower()
                language = lang_map.get(ext, "plaintext")
                line_count = len(raw.splitlines())
                try:
                    smap = parser.parse(raw, filename)
                    symbol_count = len(smap.symbols)
                except Exception:
                    symbol_count = 0

                github_meta = json.dumps({
                    "owner": body.owner, "repo": body.repo, "branch": body.branch,
                    "path": path, "sha": content_file.sha, "source": "github_app",
                    "installation_id": installation_id,
                })

                with get_db_ctx() as conn:
                    existing = conn.execute(
                        "SELECT id FROM session_files WHERE session_id = ? AND filename = ?",
                        (body.session_id, filename)
                    ).fetchone()
                    if existing:
                        file_id = existing["id"] if hasattr(existing, "__getitem__") else existing[0]
                        conn.execute(
                            "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ?, github_meta = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (raw, language, line_count, symbol_count, github_meta, file_id)
                        )
                    else:
                        file_id = str(uuid.uuid4())
                        conn.execute(
                            "INSERT INTO session_files (id, session_id, filename, content, language, lines, symbol_count, file_type, github_meta, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'code', ?, CURRENT_TIMESTAMP)",
                            (file_id, body.session_id, filename, raw, language, line_count, symbol_count, github_meta)
                        )
                    conn.commit()

                loaded.append({
                    "id": file_id, "session_id": body.session_id, "filename": filename,
                    "language": language, "lines": line_count, "symbol_count": symbol_count,
                    "file_type": "code", "github_meta": github_meta,
                })
            except Exception as e:
                _dlog("github_app_load_file_failed", path=path, error=str(e))
                errors.append({"path": path, "error": str(e)})

        _dlog("github_app_load_ok", session_id=body.session_id, loaded=len(loaded), errors=len(errors))
        return {"loaded": loaded, "errors": errors, "truncated": False}
    except Exception as e:
        _dlog("github_app_load_failed", session_id=body.session_id, error=str(e))
        raise HTTPException(status_code=400, detail=str(e))


# ── Commit / PR (write tier only) ────────────────────────────────────────────

class CommitFile(BaseModel):
    session_file_id: str
    github_path: Optional[str] = None
    sha: Optional[str] = None


class CommitRequest(BaseModel):
    owner: str
    repo: str
    branch: str
    message: str
    files: List[CommitFile]
    create_pr: bool = False
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None
    new_branch: Optional[str] = None


@router.post("/commit")
def commit_changes(body: CommitRequest, request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    installation_id = _find_installation_for_repo(user_id, body.owner, body.repo)
    g = _client_for(user_id, installation_id, "write")  # tier-gated: must be read_write

    try:
        r = g.get_repo(f"{body.owner}/{body.repo}")
        target_branch = body.branch
        if body.create_pr and body.new_branch:
            source = r.get_branch(body.branch)
            r.create_git_ref(f"refs/heads/{body.new_branch}", source.commit.sha)
            target_branch = body.new_branch

        committed_files = []
        for f in body.files:
            with get_db_ctx() as conn:
                row = conn.execute(
                    "SELECT content, github_meta FROM session_files WHERE id = ?",
                    (f.session_file_id,)
                ).fetchone()
            if not row:
                continue
            content = row["content"] if hasattr(row, "__getitem__") else row[0]
            github_meta_str = row["github_meta"] if hasattr(row, "__getitem__") else row[1]
            github_meta = json.loads(github_meta_str) if github_meta_str else {}
            github_path = f.github_path or github_meta.get("path", "")
            sha = f.sha or github_meta.get("sha", "")
            if not github_path:
                continue
            try:
                result = r.update_file(
                    path=github_path, message=body.message,
                    content=content.encode("utf-8"), sha=sha, branch=target_branch,
                )
                new_sha = result["content"].sha if result.get("content") else sha
                updated_meta = {**github_meta, "sha": new_sha, "branch": target_branch}
                with get_db_ctx() as conn:
                    conn.execute(
                        "UPDATE session_files SET github_meta = ?, github_pushed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (json.dumps(updated_meta), f.session_file_id)
                    )
                    conn.commit()
                committed_files.append(github_path)
            except Exception as ge:
                _dlog("github_app_commit_file_failed", github_path=github_path, error=str(ge))
                raise HTTPException(status_code=400, detail=f"GitHub error on {github_path}: {str(ge)}")

        result_data = {
            "ok": True, "committed": committed_files, "branch": target_branch,
            "commit_url": f"https://github.com/{body.owner}/{body.repo}/commits/{target_branch}",
        }
        if body.create_pr and body.new_branch:
            pr = r.create_pull(
                title=body.pr_title or body.message,
                body=body.pr_body or f"Changes via SurgicalAI (GitHub App)\n\n{body.message}",
                head=target_branch, base=body.branch,
            )
            result_data["pr_url"] = pr.html_url
            result_data["pr_number"] = pr.number

        _dlog("github_app_commit_ok", owner=body.owner, repo=body.repo, files=len(committed_files))
        return result_data
    except HTTPException:
        raise
    except Exception as e:
        _dlog("github_app_commit_failed", owner=body.owner, repo=body.repo, error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
