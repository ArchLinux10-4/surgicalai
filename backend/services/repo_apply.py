"""Stage repository files in the chat, then write them to disk on Apply.

Nothing in this module writes to disk except apply_files and the undo
sync, and both compare the file's hash to the hash captured when it was
loaded. A change on disk since then is a conflict, not a silent overwrite.
"""
import hashlib
import os
import uuid
from pathlib import Path

from fastapi import HTTPException

from database import get_db_ctx, _dlog
from services.repositories import get_owned, repository_for_session, resolve_inside


def content_sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def disk_state_for(repo_path: str, root: str, content: str, base_sha: str) -> str:
    """pending, applied, conflict, or missing."""
    if not repo_path or not root:
        return ""
    try:
        target = resolve_inside(root, repo_path)
    except HTTPException:
        return "conflict"
    if not target.exists():
        if not base_sha:
            return "pending"
        return "missing"
    if not target.is_file():
        return "conflict"
    try:
        disk_sha = content_sha(_read_text(target))
    except OSError:
        return "conflict"
    staged = content_sha(content or "")
    if (base_sha or "") and disk_sha != base_sha:
        return "conflict"
    if staged == disk_sha:
        return "applied"
    return "pending"


def annotate_disk_state(session_id: str, rows: list) -> list:
    """Add disk_state. Drop file content if the caller only wanted metadata."""
    repo = repository_for_session(session_id)
    root = (repo or {}).get("root_path") or ""
    out = []
    for row in rows:
        item = dict(row)
        content = item.get("content")
        rel = item.get("repo_path") or ""
        if rel and root:
            item["disk_state"] = disk_state_for(rel, root, content if content is not None else "", item.get("base_sha") or "")
        else:
            item["disk_state"] = None
        out.append(item)
    return out


def _language(filename: str) -> str:
    from routers.session_files import _get_language
    return _get_language(filename)


_PLAN_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".surgicalai_backups",
    "dist", "build", ".next", "venv", ".venv", "target",
}


def _planned_rel(root: str, filename: str) -> str | None:
    """A file under root for this plan step, or None when it is not there.

    An exact relative path wins. A bare name is used only when exactly one
    file in the folder has that name. The walk skips dependency and build
    directories and stops after a few thousand names so a huge folder cannot
    stall the request.
    """
    rel = (filename or "").replace("\\", "/").strip().lstrip("/")
    if not rel or any(part == ".." for part in rel.split("/")):
        return None
    try:
        target = resolve_inside(root, rel)
    except HTTPException:
        return None
    if target.is_file():
        return rel
    if "/" in rel:
        return None
    found: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _PLAN_SKIP_DIRS and not d.startswith(".")
        ]
        for name in filenames:
            seen += 1
            if seen > 8000:
                return found[0] if len(found) == 1 else None
            if name != rel:
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.isfile(full) and os.path.getsize(full) <= 1_000_000:
                    found.append(os.path.relpath(full, root))
            except OSError:
                continue
            if len(found) > 1:
                return None
    return found[0] if len(found) == 1 else None


def stage_planned_files(session_id: str, user_id: str, repo: dict, filenames: list) -> list:
    """Copy planned files from the open folder into the chat. Does not write disk."""
    root = (repo or {}).get("root_path") or ""
    repo_id = (repo or {}).get("id") or ""
    if not root or not repo_id:
        return []
    out = []
    seen = set()
    for raw in filenames or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        rel = _planned_rel(root, name)
        if not rel:
            continue
        try:
            row = stage_path(session_id, user_id, repo_id, rel)
        except HTTPException:
            continue
        out.append({
            "requested": name,
            "filename": row.get("filename") or rel,
            "content": row.get("content") or "",
            "id": row.get("id") or "",
            "file_type": row.get("file_type") or "code",
        })
    return out


