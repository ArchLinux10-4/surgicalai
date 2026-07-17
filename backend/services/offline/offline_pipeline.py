"""
Offline pipeline orchestrator — plain chat + single-file whole-rewrite only.

Explicitly OUT OF SCOPE for v1 (per evidence-based decision, see OFFLINE_MODE.md):
  - Agent mode / multi-step task planning
  - Tool-calling / function-calling of any kind
  - SEARCH/REPLACE surgical diffs (unreliable at 7B — whole-file rewrite instead)
  - Multi-file edits in one pass (v1 edits the single most relevant attached file)
  - Auto-apply saves the rewrite to the DB (with previous_content for undo).
    The file panel refreshes automatically. User can Undo if the local model
    produced bad output.

This module never imports from services.pipeline's Claude/OpenAI logic, and
services.pipeline is never modified to support this. The only integration
point is a single dispatch check added in backend/routers/chat.py.
"""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

from database import get_db_ctx

from .offline_client import (
    ollama_chat_stream,
    ollama_chat_once,
    OfflineModelError,
)
from .offline_prompts import (
    OFFLINE_CHAT_SYSTEM,
    OFFLINE_EDIT_SYSTEM,
    FILE_REWRITE_START,
    FILE_REWRITE_END,
    build_edit_user_prompt,
)

try:
    from services.pipeline import _dlog
except Exception:  # pragma: no cover - _dlog is best-effort logging only
    def _dlog(event: str, **kwargs):
        print(f"[offline_dlog] {event} {kwargs}")


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj) + "\n\n"


def _pick_target_file(session_files: list, user_request: str) -> Optional[dict]:
    """
    v1: pick a single file to edit. If exactly one file is attached, use it.
    If multiple are attached, prefer one whose filename is mentioned in the
    request; otherwise fall back to the most recently added file and tell
    the user which one was chosen (no silent guessing across many files).
    """
    if not session_files:
        return None
    if len(session_files) == 1:
        return session_files[0]
    lowered_req = user_request.lower()
    for f in session_files:
        if f.get("filename", "").lower() in lowered_req:
            return f
    return session_files[-1]


def _looks_like_edit_request(user_request: str) -> bool:
    edit_cues = (
        "fix", "change", "update", "refactor", "add ", "remove", "rewrite",
        "edit", "modify", "implement", "rename", "delete", "replace",
    )
    lowered = user_request.lower()
    return any(cue in lowered for cue in edit_cues)


async def run_offline_stream(
    *,
    session_files: list,
    user_request: str,
    conversation_history: list,
    session_id: str = "",
    project_memory: str = "",
    session_summary: str = "",
    user_id: str = "",
) -> AsyncIterator[str]:
    """
    Drop-in-shaped generator matching the SSE vocabulary the frontend already
    understands: {"type": "chat"|"token", ...}, {"type": "done"}, {"type": "error"}.
    """
    target_file = None
    if session_files and _looks_like_edit_request(user_request):
        target_file = _pick_target_file(session_files, user_request)

    try:
        if target_file:
            async for evt in _run_offline_edit(target_file, user_request, session_id, user_id):
                yield evt
        else:
            async for evt in _run_offline_chat(
                user_request, conversation_history, session_summary, project_memory, session_id, user_id,
                session_files=session_files,
            ):
                yield evt
    except OfflineModelError as e:
        _dlog("offline_pipeline_error", session_id=session_id, user_id=user_id, error=str(e))
        yield _sse({"type": "error", "content": str(e)})
        yield _sse({"type": "done"})
        return

    yield _sse({"type": "done"})


async def _run_offline_chat(
    user_request: str,
    conversation_history: list,
    session_summary: str,
    project_memory: str,
    session_id: str,
    user_id: str,
    session_files: list | None = None,
) -> AsyncIterator[str]:
    messages = [{"role": "system", "content": OFFLINE_CHAT_SYSTEM}]
    if project_memory:
        messages.append({"role": "system", "content": f"Team conventions/memory:\n{project_memory}"})
    if session_summary:
        messages.append({"role": "system", "content": f"Earlier conversation summary:\n{session_summary}"})
    # Inject attached file contents so the model can see and reference them
    if session_files:
        file_parts = []
        total_chars = 0
        max_chars = 24_000  # ~6k tokens budget for file context (7B model limit)
        for f in session_files:
            fname = f.get("filename", "file")
            content = f.get("content", "")
            if total_chars + len(content) > max_chars:
                # Truncate large files to fit within context budget
                remaining = max(0, max_chars - total_chars)
                if remaining > 200:
                    content = content[:remaining] + f"\n... [truncated — {len(f.get('content', '')) - remaining} more chars]"
                else:
                    file_parts.append(f"--- {fname} --- (skipped, context budget exhausted)")
                    continue
            file_parts.append(f"--- {fname} ---\n{content}\n--- end {fname} ---")
            total_chars += len(content)
        files_block = "\n\n".join(file_parts)
        messages.append({"role": "system", "content": f"Attached files:\n\n{files_block}"})
    for turn in conversation_history[-20:]:
        messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
    messages.append({"role": "user", "content": user_request})

    _dlog("offline_chat_start", session_id=session_id, user_id=user_id, msg_len=len(user_request))

    input_chars = sum(len(m.get("content", "")) for m in messages)
    collected = []
    try:
        async for delta in ollama_chat_stream(messages, input_chars_for_sizing=input_chars):
            collected.append(delta)
            yield _sse({"type": "chat", "content": delta})
    except OfflineModelError as e:
        _dlog("offline_chat_error", session_id=session_id, user_id=user_id, error=str(e))
        raise

    _dlog("offline_chat_done", session_id=session_id, user_id=user_id, out_len=len("".join(collected)))


