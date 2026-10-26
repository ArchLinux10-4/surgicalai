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
from services.limits import MAX_SEARCH_TERMS, SEARCH_HITS_PER_FILE, SEARCH_MAX_HITS, READ_MAX_CHARS
from services.repositories import (
    detect_git,
    git_diff_text,
    git_status_text,
    repository_for_session,
    resolve_inside,
    _git,
)
from services.repo_apply import stage_path

_TOOLS = ("list_repo_files", "read_repo_file", "search_repo", "git_status", "git_diff")
_DIFF_FIELDS = ("scope", "base", "head", "view", "dots")
_DIFF_FIELD_MAX = 200
_MAX_LIST = 200
_MAX_HITS = SEARCH_MAX_HITS
_MAX_RETURN = READ_MAX_CHARS


def session_repo(session_id: str):
    from database import USE_POSTGRES
    if USE_POSTGRES:
        return None
    repo = repository_for_session(session_id)
    if not repo or repo.get("missing") or repo.get("removed_at"):
        return None
    return repo


def prompt_section(repo: dict) -> str:
    from services.agent_tool_spec import repo_text
    return repo_text("tag", repo)


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
    parsed = {
        "tool": tool,
        "path": str(data.get("path") or "").strip(),
        "terms": [str(t) for t in terms if str(t).strip()][:MAX_SEARCH_TERMS],
    }
    try:
        start_line = int(data.get("start_line") or 0)
    except (TypeError, ValueError):
        start_line = 0
    if start_line > 0:
        parsed["start_line"] = start_line
    # Only git_diff reads these, and only when the model actually sent them,
    # so the parsed shape of the other tools stays exactly as it was.
    if tool == "git_diff":
        for field in _DIFF_FIELDS:
            raw = data.get(field)
            if isinstance(raw, str) and raw.strip():
                parsed[field] = raw.strip()[:_DIFF_FIELD_MAX]
    return parsed


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


_MAX_TEXT_BYTES = 1_000_000
_BINARY_PROBE = 8192
_SEARCH_BYTE_BUDGET = 20 * 1024 * 1024
_SEARCH_SECONDS = 10
# One log must not fill the match list. Cursor's search skips generated
# data the same way and keeps going with the source matches it has.
_SEARCH_SKIP_SUFFIXES = {".jsonl", ".log", ".ndjson"}
_HITS_PER_FILE = SEARCH_HITS_PER_FILE
_SEARCH_CONTINUE = (
    "\nThese are the first matches, not every file. "
    "Read the file you need and write the edit."
)


def _unreadable_reason(path: Path) -> str:
    """Skip a file that is too large or binary. Empty string means it is text."""
    try:
        size = path.stat().st_size
    except OSError:
        return "unreadable"
    if size > _MAX_TEXT_BYTES:
        return "too_large"
    try:
        with path.open("rb") as handle:
            probe = handle.read(_BINARY_PROBE)
    except OSError:
        return "unreadable"
    if b"\x00" in probe:
        return "binary"
    return ""


def _search_skip(rel: str) -> bool:
    return Path(rel).suffix.lower() in _SEARCH_SKIP_SUFFIXES


def _take_hit(hits: list, per_file: dict, rel: str, line: str) -> bool:
    """Keep a match unless this file already has its share. True when full."""
    if per_file.get(rel, 0) >= _HITS_PER_FILE:
        return len(hits) >= _MAX_HITS
    per_file[rel] = per_file.get(rel, 0) + 1
    hits.append(line[:240])
    return len(hits) >= _MAX_HITS