def stage_path(session_id: str, user_id: str, repo_id: str, rel_path: str) -> dict:
    """Copy one repository file into session_files. Does not write to disk."""
    repo = get_owned(user_id, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    root = repo["root_path"]
    target = resolve_inside(root, rel_path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    rel = str(target.relative_to(os.path.realpath(root))).replace(os.sep, "/")
    size = target.stat().st_size
    if size > 1_000_000:
        raise HTTPException(status_code=413, detail="File is over 1MB")
    text = _read_text(target)
    sha = content_sha(text)
    language = _language(rel)
    lines = len(text.splitlines())
    with get_db_ctx() as conn:
        existing = conn.execute(
            "SELECT id, edited FROM session_files WHERE session_id = ? AND filename = ?",
            (session_id, rel),
        ).fetchone()
        if existing:
            file_id = existing["id"] if hasattr(existing, "keys") else existing[0]
            edited = existing["edited"] if hasattr(existing, "keys") else existing[1]
            if edited:
                row = conn.execute(
                    "SELECT content FROM session_files WHERE id = ?",
                    (file_id,),
                ).fetchone()
                kept = row["content"] if hasattr(row, "keys") else row[0]
                return {
                    "id": file_id,
                    "filename": rel,
                    "kept_edits": True,
                    "content": kept,
                    "base_sha": sha,
                }
            conn.execute(
                """UPDATE session_files
                   SET content = ?, language = ?, lines = ?, repo_path = ?, base_sha = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (text, language, lines, rel, sha, file_id),
            )
        else:
            file_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO session_files
                   (id, session_id, filename, content, language, lines, symbol_count,
                    file_type, origin, repo_path, base_sha, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, 'code', 'uploaded', ?, ?, CURRENT_TIMESTAMP)""",
                (file_id, session_id, rel, text, language, lines, rel, sha),
            )
        conn.commit()
    _dlog("repo_file_staged", session_id=session_id, filename=rel)
    return {"id": file_id, "filename": rel, "kept_edits": False, "content": text, "base_sha": sha}


def _write_disk(root: str, rel: str, content: str, force: bool, base_sha: str) -> dict:
    target = resolve_inside(root, rel)
    if target.exists() and not target.is_file():
        return {"ok": False, "reason": "conflict"}
    if target.exists():
        disk_sha = content_sha(_read_text(target))
        if base_sha and disk_sha != base_sha and not force:
            return {"ok": False, "reason": "drift"}
    elif base_sha and not force:
        return {"ok": False, "reason": "missing"}
    from services.surgical_editor import _backup_file
    if target.exists():
        _backup_file(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content if content is not None else "", encoding="utf-8")
    return {"ok": True, "sha": content_sha(content or "")}


def apply_files(session_id: str, user_id: str, file_ids, force: bool = False) -> dict:
    repo = repository_for_session(session_id)
    if not repo or repo.get("missing"):
        raise HTTPException(status_code=404, detail="This chat has no open folder")
    if repo.get("user_id") and user_id and repo["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your repository")
    root = repo["root_path"]
    with get_db_ctx() as conn:
        rows = conn.execute(
            """SELECT id, filename, content, repo_path, base_sha
               FROM session_files WHERE session_id = ?""",
            (session_id,),
        ).fetchall()
    wanted = set(file_ids or [])
    applied, conflicts, skipped = [], [], []
    for row in rows:
        item = dict(row)
        if wanted and item["id"] not in wanted:
            continue
        rel = item.get("repo_path") or ""
        if not rel:
            skipped.append({"id": item["id"], "filename": item["filename"], "reason": "not_in_repo"})
            continue
        state = disk_state_for(rel, root, item.get("content") or "", item.get("base_sha") or "")
        if state == "applied" and not force:
            skipped.append({"id": item["id"], "filename": item["filename"], "reason": "applied"})
            continue
        result = _write_disk(root, rel, item.get("content") or "", force, item.get("base_sha") or "")
        if not result.get("ok"):
            conflicts.append({
                "id": item["id"],
                "filename": item["filename"],
                "repo_path": rel,
                "reason": result.get("reason") or "conflict",
            })
            continue
        with get_db_ctx() as conn:
            conn.execute(
                "UPDATE session_files SET base_sha = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (result["sha"], item["id"]),
            )
            conn.commit()
        applied.append({"id": item["id"], "filename": item["filename"], "repo_path": rel})
    _dlog("repo_apply", session_id=session_id, applied=len(applied), conflicts=len(conflicts))
    return {"applied": applied, "conflicts": conflicts, "skipped": skipped}


def maybe_auto_apply(session_id: str, file_id: str) -> dict:
    """Write one staged file when the repository has auto-apply on. Never raises."""
    try:
        repo = repository_for_session(session_id)
        if not repo or not repo.get("auto_apply") or repo.get("missing"):
            return {"ran": False}
        result = apply_files(session_id, repo.get("user_id") or "", [file_id], force=False)
        return {"ran": True, **result}
    except Exception as exc:
        _dlog("repo_auto_apply_failed", session_id=session_id, error=str(exc)[:200])
        return {"ran": False, "error": str(exc)[:200]}


def sync_undo_to_disk(session_id: str, file_id: str, replaced_content: str) -> str:
    """Write the restored chat copy to disk only when disk still has the copy we replaced.

    A staged edit that was never applied leaves disk on the older text, so
    undo must not touch it. An applied edit matches disk, so undo moves disk
    back with the same backup helper as Apply.
    """
    try:
        repo = repository_for_session(session_id)
        if not repo or repo.get("missing"):
            return "skipped"
        with get_db_ctx() as conn:
            row = conn.execute(
                "SELECT content, repo_path FROM session_files WHERE id = ? AND session_id = ?",
                (file_id, session_id),
            ).fetchone()
        if not row:
            return "skipped"
        item = dict(row)
        rel = item.get("repo_path") or ""
        if not rel:
            return "skipped"
        target = resolve_inside(repo["root_path"], rel)
        restored = item.get("content") or ""
        if not target.is_file():
            return "skipped"
        disk_sha = content_sha(_read_text(target))
        if disk_sha == content_sha(restored):
            with get_db_ctx() as conn:
                conn.execute(
                    "UPDATE session_files SET base_sha = ? WHERE id = ?",
                    (disk_sha, file_id),
                )
                conn.commit()
            return "skipped"
        if disk_sha != content_sha(replaced_content or ""):
            return "conflict"
        result = _write_disk(repo["root_path"], rel, restored, True, "")
        if not result.get("ok"):
            return "conflict"
        with get_db_ctx() as conn:
            conn.execute(
                "UPDATE session_files SET base_sha = ? WHERE id = ?",
                (result["sha"], file_id),
            )
            conn.commit()
        return "written"
    except Exception:
        return "skipped"
