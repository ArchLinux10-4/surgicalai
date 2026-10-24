"""Local repositories: a folder the user opened, plus its git metadata.

Hosted deploys (USE_POSTGRES) never call this from the router. A local
install that sets DATABASE_URL is treated as hosted by that same signal
and does not get Open Folder. That matches Element Picker and MCP.
"""
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from database import (
    get_db_ctx,
    get_setting,
    list_github_app_installations,
    _dlog,
)

_REMOTE_RE = re.compile(
    r"^(?:"
    r"git@(?P<host>[^:]+):(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?"
    r"|https?://(?P<host2>[^/]+)/(?P<owner2>[^/]+)/(?P<repo2>[^/]+?)(?:\.git)?"
    r"|ssh://git@(?P<host3>[^/]+)/(?P<owner3>[^/]+)/(?P<repo3>[^/]+?)(?:\.git)?"
    r")$"
)
_SEEDED_SKIP = None  # filled lazily so tests can change home


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _cell(row, key, index=0):
    if row is None:
        return None
    if hasattr(row, "keys"):
        return row[key]
    return row[index]


def parse_git_remote(url: str):
    """Return {host, owner, repo} for https, ssh, and .git URLs, or None."""
    text = (url or "").strip().rstrip("/")
    if not text:
        return None
    match = _REMOTE_RE.match(text)
    if not match:
        return None
    host = match.group("host") or match.group("host2") or match.group("host3") or ""
    owner = match.group("owner") or match.group("owner2") or match.group("owner3") or ""
    repo = match.group("repo") or match.group("repo2") or match.group("repo3") or ""
    host = host.lower().strip()
    owner = owner.strip()
    repo = repo.strip()
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not host or not owner or not repo:
        return None
    return {"host": host, "owner": owner, "repo": repo}


def _git(root: str, args: list, timeout: int = 8):
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as exc:
        return 1, "", str(exc)


def detect_git(root: str) -> dict:
    """Branch, origin URL, and whether this folder has uncommitted changes."""
    info = {
        "is_git": False,
        "branch": "",
        "remote": "",
        "dirty": False,
        "toplevel": "",
    }
    code, out, _err = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or out != "true":
        return info
    info["is_git"] = True
    _c, top, _e = _git(root, ["rev-parse", "--show-toplevel"])
    info["toplevel"] = top
    _c, branch, _e = _git(root, ["branch", "--show-current"])
    info["branch"] = branch
    _c, remote, _e = _git(root, ["remote", "get-url", "origin"])
    info["remote"] = remote if _c == 0 else ""
    _c, porcelain, _e = _git(root, ["status", "--porcelain", "--", "."])
    info["dirty"] = bool(porcelain)
    return info


def _github_suggestion(user_id: str, remote: str):
    """Parsed github.com remote, and whether an installation login matches the owner."""
    parsed = parse_git_remote(remote)
    if not parsed or parsed["host"] not in ("github.com", "www.github.com"):
        return None, False
    linked = False
    try:
        installs = list_github_app_installations(user_id) or []
        logins = {(i.get("account_login") or "").lower() for i in installs}
        linked = parsed["owner"].lower() in logins
    except Exception:
        linked = False
    return {"owner": parsed["owner"], "repo": parsed["repo"]}, linked


def validate_directory(path: str) -> str:
    """realpath of a readable directory. Rejects the filesystem root."""
    raw = (path or "").strip()
    if not raw or "\x00" in raw:
        raise HTTPException(status_code=400, detail="Enter a folder path")
    if not os.path.isabs(raw):
        raw = os.path.abspath(raw)
    if not os.path.exists(raw):
        raise HTTPException(status_code=404, detail="Folder not found")
    if not os.path.isdir(raw):
        raise HTTPException(status_code=400, detail="That path is not a folder")
    real = os.path.realpath(raw)
    if real == os.path.realpath("/"):
        raise HTTPException(status_code=400, detail="The filesystem root cannot be opened")
    if not os.access(real, os.R_OK | os.X_OK):
        raise HTTPException(status_code=403, detail="That folder is not readable")
    return real