def _search(root: str, terms: list, prefix: str = "") -> str:
    if not terms:
        return "search_repo needs a terms array."
    hits = []
    per_file = {}
    more = False
    prefix = (prefix or "").strip().strip("/")
    git = detect_git(root)
    if git["is_git"]:
        exclude = [f":(exclude)*{suffix}" for suffix in sorted(_SEARCH_SKIP_SUFFIXES)]
        scope = prefix or "."
        for term in terms:
            code, out, _err = _git(
                root,
                ["grep", "-n", "-I", "-F", "-e", term, "--", scope, *exclude],
                timeout=12,
            )
            if code not in (0, 1):
                continue
            for line in out.splitlines():
                rel = line.split(":", 1)[0]
                if _search_skip(rel):
                    continue
                if _take_hit(hits, per_file, rel, line):
                    more = True
                    break
            if more:
                break
    else:
        import time as _time
        started = _time.monotonic()
        scanned = 0
        lowered = [t.lower() for t in terms]
        root_real = os.path.realpath(root)
        walk_root = root_real
        if prefix:
            candidate = os.path.realpath(os.path.join(root_real, prefix))
            if candidate.startswith(root_real + os.sep) and os.path.isdir(candidate):
                walk_root = candidate
        for dirpath, dirnames, filenames in os.walk(walk_root):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in {".git", "node_modules", "__pycache__", "dist", "build", ".next", "venv", ".venv"}
                and not d.startswith(".")
            )
            for name in sorted(filenames):
                if _time.monotonic() - started > _SEARCH_SECONDS or scanned >= _SEARCH_BYTE_BUDGET:
                    more = True
                    break
                if _search_skip(name):
                    continue
                full = Path(dirpath) / name
                rel = os.path.relpath(str(full), root_real).replace(os.sep, "/")
                if prefix and rel != prefix and not rel.startswith(prefix + "/"):
                    continue
                try:
                    path = resolve_inside(root, rel)
                except HTTPException:
                    continue
                if _unreadable_reason(path):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                scanned += size
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), start=1):
                    if per_file.get(rel, 0) >= _HITS_PER_FILE:
                        break
                    if any(term in line.lower() for term in lowered):
                        if _take_hit(hits, per_file, rel, f"{rel}:{i}:{line[:200]}"):
                            more = True
                            break
                if more:
                    break
            if more:
                break
    if not hits and not more:
        return "No matches."
    text = "\n".join(hits) if hits else "No matches."
    if more:
        text += _SEARCH_CONTINUE
    return text


def _read_window(content: str, start_line: int):
    """A slice of a long file. The caller is told the next line to request."""
    lines = content.splitlines()
    total = len(lines)
    start = start_line if start_line and start_line > 0 else 1
    if total and start > total:
        start = total
    chosen = []
    size = 0
    end = start - 1
    for number, line in enumerate(lines[start - 1:], start=start):
        piece = line + "\n"
        if chosen and size + len(piece) > _MAX_RETURN:
            break
        chosen.append(line)
        size += len(piece)
        end = number
    continues = bool(total) and end < total
    return start, end, total, "\n".join(chosen), continues


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
            return git_status_text(root, session_id)
        if tool == "git_diff":
            return git_diff_text(
                root,
                scope=data.get("scope") or "",
                base=data.get("base") or "",
                head=data.get("head") or "",
                view=data.get("view") or "",
                path=data.get("path") or "",
                dots=data.get("dots") or "",
            )
        if tool == "list_repo_files":
            return _list_files(root, data.get("path") or "")
        if tool == "search_repo":
            return _search(root, data.get("terms") or [], data.get("path") or "")
        if tool == "read_repo_file":
            path = data.get("path") or ""
            if not path:
                return "read_repo_file needs a path."
            try:
                target = resolve_inside(root, path)
            except HTTPException:
                return "That path is outside the open folder."
            why = _unreadable_reason(target) if target.is_file() else "missing"
            if why == "too_large":
                return "That file is larger than 1 MB, so it was not opened."
            if why == "binary":
                return "That file looks binary, so it was not opened."
            if why == "unreadable":
                return "That file could not be read."
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
            start, end, total, body, continues = _read_window(content, int(data.get("start_line") or 1))
            span = f"lines {start}-{end} of {total}"
            more = ""
            if continues:
                more = (
                    f"\nFile continues at line {end + 1}. "
                    f"Call read_repo_file with start_line {end + 1}. "
                    "Then write the edit."
                )
            _dlog("repo_tool_read", session_id=session_id, filename=filename, start_line=start)
            return f"FILE: {filename} {span}\n{note}{more}\n```\n{body}\n```"
        return f"Unknown repo tool '{tool}'."
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return f"Repo tool failed: {detail}"
