"""GitHub integration router — PAT-based, per-user encrypted storage."""
import base64
import json
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

try:
    from github import Github, GithubException, Auth
    PYGITHUB_AVAILABLE = True
except ImportError:
    PYGITHUB_AVAILABLE = False

from database import get_user_api_key, set_user_api_key, get_db
from crypto_utils import encrypt_api_key, decrypt_api_key

router = APIRouter()


def _get_user_id(request: Request) -> str:
    return getattr(request.state, "user_id", "") or ""


def _get_github_client(user_id: str):
    if not PYGITHUB_AVAILABLE:
        raise HTTPException(status_code=500, detail="PyGithub not installed on server.")
    encrypted = get_user_api_key(user_id, "github")
    if not encrypted:
        raise HTTPException(
            status_code=401,
            detail="GitHub not connected. Add a Personal Access Token in Settings → GitHub."
        )
    pat = decrypt_api_key(encrypted)
    return Github(auth=Auth.Token(pat))


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def github_status(request: Request):
    user_id = _get_user_id(request)
    if not PYGITHUB_AVAILABLE:
        return {"connected": False, "error": "PyGithub not installed"}
    encrypted = get_user_api_key(user_id, "github")
    if not encrypted:
        return {"connected": False}
    try:
        pat = decrypt_api_key(encrypted)
        g = Github(auth=Auth.Token(pat))
        user = g.get_user()
        return {
            "connected": True,
            "username": user.login,
            "name": user.name or user.login,
            "avatar_url": user.avatar_url,
            "public_repos": user.public_repos,
        }
    except Exception:
        return {"connected": False}


# ── Connect / Disconnect ──────────────────────────────────────────────────────

class ConnectRequest(BaseModel):
    pat: str


@router.post("/connect")
def github_connect(body: ConnectRequest, request: Request):
    if not PYGITHUB_AVAILABLE:
        raise HTTPException(status_code=500, detail="PyGithub not installed on server.")
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        g = Github(auth=Auth.Token(body.pat.strip()))
        user = g.get_user()
        login = user.login
        avatar_url = user.avatar_url
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Bad credentials" in msg:
            raise HTTPException(status_code=401, detail="Invalid token. Make sure it has 'repo' scope.")
        raise HTTPException(status_code=500, detail=f"Connection failed: {msg}")

    encrypted = encrypt_api_key(body.pat.strip())
    set_user_api_key(user_id, "github", encrypted)
    return {"ok": True, "username": login, "avatar_url": avatar_url}


@router.delete("/disconnect")
def github_disconnect(request: Request):
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    set_user_api_key(user_id, "github", "")
    return {"ok": True}


# ── Repos ─────────────────────────────────────────────────────────────────────

@router.get("/repos")
def list_repos(request: Request):
    user_id = _get_user_id(request)
    g = _get_github_client(user_id)
    try:
        user = g.get_user()
        repos = []
        for repo in user.get_repos(sort="updated", direction="desc"):
            repos.append({
                "id": repo.id,
                "name": repo.name,
                "full_name": repo.full_name,
                "owner": repo.owner.login,
                "private": repo.private,
                "description": repo.description or "",
                "default_branch": repo.default_branch,
                "updated_at": repo.updated_at.isoformat() if repo.updated_at else None,
                "language": repo.language or "",
                "stars": repo.stargazers_count,
            })
            if len(repos) >= 100:
                break
        return {"repos": repos}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Branches ──────────────────────────────────────────────────────────────────

@router.get("/repos/{owner}/{repo}/branches")
def list_branches(owner: str, repo: str, request: Request):
    user_id = _get_user_id(request)
    g = _get_github_client(user_id)
    try:
        r = g.get_repo(f"{owner}/{repo}")
        branches = [b.name for b in r.get_branches()]
        return {"branches": branches, "default": r.default_branch}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── File Tree ─────────────────────────────────────────────────────────────────

@router.get("/repos/{owner}/{repo}/tree")
def get_tree(
    owner: str,
    repo: str,
    request: Request,
    branch: str = "main",
    path: str = "",
):
    user_id = _get_user_id(request)
    g = _get_github_client(user_id)
    try:
        r = g.get_repo(f"{owner}/{repo}")
        contents = r.get_contents(path or "", ref=branch)
        if not isinstance(contents, list):
            contents = [contents]
        items = []
        for item in sorted(contents, key=lambda x: (0 if x.type == "dir" else 1, x.name.lower())):
            items.append({
                "name": item.name,
                "path": item.path,
                "type": item.type,
                "size": item.size or 0,
                "sha": item.sha,
            })
        return {"items": items, "path": path}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Load Files into Session ───────────────────────────────────────────────────