def resolve_inside(root: str, requested: str) -> Path:
    """Resolve requested under root. Blocks traversal and symlink escapes."""
    if requested is None:
        requested = ""
    if "\x00" in requested or "\x00" in (root or ""):
        raise HTTPException(status_code=400, detail="Invalid path")
    workspace = os.path.realpath(root)
    if os.path.isabs(requested):
        resolved = os.path.realpath(requested)
    else:
        resolved = os.path.realpath(os.path.join(workspace, requested))
    if resolved != workspace and not resolved.startswith(workspace + os.sep):
        raise HTTPException(status_code=403, detail="Access denied")
    return Path(resolved)


def _seeded_defaults() -> set:
    home = os.path.realpath(str(Path.home()))
    return {home, os.path.realpath("/tmp")}


def _public(row: dict, user_id: str) -> dict:
    root = row.get("root_path") or ""
    missing = not (root and os.path.isdir(root))
    git = detect_git(root) if not missing else {
        "is_git": False, "branch": row.get("default_branch") or "", "remote": row.get("git_remote_url") or "", "dirty": False,
    }
    suggestion, _linked = (None, False)
    if not missing and git.get("remote"):
        suggestion, _linked = _github_suggestion(user_id, git["remote"])
    elif row.get("git_remote_url"):
        suggestion, _linked = _github_suggestion(user_id, row.get("git_remote_url") or "")
    owner = (row.get("github_owner") or "").strip()
    repo = (row.get("github_repo") or "").strip()
    return {
        "id": row["id"],
        "name": row.get("name") or "",
        "root_path": root,
        "git_remote_url": row.get("git_remote_url") or git.get("remote") or "",
        "github_owner": owner,
        "github_repo": repo,
        "github_linked": bool(owner and repo),
        "default_branch": row.get("default_branch") or git.get("branch") or "",
        "auto_apply": bool(row.get("auto_apply")),
        "last_opened_at": row.get("last_opened_at"),
        "created_at": row.get("created_at"),
        "removed": bool(row.get("removed_at")),
        "missing": missing,
        "is_git": bool(git.get("is_git")),
        "dirty": bool(git.get("dirty")),
        "suggested_github": None if (owner and repo) else suggestion,
    }


def _load_row(conn, user_id: str, repo_id: str):
    return conn.execute(
        "SELECT * FROM repositories WHERE id = ? AND user_id = ?",
        (repo_id, user_id),
    ).fetchone()


def _as_dict(row) -> dict:
    return dict(row) if row is not None else {}


def get_owned(user_id: str, repo_id: str, include_removed: bool = False):
    if not user_id or not repo_id:
        return None
    with get_db_ctx() as conn:
        row = _load_row(conn, user_id, repo_id)
    if not row:
        return None
    data = _as_dict(row)
    if data.get("removed_at") and not include_removed:
        return None
    return data


def require_root(user_id: str, repo_id: str) -> str:
    row = get_owned(user_id, repo_id)
    if not row:
        raise HTTPException(status_code=404, detail="Repository not found")
    root = row.get("root_path") or ""
    if not root or not os.path.isdir(root):
        raise HTTPException(status_code=404, detail="Folder not found. Relink it from the sidebar.")
    return os.path.realpath(root)