async def _run_offline_edit(
    target_file: dict,
    user_request: str,
    session_id: str,
    user_id: str,
) -> AsyncIterator[str]:
    filename = target_file.get("filename", "file")
    content = target_file.get("content", "")

    yield _sse({
        "type": "chat",
        "content": f"*Offline mode: rewriting `{filename}` locally (this may take a bit longer than cloud models)...*\n\n",
    })

    messages = [
        {"role": "system", "content": OFFLINE_EDIT_SYSTEM},
        {"role": "user", "content": build_edit_user_prompt(filename, content, user_request)},
    ]

    _dlog("offline_edit_start", session_id=session_id, user_id=user_id, filename=filename, file_len=len(content))

    try:
        result = await ollama_chat_once(messages, input_chars_for_sizing=len(content))
    except OfflineModelError as e:
        _dlog("offline_edit_error", session_id=session_id, user_id=user_id, filename=filename, error=str(e))
        raise

    raw = result["content"]
    truncated = result["truncated"]

    if FILE_REWRITE_START not in raw or FILE_REWRITE_END not in raw:
        _dlog("offline_edit_parse_fail", session_id=session_id, user_id=user_id, filename=filename,
              truncated=truncated, out_len=len(raw))
        yield _sse({
            "type": "chat",
            "content": (
                "The local model didn't return the expected rewrite format "
                "(this can happen with smaller local models on complex requests). "
                "Here's its raw response so you can see what it produced:\n\n" + raw
            ),
        })
        return

    explanation = raw.split(FILE_REWRITE_START)[0].strip()
    rewritten = raw.split(FILE_REWRITE_START, 1)[1].split(FILE_REWRITE_END, 1)[0]
    # Model sometimes wraps content in a stray code fence despite instructions.
    rewritten = rewritten.strip()
    if rewritten.startswith("```"):
        lines = rewritten.split("\n")
        rewritten = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    if truncated:
        _dlog("offline_edit_truncated", session_id=session_id, user_id=user_id, filename=filename,
              out_len=len(raw))
        yield _sse({
            "type": "chat",
            "content": (
                "\u26a0\ufe0f *The local model's response was cut off before finishing "
                f"(file may be too large for a single pass). The partial rewrite of `{filename}` "
                "has been saved \u2014 please review carefully and use Undo if needed.*\n\n"
            ),
        })

    # ── Save rewritten file to DB (with previous_content for undo) ──
    file_id = target_file.get("id")
    saved = False
    if file_id:
        try:
            with get_db_ctx() as conn:
                row = conn.execute(
                    "SELECT content, updated_at FROM session_files WHERE id = ? AND session_id = ?",
                    (file_id, session_id)
                ).fetchone()
                if row:
                    prev_content = row["content"] if hasattr(row, "__getitem__") else row[0]
                    row_updated_at = row["updated_at"] if hasattr(row, "__getitem__") else row[1]
                    new_lines = len(rewritten.splitlines())
                    conn.execute(
                        "UPDATE session_files SET content = ?, previous_content = ?, lines = ?, "
                        "edited = 1, updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND session_id = ? AND updated_at = ?",
                        (rewritten, prev_content, new_lines, file_id, session_id, row_updated_at)
                    )
                    conn.commit()
                    saved = True
                    _dlog("offline_edit_saved", session_id=session_id, user_id=user_id,
                          filename=filename, file_id=file_id, new_lines=new_lines)
        except Exception as exc:
            _dlog("offline_edit_save_error", session_id=session_id, user_id=user_id,
                  filename=filename, error=str(exc))

    if saved:
        body = (
            f"{explanation}\n\n"
            f"\u2705 **`{filename}` has been updated.** "
            f"The file panel will refresh automatically. "
            f"Use the **Undo** button if you want to revert.\n"
        )
    else:
        # Fallback: couldn't save — show the code so the user can copy it
        ext = filename.split(".")[-1] if "." in filename else ""
        body = (
            f"{explanation}\n\n"
            f"Could not auto-save `{filename}` — here is the rewritten file:\n\n"
            f"```{ext}\n{rewritten}\n```\n"
        )
    yield _sse({"type": "chat", "content": body})

    _dlog("offline_edit_done", session_id=session_id, user_id=user_id, filename=filename,
          out_len=len(rewritten), truncated=truncated, saved=saved)
