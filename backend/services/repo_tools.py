"""Read tools for a chat that has a local repository open.

The model asks with <repo_request>{"tool":"read_repo_file","path":"..."}</repo_request>
on Claude, GPT, and Gemini, and with the request_repo function on Grok.
A read is copied into session_files so a later edit stays staged until Apply.
Offline mode does not get these tools.
"""
import json
import os
from pathlib import Path

from database import _dlog
from services.repositories import (
    detect_git,
    git_status_text,
    repository_for_session,
    resolve_inside,
    _git,
)
from services.repo_apply import stage_path

_TOOLS = ("list_repo_files", "read_repo_file", "search_repo", "git_status")
_MAX_LIST = 200
_MAX_HITS = 40
_MAX_RETURN = 80_000


def session_repo(session_id: str):
    from database import USE_POSTGRES
    if USE_POSTGRES:
        return None
    repo = repository_for_session(session_id)
    if not repo or repo.get("missing") or repo.get("removed_at"):
        return None
    return repo


def prompt_section(repo: dict) -> str:
    name = repo.get("name") or "repository"
    root = repo.get("root_path") or ""
    return (
        f"You can read the open local repository \"{name}\" at {root}.\n"
        '<repo_request>{"tool":"list_repo_files"}</repo_request>\n'
        '<repo_request>{"tool":"read_repo_file","path":"src/app.py"}</repo_request>\n'
        '<repo_request>{"tool":"search_repo","terms":["symbol"]}</repo_request>\n'
        '<repo_request>{"tool":"git_status"}</repo_request>\n'
        "read_repo_file loads the file into this chat. Edits stay staged until "
        "the user applies them to disk. You cannot run shell commands and you "
        "cannot write to disk."
    )


def parse_repo_request(raw: str):
    try:
        data = json.loads(raw or "")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    tool = str(data.get("tool") or "").strip()
    if tool not in _TOOLS:
        return {"_error": f"Unknown repo tool '{tool}'. Use one of: {', '.join(_TOOLS)}."}
    terms = data.get("terms") or []
    if isinstance(terms, str):
        terms = [terms]
    if not isinstance(terms, list):
        terms = []
    return {
        "tool": tool,
        "path": str(data.get("path") or "").strip(),
        "terms": [str(t) for t in terms if str(t).strip()][:8],
    }


def _ignored_walk(root: str, prefix: str):
    from routers.files import IGNORED_DIRS, IGNORED_EXTS
    base = resolve_inside(root, prefix) if prefix else Path(os.path.realpath(root))
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if Path(name).suffix.lower() in IGNORED_EXTS:
                continue
            full = Path(dirpath) / name
            rel = str(full.relative_to(os.path.realpath(root))).replace(os.sep, "/")
            found.append(rel)
            if len(found) >= _MAX_LIST:
                return found
    return found


def _list_files(root: str, prefix: str) -> str:
    code, out, _err = _git(root, ["ls-files", "--", prefix or "."])
    if code == 0 and out:
        lines = [ln for ln in out.splitlines() if ln.strip()]
    else:
        lines = _ignored_walk(root, prefix)
    if prefix:
        lines = [ln for ln in lines if ln.startswith(prefix.rstrip("/") + "/") or ln == prefix]
    shown = lines[:_MAX_LIST]
    if not shown:
        return "No files found."
    text = "\n".join(shown)
    if len(lines) > _MAX_LIST:
        text += f"\n... {len(lines) - _MAX_LIST} more"
    return text


def _search(root: str, terms: list) -> str:
    if not terms:
        return "search_repo needs a terms array."
    hits = []
    git = detect_git(root)
    if git["is_git"]:
        for term in terms:
            code, out, _err = _git(root, ["grep", "-n", "-I", "-F", "-e", term, "--", "."], timeout=12)
            if code not in (0, 1):
                continue
            for line in out.splitlines():
                hits.append(line[:240])
                if len(hits) >= _MAX_HITS:
                    break
            if len(hits) >= _MAX_HITS:
                break
    else:
        files = _ignored_walk(root, "")
        lowered = [t.lower() for t in terms]
        for rel in files:
            try:
                text = resolve_inside(root, rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                low = line.lower()
                if any(term in low for term in lowered):
                    hits.append(f"{rel}:{i}:{line[:200]}")
                if len(hits) >= _MAX_HITS:
                    break
            if len(hits) >= _MAX_HITS:
                break
    if not hits:
        return "No matches."
    return "\n".join(hits)


def execute_repo_tool(session_id: str, user_id: str, data: dict, session_files=None, content_lookup=None) -> str:
    """Run one repo tool. Always returns text. Never raises."""
    try:
        if not isinstance(data, dict):
            return "Repo request was not a JSON object."
        if data.get("_error"):
            return str(data["_error"])
        if data.get("_invalid"):
            return "The <repo_request> body was not valid JSON. Use {\"tool\":\"git_status\"}."
        repo = session_repo(session_id)
        if not repo:
            return "This chat has no open folder."
        if user_id and repo.get("user_id") and repo["user_id"] != user_id:
            return "This folder belongs to a different account."
        tool = data.get("tool") or ""
        root = repo["root_path"]
        if tool == "git_status":
            return git_status_text(root)
        if tool == "list_repo_files":
            return _list_files(root, data.get("path") or "")
        if tool == "search_repo":
            return _search(root, data.get("terms") or [])
        if tool == "read_repo_file":
            path = data.get("path") or ""
            if not path:
                return "read_repo_file needs a path."
            staged = stage_path(session_id, user_id or repo.get("user_id") or "", repo["id"], path)
            content = staged.get("content") or ""
            filename = staged["filename"]
            entry = {
                "id": staged["id"],
                "filename": filename,
                "content": content,
                "repo_path": filename,
                "base_sha": staged.get("base_sha") or "",
                "language": "",
                "file_type": "code",
            }
            if isinstance(session_files, list):
                session_files[:] = [sf for sf in session_files if sf.get("filename") != filename]
                session_files.append(entry)
            if isinstance(content_lookup, dict):
                content_lookup[filename] = content
            note = "Loaded into this chat."
            if staged.get("kept_edits"):
                note = "This chat already has edits to this file, so the disk copy was not reloaded."
            body = content if len(content) <= _MAX_RETURN else content[:_MAX_RETURN] + "\n... [truncated]"
            _dlog("repo_tool_read", session_id=session_id, filename=filename)
            return f"FILE: {filename}\n{note}\n```\n{body}\n```"
        return f"Unknown repo tool '{tool}'."
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return f"Repo tool failed: {detail}"