def repository_for_session(session_id: str):
    """Active repository for a chat, or None. Never raises."""
    from database import USE_POSTGRES as hosted
    if hosted or not session_id:
        return None
    try:
        with get_db_ctx() as conn:
            row = conn.execute(
                """SELECT r.* FROM chat_sessions s
                   JOIN repositories r ON r.id = s.repository_id
                   WHERE s.id = ? AND r.removed_at IS NULL""",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        data = _as_dict(row)
        if not data.get("root_path") or not os.path.isdir(data["root_path"]):
            data["missing"] = True
        return data
    except Exception:
        return None


def preview_path(user_id: str, path: str) -> dict:
    real = validate_directory(path)
    git = detect_git(real)
    suggestion, linked = _github_suggestion(user_id, git.get("remote") or "")
    return {
        "path": real,
        "name": Path(real).name or real,
        "is_git": git["is_git"],
        "branch": git["branch"],
        "remote": git["remote"],
        "dirty": git["dirty"],
        "suggested_github": suggestion,
        "github_match": linked,
    }


def _upsert(user_id: str, real: str, name: str | None) -> dict:
    git = detect_git(real)
    suggestion, linked = _github_suggestion(user_id, git.get("remote") or "")
    owner = suggestion["owner"] if linked and suggestion else None
    repo = suggestion["repo"] if linked and suggestion else None
    display = (name or "").strip() or Path(real).name or real
    display = display[:120]
    now = _now()
    with get_db_ctx() as conn:
        existing = conn.execute(
            "SELECT * FROM repositories WHERE user_id = ? AND root_path = ?",
            (user_id, real),
        ).fetchone()
        if existing:
            data = _as_dict(existing)
            # Re-opening keeps a rename and an explicit link or unlink.
            # Auto-link only happens the first time the folder is registered.
            keep_name = data.get("name") if not (name or "").strip() else display
            conn.execute(
                """UPDATE repositories
                   SET name = ?, git_remote_url = ?, default_branch = ?,
                       removed_at = NULL, last_opened_at = ?
                   WHERE id = ?""",
                (
                    keep_name,
                    git.get("remote") or data.get("git_remote_url") or None,
                    git.get("branch") or data.get("default_branch") or None,
                    now,
                    data["id"],
                ),
            )
            conn.commit()
            row = _load_row(conn, user_id, data["id"])
        else:
            repo_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO repositories
                   (id, user_id, name, root_path, git_remote_url, github_owner, github_repo,
                    default_branch, auto_apply, last_opened_at, created_at, removed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)""",
                (
                    repo_id, user_id, display, real, git.get("remote") or None,
                    owner, repo, git.get("branch") or None, now, now,
                ),
            )
            conn.commit()
            row = _load_row(conn, user_id, repo_id)
    _dlog("repository_opened", user_id=user_id, path=real)
    return _public(_as_dict(row), user_id)


def open_repository(user_id: str, path: str, name: str | None = None) -> dict:
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    real = validate_directory(path)
    return _upsert(user_id, real, name)


def ensure_legacy_workspace(user_id: str) -> None:
    """Register a custom workspace_path once, so an existing folder is not lost.

    The installer seeds workspace_path to the home directory (and hosted
    seeds /tmp). Those defaults are not a project, so they are not opened
    automatically. A path the user actually set is.
    """
    from database import USE_POSTGRES
    if not user_id or USE_POSTGRES:
        return
    raw = (get_setting("workspace_path", "") or "").strip()
    if not raw or not os.path.isdir(raw):
        return
    try:
        real = os.path.realpath(raw)
    except Exception:
        return
    if real in _seeded_defaults() or real == os.path.realpath("/"):
        return
    try:
        _upsert(user_id, real, None)
    except HTTPException:
        return
    except Exception as exc:
        _dlog("repository_legacy_skip", user_id=user_id, error=str(exc)[:200])


def list_repositories(user_id: str, include_removed: bool = False) -> list:
    if not user_id:
        return []
    ensure_legacy_workspace(user_id)
    sql = "SELECT * FROM repositories WHERE user_id = ?"
    if not include_removed:
        sql += " AND removed_at IS NULL"
    sql += " ORDER BY removed_at IS NOT NULL, last_opened_at DESC"
    with get_db_ctx() as conn:
        rows = conn.execute(sql, (user_id,)).fetchall()
    return [_public(_as_dict(r), user_id) for r in rows]


def update_repository(user_id: str, repo_id: str, fields: dict) -> dict:
    row = get_owned(user_id, repo_id, include_removed=True)
    if not row:
        raise HTTPException(status_code=404, detail="Repository not found")
    name = row.get("name")
    owner = row.get("github_owner")
    repo = row.get("github_repo")
    auto = 1 if row.get("auto_apply") else 0
    removed_at = row.get("removed_at")
    if fields.get("restore"):
        removed_at = None
    if "name" in fields and fields["name"] is not None:
        name = str(fields["name"]).strip()[:120] or name
    if fields.get("unlink_github"):
        owner, repo = None, None
    if fields.get("github_owner") is not None or fields.get("github_repo") is not None:
        owner = str(fields.get("github_owner") or "").strip()
        repo = str(fields.get("github_repo") or "").strip()
        if not owner and not repo:
            owner, repo = None, None
        else:
            from services.github_natural_tag import validate_github_target
            branch = (row.get("default_branch") or "main").strip() or "main"
            err = validate_github_target(owner, repo, branch)
            if err:
                raise HTTPException(status_code=400, detail=err)
    if "auto_apply" in fields and fields["auto_apply"] is not None:
        auto = 1 if fields["auto_apply"] else 0
    with get_db_ctx() as conn:
        conn.execute(
            """UPDATE repositories
               SET name = ?, github_owner = ?, github_repo = ?, auto_apply = ?,
                   removed_at = ?, last_opened_at = ?
               WHERE id = ? AND user_id = ?""",
            (name, owner, repo, auto, removed_at, _now(), repo_id, user_id),
        )
        conn.commit()
        updated = _load_row(conn, user_id, repo_id)
    return _public(_as_dict(updated), user_id)


def remove_repository(user_id: str, repo_id: str) -> dict:
    """Hide the folder from the sidebar. Chats and files on disk stay."""
    row = get_owned(user_id, repo_id, include_removed=True)
    if not row:
        raise HTTPException(status_code=404, detail="Repository not found")
    with get_db_ctx() as conn:
        conn.execute(
            "UPDATE repositories SET removed_at = ? WHERE id = ? AND user_id = ?",
            (_now(), repo_id, user_id),
        )
        conn.commit()
    _dlog("repository_removed", user_id=user_id, repo_id=repo_id)
    return {"ok": True}


def list_directories(path: str) -> dict:
    """In-app folder browser. Local installs only; the router is 404 when hosted."""
    raw = (path or "").strip()
    if not raw:
        raw = os.environ.get("SURGICALAI_BROWSE_ROOT") or str(Path.home())
    real = validate_directory(raw)
    parent = str(Path(real).parent)
    if os.path.realpath(parent) == real:
        parent = ""
    entries = []
    try:
        names = sorted(os.listdir(real), key=str.lower)
    except OSError as exc:
        raise HTTPException(status_code=403, detail="That folder is not readable") from exc
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(real, name)
        if os.path.isdir(full):
            entries.append({"name": name, "path": os.path.realpath(full)})
        if len(entries) >= 200:
            break
    return {"path": real, "parent": parent, "entries": entries}


def assert_git_path_allowed(user_id: str, repo_path: str) -> str:
    """Allow git only inside a registered repo or the configured workspace.

    The file browser's workspace_path stays allowed so the existing git
    panel keeps working for files opened from that folder.
    """
    if not repo_path or "\x00" in repo_path:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.exists(repo_path):
        raise HTTPException(status_code=404, detail="Path not found")
    resolved = os.path.realpath(repo_path)
    roots = []
    if user_id:
        with get_db_ctx() as conn:
            rows = conn.execute(
                "SELECT root_path FROM repositories WHERE user_id = ? AND removed_at IS NULL",
                (user_id,),
            ).fetchall()
        for row in rows:
            root = _cell(row, "root_path", 0) or ""
            if root:
                roots.append(os.path.realpath(root))
    workspace = (get_setting("workspace_path", "") or "").strip()
    if workspace and os.path.isdir(workspace):
        roots.append(os.path.realpath(workspace))
    for root in roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    raise HTTPException(status_code=403, detail="Access denied")


def git_status_text(root: str) -> str:
    git = detect_git(root)
    if not git["is_git"]:
        return "This folder is not a git repository."
    lines = [f"Branch: {git['branch'] or '(detached)'}"]
    if git["remote"]:
        lines.append(f"Remote: {git['remote']}")
    _c, porcelain, _e = _git(root, ["status", "--porcelain", "--", "."])
    changes = [ln for ln in porcelain.splitlines() if ln.strip()]
    if not changes:
        lines.append("Working tree clean.")
    else:
        lines.append(f"{len(changes)} uncommitted path(s):")
        lines.extend(changes[:40])
        if len(changes) > 40:
            lines.append(f"... {len(changes) - 40} more")
    return "\n".join(lines)
