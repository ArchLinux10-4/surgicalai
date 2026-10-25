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


def _git_raw(root: str, args: list, timeout: int = 20):
    """Like _git, but keeps stdout intact. Diffs need their blank lines and a
    longer timeout than the 8 seconds _git allows. Never raises."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", (proc.stderr or "").strip()
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
        "branch": git.get("branch") or "",
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


# Refs the model may name. Allow-list, so a value can never smuggle a flag
# or a shell metacharacter. ".." is excluded on purpose: a range is built
# here from base and head, never taken as one string.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._\-~^@{}]*$")

# A patch with no path is only returned whole when it is this short.
# Anything bigger comes back as a stat plus a note to name the file.
_PATCH_LINE_CAP = 200
_DIFF_LINE_CAP = 400

_DIFF_SCOPES = ("unstaged", "staged", "worktree", "range")
_DIFF_VIEWS = ("stat", "patch")

# Stops a user's git config from coloring output, running an external diff,
# or applying a textconv filter. quotepath keeps non-ASCII names readable.
_DIFF_CONFIG = [
    "-c", "core.quotepath=false",
    "-c", "diff.external=",
    # Without a hint, git prints NUL bytes from an untracked file as text.
    "-c", "diff.attributes.binary=true",
    "--no-pager",
]


def _safe_ref(value):
    """(ref, error). Empty means the caller did not give one."""
    if value is None:
        return "", ""
    text = str(value)
    if text != text.strip() or not text:
        return "", "" if not text.strip() else "A ref cannot have spaces or newlines."
    if len(text) > 200 or ".." in text or not _REF_RE.match(text):
        return "", f"'{text[:60]}' is not a valid git ref."
    return text, ""


def _ref_exists(root: str, ref: str) -> bool:
    code, _out, _err = _git(root, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
    return code == 0


def _resolve_base(root: str):
    """The ref local HEAD is compared against. Never fetches.

    origin/main first, then whatever origin/HEAD points at, then main.
    None when the folder has none of them.
    """
    if _ref_exists(root, "origin/main"):
        return "origin/main"
    code, out, _err = _git(root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if code == 0 and out and _ref_exists(root, out):
        return out
    if _ref_exists(root, "main"):
        return "main"
    return None


def _diff_args(root: str, git_args: list, rel_path: str):
    """git_args is the diff verb and its flags. A path goes after --."""
    args = [*_DIFF_CONFIG, *git_args]
    if rel_path:
        args += ["--", rel_path]
    return _git_raw(root, args)


def _cap_lines(text: str, limit: int, footer: str) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.rstrip("\n")
    kept = "\n".join(lines[:limit])
    return f"{kept}\n{footer.format(n=limit)}"


def _rel_inside(root: str, path: str):
    """(relative posix path, error). Empty path means the whole folder."""
    if not path:
        return "", ""
    try:
        resolved = resolve_inside(root, path)
    except HTTPException as exc:
        return "", str(exc.detail or "Access denied")
    if not resolved.exists():
        return "", f"No file named '{path}' in this folder."
    real_root = os.path.realpath(root)
    if os.path.realpath(str(resolved)) == real_root:
        return "", ""
    rel = str(resolved.relative_to(real_root)).replace(os.sep, "/")
    return rel, ""


def _intent_to_add(root: str, rel_path: str):
    """Untracked files are invisible to git diff. Mark them intent-to-add for
    this one command so they show up, then put the index back.

    Returns the paths that were marked, so the caller can unmark them.
    """
    code, out, _err = _git(root, ["ls-files", "--others", "--exclude-standard", "--", rel_path or "."])
    rows = [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []
    marked = []
    for name in rows:
        add_code, _o, _e = _git(root, ["add", "-N", "--", name])
        if add_code == 0:
            marked.append(name)
    return marked


def _clear_intent(root: str, marked: list) -> None:
    if marked:
        _git(root, ["reset", "-q", "--"] + marked)


def _section(root: str, heading: str, git_args: list, rel_path: str, view: str, empty: str) -> str:
    code, out, err = _diff_args(root, git_args, rel_path)
    body = out.strip("\n")
    if code != 0:
        detail = err or "git diff failed."
        return f"{heading}\n{detail}"
    if not body.strip():
        return f"{heading}\n{empty}"
    if view == "patch":
        body = _cap_lines(
            body, _DIFF_LINE_CAP,
            "Output cut at {n} lines. Narrow with path, or use view stat for the file list.",
        )
    return f"{heading}\n{body}"


def git_diff_text(root: str, scope: str = "", base: str = "", head: str = "",
                  view: str = "", path: str = "", dots: str = "") -> str:
    """Read-only diff of the open folder. Always returns text, never raises.

    No scope and no refs: committed changes on HEAD since it split from the
    repo base (three-dot), then uncommitted changes versus HEAD. The base is
    origin/main, else origin's default branch, else main. Nothing is fetched.
    """
    try:
        return _git_diff_text(root, scope, base, head, view, path, dots)
    except Exception as exc:
        return f"git diff failed: {exc}"


def _git_diff_text(root: str, scope: str, base: str, head: str, view: str, path: str, dots: str) -> str:
    git = detect_git(root)
    if not git["is_git"]:
        return "This folder is not a git repository."

    scope = (scope or "").strip().lower()
    view = (view or "").strip().lower() or "stat"
    if scope and scope not in _DIFF_SCOPES:
        return f"Unknown diff scope '{scope}'. Use one of: {', '.join(_DIFF_SCOPES)}."
    if view not in _DIFF_VIEWS:
        return "Unknown diff view. Use stat or patch."
    if dots and dots not in ("two", "three"):
        return "Unknown diff dots. Use two or three."

    rel_path, path_err = _rel_inside(root, (path or "").strip())
    if path_err:
        return path_err

    base_ref, base_err = _safe_ref(base)
    if base_err:
        return base_err
    head_ref, head_err = _safe_ref(head)
    if head_err:
        return head_err

    if scope in ("unstaged", "staged", "worktree"):
        return _scoped_diff(root, scope, view, rel_path)

    if scope == "range" or base_ref or head_ref:
        return _range_diff(root, base_ref, head_ref, view, rel_path, dots or "three")

    return _default_diff(root, view, rel_path)


def _stat_or_patch(view: str, rel_path: str):
    """Flags for one diff, and whether the patch is being withheld for size."""
    if view == "stat":
        return ["--stat=200,200", "--no-color"], False
    if rel_path:
        return ["--no-color", "--no-ext-diff", "--no-textconv"], False
    return ["--no-color", "--no-ext-diff", "--no-textconv"], True


def _patch_gate(root: str, git_args: list, rel_path: str, heading: str) -> str:
    """A patch with no path comes back whole only while it stays short."""
    code, out, err = _diff_args(root, git_args, "")
    if code != 0:
        return ""
    if len(out.splitlines()) <= _PATCH_LINE_CAP:
        return ""
    stat_code, stat_out, _stat_err = _diff_args(
        root, ["diff", "--stat=200,200", "--no-color", *git_args[1:]], "")
    listing = stat_out.strip("\n") if stat_code == 0 else ""
    note = "That patch is long. Pass path to see one file."
    return f"{heading}\n{listing}\n{note}".rstrip()


def _scoped_diff(root: str, scope: str, view: str, rel_path: str) -> str:
    flags, gated = _stat_or_patch(view, rel_path)
    if scope == "unstaged":
        label, extra, empty = "Unstaged changes (not yet staged)", [], "No unstaged changes."
    elif scope == "staged":
        label, extra, empty = "Staged changes (in the index)", ["--cached"], "No staged changes."
    else:
        label, extra, empty = "Uncommitted changes versus HEAD", ["HEAD"], "No uncommitted changes."
    # Staged changes are already in the index. The other two scopes also
    # cover files that have never been committed, which git diff hides.
    marked = [] if scope == "staged" else _intent_to_add(root, rel_path)
    try:
        heading = f"{label} ({view})."
        if gated:
            withheld = _patch_gate(root, ["diff", *extra, *flags], rel_path, heading)
            if withheld:
                return withheld
        return _section(root, heading, ["diff", *extra, *flags], rel_path, view, empty)
    finally:
        _clear_intent(root, marked)


def _range_diff(root: str, base_ref: str, head_ref: str, view: str, rel_path: str, dots: str) -> str:
    if not base_ref:
        base_ref = _resolve_base(root) or ""
    if not base_ref:
        return "No base ref to compare against. This folder has no origin/main and no main."
    head_ref = head_ref or "HEAD"
    for ref in (base_ref, head_ref):
        if ref != "HEAD" and not _ref_exists(root, ref):
            return f"No ref named '{ref}' in this folder. The remote was not fetched."
    joiner = "..." if dots == "three" else ".."
    kind = "since they diverged" if dots == "three" else "between the two tips"
    heading = f"Changes {kind}: {base_ref}{joiner}{head_ref} ({view}). The remote was not fetched."
    flags, gated = _stat_or_patch(view, rel_path)
    spec = f"{base_ref}{joiner}{head_ref}"
    if gated:
        withheld = _patch_gate(root, ["diff", spec, *flags], rel_path, heading)
        if withheld:
            return withheld
    return _section(root, heading, ["diff", spec, *flags], rel_path, view, "No changes in that range.")


def _default_diff(root: str, view: str, rel_path: str) -> str:
    base = _resolve_base(root)
    flags = ["--stat=200,200", "--no-color"] if view == "stat" else ["--no-color", "--no-ext-diff", "--no-textconv"]
    parts = []

    code, head_out, _err = _git(root, ["rev-parse", "--verify", "--quiet", "HEAD"])
    has_head = code == 0 and bool(head_out)

    if base and has_head:
        heading = (
            f"Local HEAD since it split from {base} ({view}). "
            "The remote was not fetched, so this is the origin already stored in this folder."
        )
        if view == "patch" and not rel_path:
            withheld = _patch_gate(root, ["diff", f"{base}...HEAD", *flags], "", heading)
            parts.append(withheld or _section(
                root, heading, ["diff", f"{base}...HEAD", *flags], "", view,
                "No committed changes since the base."))
        else:
            parts.append(_section(
                root, heading, ["diff", f"{base}...HEAD", *flags], rel_path, view,
                "No committed changes since the base."))
    elif not has_head:
        parts.append("This folder has no commits yet, so there is nothing to compare against a base.")
    else:
        parts.append(
            "No base ref found. This folder has no origin/main and no main, "
            "so committed changes cannot be compared. The remote was not fetched."
        )

    if has_head:
        heading = f"Uncommitted changes versus HEAD ({view})."
        marked = _intent_to_add(root, rel_path)
        try:
            if view == "patch" and not rel_path:
                withheld = _patch_gate(root, ["diff", "HEAD", *flags], "", heading)
                parts.append(withheld or _section(
                    root, heading, ["diff", "HEAD", *flags], "", view, "No uncommitted changes."))
            else:
                parts.append(_section(
                    root, heading, ["diff", "HEAD", *flags], rel_path, view, "No uncommitted changes."))
        finally:
            _clear_intent(root, marked)
    return "\n\n".join(parts)


def git_status_text(root: str, session_id: str = "") -> str:
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
    if session_id:
        from services.git_cli import unapplied_edits_note
        note = unapplied_edits_note(session_id, root)
        if note:
            lines.append(note)
    return "\n".join(lines)