class LoadFilesRequest(BaseModel):
    session_id: str
    owner: str
    repo: str
    branch: str
    paths: List[str]


@router.post("/load")
def load_files(body: LoadFilesRequest, request: Request):
    user_id = _get_user_id(request)
    g = _get_github_client(user_id)

    from services.ast_parser import ASTParser
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
        loaded = []
        errors = []

        for path in body.paths:
            try:
                content_file = r.get_contents(path, ref=body.branch)
                raw = base64.b64decode(content_file.content).decode("utf-8", errors="replace")
                filename = content_file.name
                ext = Path(filename).suffix.lower()
                language = lang_map.get(ext, "plaintext")
                lines = len(raw.splitlines())
                try:
                    smap = parser.parse(raw, filename)
                    symbol_count = len(smap.symbols)
                except Exception:
                    symbol_count = 0

                github_meta = json.dumps({
                    "owner": body.owner,
                    "repo": body.repo,
                    "branch": body.branch,
                    "path": path,
                    "sha": content_file.sha,
                })

                conn = get_db()
                existing = conn.execute(
                    "SELECT id FROM session_files WHERE session_id = ? AND filename = ?",
                    (body.session_id, filename)
                ).fetchone()

                if existing:
                    file_id = existing["id"] if hasattr(existing, "__getitem__") else existing[0]
                    conn.execute(
                        "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ?, github_meta = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (raw, language, lines, symbol_count, github_meta, file_id)
                    )
                else:
                    file_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO session_files (id, session_id, filename, content, language, lines, symbol_count, file_type, github_meta, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'code', ?, CURRENT_TIMESTAMP)",
                        (file_id, body.session_id, filename, raw, language, lines, symbol_count, github_meta)
                    )
                conn.commit()
                conn.close()

                loaded.append({
                    "id": file_id,
                    "session_id": body.session_id,
                    "filename": filename,
                    "language": language,
                    "lines": lines,
                    "symbol_count": symbol_count,
                    "file_type": "code",
                    "github_meta": github_meta,
                    "updated_at": None,
                    "created_at": None,
                })
            except Exception as e:
                errors.append({"path": path, "error": str(e)})

        return {"loaded": loaded, "errors": errors}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Commit Changes Back ───────────────────────────────────────────────────────

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
    g = _get_github_client(user_id)

    try:
        r = g.get_repo(f"{body.owner}/{body.repo}")

        target_branch = body.branch
        if body.create_pr and body.new_branch:
            source = r.get_branch(body.branch)
            r.create_git_ref(f"refs/heads/{body.new_branch}", source.commit.sha)
            target_branch = body.new_branch

        committed_files = []
        for f in body.files:
            conn = get_db()
            row = conn.execute(
                "SELECT content, github_meta FROM session_files WHERE id = ?",
                (f.session_file_id,)
            ).fetchone()
            conn.close()

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
                    path=github_path,
                    message=body.message,
                    content=content.encode("utf-8"),
                    sha=sha,
                    branch=target_branch,
                )
                new_sha = result["content"].sha if result.get("content") else sha
                updated_meta = {**github_meta, "sha": new_sha, "branch": target_branch}
                conn = get_db()
                conn.execute(
                    "UPDATE session_files SET github_meta = ?, github_pushed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (json.dumps(updated_meta), f.session_file_id)
                )
                conn.commit()
                conn.close()
                committed_files.append(github_path)
            except Exception as ge:
                raise HTTPException(
                    status_code=400,
                    detail=f"GitHub error on {github_path}: {str(ge)}"
                )

        result_data: dict = {
            "ok": True,
            "committed": committed_files,
            "branch": target_branch,
            "commit_url": f"https://github.com/{body.owner}/{body.repo}/commits/{target_branch}",
        }

        if body.create_pr and body.new_branch:
            pr = r.create_pull(
                title=body.pr_title or body.message,
                body=body.pr_body or f"Changes via SurgicalAI\n\n{body.message}",
                head=target_branch,
                base=body.branch,
            )
            result_data["pr_url"] = pr.html_url
            result_data["pr_number"] = pr.number

        return result_data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
