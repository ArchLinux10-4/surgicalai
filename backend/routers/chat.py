"""Chat router — standard AI chat with file context."""
import asyncio
import uuid
import json
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from models.schemas import ChatRequest, NewSessionRequest, ChatSession
from database import get_db, get_db_ctx, get_setting, get_user_api_key, GLOBAL_MEMORY_KEY
from services.pipeline import run_chat, run_chat_stream, _dlog

router = APIRouter()


async def _with_heartbeat(aiter, interval: int = 5):
    """Wrap an async iterator, yielding SSE keepalive comments when it stalls.

    Prevents Railway/nginx from closing idle SSE connections during long Claude
    API calls (architect pass, QA pass, fix-loop retry).  SSE comment lines
    starting with ':' are forwarded by all proxies and silently ignored by
    browsers, so this is invisible to the client UI.
    """
    _DONE = object()
    queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        try:
            async for item in aiter:
                await queue.put(item)
        finally:
            await queue.put(_DONE)

    task = asyncio.create_task(_produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is _DONE:
                break
            yield item
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as _hb_exc:
            _dlog("heartbeat_swallow", exc_type=type(_hb_exc).__name__, exc_msg=str(_hb_exc))


def _load_effective_memory(conn, session_id):
    """Merge GLOBAL (team-wide) project memory with per-session memory.

    Global conventions are injected into every prompt for every session/user;
    any session-specific memory is appended after. Returns None when both are
    empty so downstream code behaves exactly as before for the no-memory case.
    """
    parts = []
    g = conn.execute(
        "SELECT content FROM project_memory WHERE workspace_path = ? LIMIT 1",
        (GLOBAL_MEMORY_KEY,)
    ).fetchone()
    if g and (g["content"] or "").strip():
        parts.append(g["content"].strip())
    if session_id and session_id != GLOBAL_MEMORY_KEY:
        s = conn.execute(
            "SELECT content FROM project_memory WHERE workspace_path = ? LIMIT 1",
            (session_id,)
        ).fetchone()
        if s and (s["content"] or "").strip():
            parts.append(s["content"].strip())
    return "\n\n".join(parts) if parts else None


@router.post("/sessions")
def create_session(req: NewSessionRequest, request: Request):
    session_id = str(uuid.uuid4())
    user_id = getattr(request.state, "user_id", None)
    with get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, file_path, model, user_id) VALUES (?, ?, ?, ?, ?)",
            (session_id, req.title, req.file_path, req.model or get_setting("architect_model", "gpt-4.1"), user_id)
        )
        conn.commit()
    return {"id": session_id, "title": req.title}

@router.get("/search")
def search_chats(request: Request, q: str):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return []
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    with get_db_ctx() as conn:
        # Query A: sessions matching by title
        session_rows = conn.execute(
            "SELECT id, title FROM chat_sessions WHERE user_id = ? AND LOWER(title) LIKE LOWER(?)",
            (user_id, like)
        ).fetchall()
        # Query B: messages matching content
        message_rows = conn.execute(
            "SELECT m.id, m.session_id, m.role, m.content, m.created_at, s.title FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id WHERE s.user_id = ? AND LOWER(m.content) LIKE LOWER(?)",
            (user_id, like)
        ).fetchall()
        results_dict = {}
        # Seed with sessions from Query A
        for row in session_rows:
            results_dict[row["id"]] = {
                "session_id": row["id"],
                "session_name": row["title"],
                "matched_messages": []
            }
        # Add messages from Query B
        for m in message_rows:
            sid = m["session_id"]
            if sid not in results_dict:
                results_dict[sid] = {
                    "session_id": sid,
                    "session_name": m["title"],
                    "matched_messages": []
                }
            results_dict[sid]["matched_messages"].append({
                "message_id": m["id"],
                "role": m["role"],
                "content_snippet": (m["content"] or "")[:200],
                "created_at": m["created_at"]
            })
        # For sessions from Query A with no matched_messages, fetch matching messages in that session
        for sid, entry in results_dict.items():
            if not entry["matched_messages"]:
                extra_rows = conn.execute(
                    "SELECT id, role, content, created_at FROM chat_messages WHERE session_id = ? AND LOWER(content) LIKE LOWER(?)",
                    (sid, like)
                ).fetchall()
                for m in extra_rows:
                    entry["matched_messages"].append({
                        "message_id": m["id"],
                        "role": m["role"],
                        "content_snippet": (m["content"] or "")[:200],
                        "created_at": m["created_at"]
                    })
        return list(results_dict.values())



# ---------------------------------------------------------------------------
# Rolling compaction helper
# ---------------------------------------------------------------------------
import openai as _openai_mod

# Token-aware compaction settings
HISTORY_TOKEN_BUDGET = 30_000  # Max estimated tokens before compaction triggers
MIN_RECENT_KEEP = 6            # Always keep at least this many recent messages

async def _compact_session(session_id: str, user_id: str) -> str:
    """
    Token-aware compaction: compact oldest uncompacted messages, keeping
    MIN_RECENT_KEEP recent ones. Uses GPT-4.1-mini (fast + cheap).
    Returns the new summary string.
    """
    with get_db_ctx() as db:
        try:
            all_uncompacted = db.execute(
                "SELECT id, role, content FROM chat_messages "
                "WHERE session_id = ? AND is_compacted = 0 "
                "ORDER BY created_at ASC",
                (session_id,)
            ).fetchall()
            if not all_uncompacted or len(all_uncompacted) <= MIN_RECENT_KEEP:
                return ""

            # Keep MIN_RECENT_KEEP most recent, compact everything else
            to_compact = all_uncompacted[:-MIN_RECENT_KEEP]
            if not to_compact:
                return ""
            _dlog("compact_token_aware", session_id=session_id,
                  total_uncompacted=len(all_uncompacted),
                  compacting=len(to_compact), keeping=MIN_RECENT_KEEP)

            session_row = db.execute(
                "SELECT session_summary FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            existing = (session_row["session_summary"] if session_row and session_row["session_summary"] else "") or ""

            turns_text_parts = []
            for r in to_compact:
                row = dict(r)
                role = row.get("role", "user").upper()
                raw = str(row.get("content", ""))

                # Clean stored format — same logic as _clean_history_content()
                # so the summarizer sees readable text, not raw JSON blobs
                if raw.startswith("__NATURAL_AND_RESULT__:"):
                    try:
                        import json as _j
                        payload = _j.loads(raw[len("__NATURAL_AND_RESULT__:"):])
                        text = payload.get("text", "").strip()
                        result = payload.get("result", {})
                        changes = []
                        for _fname, _fdata in result.get("changes_by_file", {}).items():
                            for ch in (_fdata.get("changes", []) if isinstance(_fdata, dict) else []):
                                sym = ch.get("symbol", {})
                                name = (sym.get("name") or sym.get("full_path", "")) if isinstance(sym, dict) else ""
                                if name:
                                    changes.append(f"{_fname}::{name}")
                        qa_flags = []
                        for _fname, _fdata in result.get("changes_by_file", {}).items():
                            for ch in (_fdata.get("changes", []) if isinstance(_fdata, dict) else []):
                                qr = ch.get("qa_result") or {}
                                verdict = qr.get("verdict", "")
                                summary = (qr.get("summary") or "").strip()
                                sym = ch.get("symbol", {})
                                name = (sym.get("name") or "") if isinstance(sym, dict) else ""
                                if verdict in ("warning", "blocked") and summary:
                                    qa_flags.append(f"{name}: {summary}")
                        if changes:
                            text += f" [Changed: {', '.join(changes[:4])}]"
                        if qa_flags:
                            text += f" [QA flagged: {'; '.join(qa_flags[:2])}]"
                        cleaned = text or "Made code changes."
                    except Exception:
                        cleaned = "Made code changes."
                elif raw.startswith("__SURGICAL_RESULT__:"):
                    cleaned = "Made code changes (surgical edit)."
                else:
                    cleaned = raw

                turns_text_parts.append(f"{role}: {cleaned[:500]}")

            turns_text = "\n".join(turns_text_parts)
            prompt_parts = []
            if existing:
                prompt_parts.append(f"Previous summary:\n{existing}\n")
            prompt_parts.append(f"New conversation turns to add:\n{turns_text}")

            compact_prompt = (
                "Summarize this coding assistant conversation history. "
                "Focus on: what files were discussed, what code changes were made or planned, "
                "key decisions, and any patterns or conventions established. "
                "Be concise but complete. Under 400 words. Use bullet points."
            )

            openai_key = get_setting("openai_api_key") or (get_user_api_key(user_id, "openai") if user_id else "")
            anthropic_key = get_setting("anthropic_api_key") or (get_user_api_key(user_id, "anthropic") if user_id else "")

            new_summary = ""
            if openai_key:
                client = _openai_mod.AsyncOpenAI(api_key=openai_key)
                resp = await client.chat.completions.create(
                    model="gpt-4.1-mini",
                    messages=[
                        {"role": "system", "content": compact_prompt},
                        {"role": "user", "content": "\n".join(prompt_parts)}
                    ],
                    max_tokens=500,
                )
                new_summary = resp.choices[0].message.content or ""
            elif anthropic_key:
                from anthropic import AsyncAnthropic as _AsyncAnthropic
                aclient = _AsyncAnthropic(api_key=anthropic_key)
                resp = await aclient.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=500,
                    system=compact_prompt,
                    messages=[{"role": "user", "content": "\n".join(prompt_parts)}],
                )
                new_summary = resp.content[0].text if resp.content else ""
            else:
                return existing

            ids = [dict(r)["id"] for r in to_compact]
            placeholders = ",".join(["?" for _ in ids])
            db.execute(
                f"UPDATE chat_messages SET is_compacted = 1 WHERE id IN ({placeholders})",
                ids
            )
            db.execute(
                "UPDATE chat_sessions SET session_summary = ? WHERE id = ?",
                (new_summary, session_id)
            )
            db.commit()
            return new_summary
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"[compact] Error: {exc}")
            return ""


@router.get("/sessions")
def list_sessions(request: Request):
    user_id = getattr(request.state, "user_id", None)
    with get_db_ctx() as conn:
        if user_id:
            rows = conn.execute("""
                SELECT s.*, COUNT(m.id) as message_count
                FROM chat_sessions s
                LEFT JOIN chat_messages m ON m.session_id = s.id
                WHERE s.user_id = ? OR s.user_id IS NULL
                GROUP BY s.id
                ORDER BY s.updated_at DESC
                LIMIT 50
            """, (user_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT s.*, COUNT(m.id) as message_count
                FROM chat_sessions s
                LEFT JOIN chat_messages m ON m.session_id = s.id
                GROUP BY s.id
                ORDER BY s.updated_at DESC
                LIMIT 50
            """).fetchall()
    return [dict(r) for r in rows]


@router.get("/sessions/{session_id}/messages")
def get_messages(session_id: str):
    with get_db_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,)
        ).fetchall()
    msgs = []
    for r in rows:
        d = dict(r)
        content = d.get("content", "")
        if content.startswith("__SURGICAL_RESULT__:"):
            d["message_type"] = "surgical_result"
            d["surgical_data"] = content[len("__SURGICAL_RESULT__:"):]
            d["content"] = ""
        elif content.startswith("__NATURAL_AND_RESULT__:"):
            try:
                import json as _j
                payload = _j.loads(content[len("__NATURAL_AND_RESULT__:"):])
                d["message_type"] = "natural_result"
                d["content"] = payload.get("text", "")
                # Pack the result back in surgical_data for the diff card renderer
                d["surgical_data"] = _j.dumps(payload.get("result", {}))
            except Exception:
                d["message_type"] = "surgical_result"
                d["surgical_data"] = content[len("__NATURAL_AND_RESULT__:"):]
                d["content"] = ""
        msgs.append(d)
    return msgs


@router.post("/send")
def send_message(req: ChatRequest):
    if not get_setting("openai_api_key") and not get_setting("anthropic_api_key") and get_setting("ollama_enabled") != "true":
        raise HTTPException(status_code=401, detail="No AI backend configured. Go to Settings.")

    with get_db_ctx() as conn:

        # Save user message
        msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (msg_id, req.session_id, "user", req.message)
        )

        # Get conversation history
        history = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY created_at ASC",
            (req.session_id,)
        ).fetchall()

        messages = [{"role": r["role"], "content": r["content"]} for r in history]

        # Get pinned context and project memory
        workspace = req.session_id  # Use session_id as workspace for per-session memory
        pinned_rows = conn.execute(
            "SELECT * FROM pinned_context WHERE workspace_path = ?", (workspace,)
        ).fetchall() if workspace else []

        pinned_context = []
        for pin in pinned_rows:
            try:
                with open(pin["file_path"], 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()[:3000]
                pinned_context.append({"label": pin["label"], "file_path": pin["file_path"], "content": content})
            except Exception:
                pass

        # Global (team-wide) memory + per-session memory, both injected every prompt
        project_memory = _load_effective_memory(conn, workspace)

        conn.commit()

    # Run AI
    try:
        response = run_chat(
            messages=messages,
            file_content=req.file_content,
            symbol_context=req.symbol_context,
            model=req.model,
            pinned_context=pinned_context,
            project_memory=project_memory
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

    # Save assistant message
    with get_db_ctx() as conn:
        resp_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (resp_id, req.session_id, "assistant", response)
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (req.session_id,)
        )
        conn.commit()

    return {"id": resp_id, "role": "assistant", "content": response}


@router.post("/stream")
async def stream_message(req: ChatRequest):
    """Streaming version of chat send. Returns SSE stream."""
    if not get_setting("openai_api_key") and not get_setting("anthropic_api_key") and get_setting("ollama_enabled") != "true":
        raise HTTPException(status_code=401, detail="No AI backend configured.")

    with get_db_ctx() as conn:

        # Save user message
        msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (msg_id, req.session_id, "user", req.message)
        )

        # Get history
        history = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY created_at ASC",
            (req.session_id,)
        ).fetchall()
        messages = [{"role": r["role"], "content": r["content"]} for r in history]

        # Get pinned context and memory
        workspace = req.session_id  # Use session_id as workspace for per-session memory
        pinned_rows = conn.execute(
            "SELECT * FROM pinned_context WHERE workspace_path = ?", (workspace,)
        ).fetchall() if workspace else []

        pinned_context = []
        for pin in pinned_rows:
            try:
                with open(pin["file_path"], 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()[:3000]
                pinned_context.append({"label": pin["label"], "file_path": pin["file_path"], "content": content})
            except Exception:
                pass

        # Global (team-wide) memory + per-session memory, both injected every prompt
        project_memory = _load_effective_memory(conn, workspace)

        conn.commit()

    session_id = req.session_id

    async def stream_and_collect():
        collected = []
        async for chunk in run_chat_stream(
            messages, req.file_content, req.symbol_context, req.model, pinned_context, project_memory
        ):
            if chunk.startswith("data: "):
                try:
                    data = json.loads(chunk[6:])
                    if data.get("type") == "token":
                        collected.append(data.get("content", ""))
                    elif data.get("type") == "done":
                        # Save full response to DB
                        full_text = "".join(collected)
                        resp_id = str(uuid.uuid4())
                        with get_db_ctx() as db:
                            db.execute(
                                "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
                                (resp_id, session_id, "assistant", full_text)
                            )
                            db.execute(
                                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (session_id,)
                            )
                            db.commit()
                except Exception:
                    pass
            yield chunk

    return StreamingResponse(stream_and_collect(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    with get_db_ctx() as conn:
        # Cascade: delete files and messages before session row
        conn.execute("DELETE FROM session_files WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        conn.commit()
        # Hard-delete all session-scoped auxiliary rows so nothing orphans by
        # session_id (code-diff history, applied/undo state, tasks, audit logs).
        # Each runs in its own committed step and is best-effort: a not-yet-created
        # table simply rolls back and is skipped, never breaking the core delete.
        for _tbl in (
            "change_history",
            "applied_changes",
            "agent_tasks",
            "qa_log",
            "compliance_log",
        ):
            try:
                conn.execute(f"DELETE FROM {_tbl} WHERE session_id = ?", (session_id,))
                conn.commit()
            except Exception:
                conn.rollback()
    return {"ok": True}


@router.patch("/sessions/{session_id}")
def rename_session(session_id: str, body: dict):
    title = body.get("title", "")
    if not title:
        raise HTTPException(status_code=400, detail="Title required")
    with get_db_ctx() as conn:
        conn.execute("UPDATE chat_sessions SET title = ? WHERE id = ?", (title, session_id))
        conn.commit()
    return {"ok": True}


@router.post("/smart-stream")
async def smart_stream(req: dict, request: Request):
    """
    v1.3 Smart unified endpoint.
    Loads session files from DB, auto-routes to surgical edit or chat response.
    """
    from fastapi.responses import StreamingResponse
    from services.pipeline import run_natural_pipeline_stream, run_smart_pipeline_stream

    session_id = req.get("session_id")
    message = req.get("message", "")
    # v1.5: explicit Agent Mode toggle from the UI. ORed with the phrase cue
    # below — never replaces it, so "create tasks" prompts keep working.
    force_tasks = bool(req.get("force_tasks", False))
    current_user_id = getattr(request.state, "user_id", "") or ""

    # Check per-user encrypted keys AND global settings
    from database import get_user_api_key
    has_openai = bool(get_setting("openai_api_key")) or bool(get_user_api_key(current_user_id, "openai") if current_user_id else "")
    has_anthropic = bool(get_setting("anthropic_api_key")) or bool(get_user_api_key(current_user_id, "anthropic") if current_user_id else "")
    if not has_openai and not has_anthropic and get_setting("ollama_enabled") != "true":
        raise HTTPException(status_code=401, detail="No AI backend configured. Go to Settings.")

    with get_db_ctx() as conn:

        # Save user message
        msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (msg_id, session_id, "user", message)
        )

        # Load session_summary
        sess_row = conn.execute(
            "SELECT session_summary FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        session_summary = (sess_row["session_summary"] if sess_row and sess_row["session_summary"] else "") or ""

        # Get conversation history — only uncompacted messages
        history = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id = ? AND is_compacted = 0 ORDER BY created_at ASC",
            (session_id,)
        ).fetchall()
        conversation_history = [{"role": r["role"], "content": r["content"]} for r in history]

        # Token-aware compaction trigger: estimate tokens from loaded history
        _history_tokens = sum(len(h.get("content") or "") for h in conversation_history) // 4
        needs_compaction = _history_tokens > HISTORY_TOKEN_BUDGET
        _dlog("compaction_check", session_id=session_id,
              history_tokens=_history_tokens, budget=HISTORY_TOKEN_BUDGET,
              msg_count=len(conversation_history), needs_compaction=needs_compaction)

        # Load session files
        file_rows = conn.execute(
            "SELECT id, filename, content, language, lines, symbol_count, file_type FROM session_files WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,)
        ).fetchall()
        session_files = [dict(r) for r in file_rows]

        # File-awareness hint: prepend attached file list to user message so
        # all models (including smaller ones) notice files immediately.
        if session_files:
            _fnames = [f['filename'] for f in session_files]
            _file_hint = (f"[{len(_fnames)} file(s) attached: {', '.join(_fnames)}. "
                          "Examine their contents before responding.]")
            message = f"{_file_hint}\n\n{message}"

        # Project memory — GLOBAL team conventions (every prompt) + any per-session memory
        project_memory = _load_effective_memory(conn, session_id)

        conn.commit()

    async def stream_and_save():
        import json as _json

        collected_tokens = []
        result_content = None
        # Latest resolution checkpoint from the pipeline (session e4e9d098):
        # resolved edits emitted BEFORE risky long waits, so a disconnect
        # doesn't vaporize completed work.
        checkpoint_content = None
        current_summary = session_summary
        _stream_t0 = time.time()
        _phase = "init"
        _dlog("sse_stream_start", session_id=session_id, user_id=current_user_id,
              needs_compaction=needs_compaction, msg_len=len(message))

        # --- Rolling compaction (with keepalive) ---
        if needs_compaction:
            _phase = "compaction"
            _compact_t0 = time.time()
            _compact_ka = 0
            _dlog("sse_compaction_start", session_id=session_id, user_id=current_user_id)
            yield "data: " + _json.dumps({"type": "compacting", "content": "Compacting conversation history..."}) + "\n\n"
            try:
                _compact_task = asyncio.create_task(
                    _compact_session(session_id, current_user_id)
                )
                while not _compact_task.done():
                    done, _ = await asyncio.wait({_compact_task}, timeout=5)
                    if not done:
                        _compact_ka += 1
                        yield ": keepalive\n\n"
                new_sum = _compact_task.result()
                if new_sum:
                    current_summary = new_sum
                _dlog("sse_compaction_done", session_id=session_id, user_id=current_user_id,
                      duration_s=round(time.time() - _compact_t0, 1), keepalives=_compact_ka)
            except Exception as _ce:
                _dlog("sse_compaction_error", session_id=session_id, user_id=current_user_id,
                      duration_s=round(time.time() - _compact_t0, 1), error=str(_ce))
                print(f"[compact] failed: {_ce}")
            yield "data: " + _json.dumps({"type": "compacting_done", "content": "History compacted"}) + "\n\n"

        # Use natural pipeline (Claude talks naturally, embeds <surgical_edit> tags)
        # Fall back to legacy pipeline for non-Claude models
        from database import get_setting as _gs
        _arch_model = _gs("architect_model", "gpt-4.1")
        _is_claude = _arch_model.startswith("claude-")
        _use_natural = True  # All models use natural pipeline (R25)

        # ── Agentic task branch ───────────────────────────────────────────
        # Only when (a) using the Claude natural pipeline and (b) the user
        # explicitly asked to break the work into tasks. Otherwise the normal
        # single-pass path below runs exactly as before (zero behaviour change).
        from services.task_planner import (
            wants_task_breakdown, plan_tasks, create_tasks, update_task,
            cancel_requested_for_run, mark_pending_cancelled,
        )

        def _sse(obj):
            return "data: " + _json.dumps(obj) + "\n\n"

        _cue_match = wants_task_breakdown(message)
        _dlog("sse_task_gate", session_id=session_id, user_id=current_user_id,
              force_tasks=force_tasks, cue_match=_cue_match, use_natural=_use_natural)

        if (force_tasks or _cue_match) and not _is_claude:
            # Tasks were explicitly requested (toggle or phrase) but the active
            # model is not Claude, so the agentic pipeline cannot run. Tell the
            # user instead of silently ignoring the request (fix #4).
            _dlog("sse_task_gate_model_notice", session_id=session_id,
                  user_id=current_user_id, architect_model=_arch_model)
            yield _sse({"type": "chat", "content": (
                "*Note: Agent Mode (multi-agent tasks) requires a Claude model. "
                "Running this as a normal single-pass request instead.*\n\n"
            )})

        if _is_claude and (force_tasks or _cue_match):
            _phase = "planning"
            _plan_t0 = time.time()
            _plan_ka = 0
            _dlog("sse_planning_start", session_id=session_id, user_id=current_user_id)
            yield _sse({"type": "planning_started"})
            yield _sse({"type": "progress", "content": "Planning tasks..."})
            try:
                _plan_task = asyncio.create_task(
                    plan_tasks(message, session_files, current_user_id)
                )
                while not _plan_task.done():
                    done, _ = await asyncio.wait({_plan_task}, timeout=5)
                    if not done:
                        _plan_ka += 1
                        yield ": keepalive\n\n"
                _plan = _plan_task.result()
                _dlog("sse_planning_done", session_id=session_id, user_id=current_user_id,
                      duration_s=round(time.time() - _plan_t0, 1), keepalives=_plan_ka,
                      task_count=len((_plan.get("tasks") or [])))
            except Exception as _pe:
                _dlog("sse_planning_error", session_id=session_id, user_id=current_user_id,
                      duration_s=round(time.time() - _plan_t0, 1), error=str(_pe))
                print(f"[tasks] planning failed: {_pe}")
                _plan = {"tasks": []}

            _planned = _plan.get("tasks", []) or []

            # NOTE: a post-plan merge step used to collapse any tasks that
            # named the same file into one task (plan_validator.validate_and_merge).
            # Removed: real-key testing proved it was why Agent Mode almost
            # never produced more than 1 task (see plan_validator.py module
            # docstring for the evidence). services/task_runner.py's wave
            # builder already guarantees same-file tasks never run in
            # parallel — it puts them in separate sequential waves — so no
            # pre-merge is needed for correctness, only for the (wrong)
            # assumption that same-file == must-be-one-task.

            # User explicitly said "create tasks" — always honour that.
            # The >= 2 guard only matters for auto-detection (currently unused).
            # When only 1 task, pass the original user message as the detail
            # so the agent gets the full request, not a lossy planner summary.
            if len(_planned) == 1:
                _planned[0]["detail"] = message
            if len(_planned) >= 1:
                run_id = str(uuid.uuid4())
                tasks = create_tasks(session_id, run_id, _planned)
                # Server-side runner flag (v2.0): when ON, the client hands
                # execution to POST /api/runs/start instead of driving the
                # per-task SSE queue itself. Flag OFF -> exact v1.4 behaviour.
                try:
                    from services.task_runner import server_runner_enabled as _sre
                    _server_run = bool(_sre())
                except Exception as _srx:
                    _dlog("sse_server_run_flag_error", session_id=session_id,
                          error=str(_srx)[:200])
                    _server_run = False
                _dlog("sse_tasks_created", session_id=session_id, user_id=current_user_id,
                      run_id=run_id, task_count=len(tasks), server_run=_server_run)
                yield _sse({
                    "type": "task_plan",
                    "run_id": run_id,
                    "server_run": _server_run,
                    "preamble": _plan.get("preamble", ""),
                    "tasks": [
                        {"id": t["id"], "seq": t["seq"], "title": t["title"],
                         "detail": t["detail"], "kind": t.get("kind", "code"),
                         "status": "pending"}
                        for t in tasks
                    ],
                })

                # Tasks are persisted. Execution is delegated to per-task SSE
                # streams (POST /chat/execute-task), driven by the client one
                # task at a time. Each task runs in its own short-lived
                # connection, so no single stream can approach the proxy /
                # process timeout that previously killed long multi-task runs.
                _phase = "tasks_planned"
                _dlog("sse_stream_done", session_id=session_id, user_id=current_user_id,
                      path="task_plan", total_duration_s=round(time.time() - _stream_t0, 1),
                      task_count=len(tasks))
                yield _sse({"type": "done"})
                return
            else:
                # Planning failed or the model returned zero usable tasks.
                # Previously this fell straight through to single-pass with
                # no signal at all — the non-Claude branch above already
                # tells the user when Agent Mode can't run; this mirrors it
                # for the "Claude, but planning produced nothing" case so the
                # user isn't left wondering why "create tasks" silently did
                # a normal single-pass edit instead.
                _dlog("sse_planning_empty_fallback", session_id=session_id,
                      user_id=current_user_id, architect_model=_arch_model)
                yield _sse({"type": "chat", "content": (
                    "*Note: Agent Mode couldn't generate a task plan for this "
                    "request. Running it as a normal single-pass request instead.*\n\n"
                )})
        # ── End agentic task branch ───────────────────────────────────────

        _phase = "single_pass"
        _dlog("sse_single_pass_start", session_id=session_id, user_id=current_user_id,
              elapsed_s=round(time.time() - _stream_t0, 1))

        # ── Offline mode dispatch (isolated codebase — see services/offline/) ──
        # Fully separate pipeline for local Qwen2.5-Coder:7b via Ollama. Chosen
        # only when offline mode is enabled AND no cloud key is configured for
        # the active model, so Claude/GPT users are never routed here.
        from services.pipeline import _should_use_ollama as _use_offline_check
        if _use_offline_check(_arch_model, current_user_id):
            from services.offline.offline_pipeline import run_offline_stream
            _pipeline = run_offline_stream
        else:
            _pipeline = run_natural_pipeline_stream if _use_natural else run_smart_pipeline_stream
        _saved = False

        try:
            async for chunk in _with_heartbeat(_pipeline(
                session_files=session_files,
                user_request=message,
                conversation_history=conversation_history,
                session_id=session_id,
                project_memory=project_memory,
                session_summary=current_summary,
                user_id=current_user_id,
            )):
                if chunk.startswith("data: "):
                    try:
                        data = _json.loads(chunk[6:])
                        chunk_type = data.get("type", "")
                        if chunk_type in ("token", "chat"):
                            collected_tokens.append(data.get("content", ""))
                        elif chunk_type == "smart_result":
                            result_content = data.get("content", "")
                        elif chunk_type == "checkpoint":
                            checkpoint_content = data.get("content", "")
                        elif chunk_type in ("done", "error"):
                            # Save assistant message — store both natural text AND result
                            with get_db_ctx() as db:
                                resp_id = str(uuid.uuid4())
                                natural_text = "".join(collected_tokens).strip()

                                if result_content:
                                    parsed_result = _json.loads(result_content)

                                    # Change 1: Surface QA warnings in the chat bubble text so
                                    # users see them in conversation flow, not only behind "Review".
                                    # Collect unique warning/blocked summaries from all changes.
                                    qa_warnings = []
                                    for _fdata in parsed_result.get("changes_by_file", {}).values():
                                        for ch in (_fdata.get("changes", []) if isinstance(_fdata, dict) else []):
                                            qr = ch.get("qa_result") or {}
                                            verdict = qr.get("verdict", "")
                                            summary = (qr.get("summary") or "").strip()
                                            score   = qr.get("qa_score")
                                            if verdict in ("warning", "blocked") and summary:
                                                icon = "⚠️" if verdict == "warning" else "🚫"
                                                sym  = (ch.get("symbol") or {})
                                                sym_name = sym.get("name") or sym.get("full_path", "change")
                                                qa_warnings.append(f"{icon} **{sym_name}** (QA {score}/10): {summary}")

                                    if qa_warnings:
                                        natural_text = (
                                            natural_text
                                            + ("\n\n" if natural_text else "")
                                            + "**QA Notes:**\n"
                                            + "\n".join(f"- {w}" for w in qa_warnings)
                                        )

                                    saved_content = "__NATURAL_AND_RESULT__:" + _json.dumps({
                                        "text": natural_text,
                                        "result": parsed_result,
                                    })
                                else:
                                    saved_content = natural_text

                                db.execute(
                                    "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
                                    (resp_id, session_id, "assistant", saved_content)
                                )
                                db.execute(
                                    "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                    (session_id,)
                                )
                                db.commit()
                                _saved = True
                    except Exception as _save_err:
                        print(f"[STREAM] DB save failed: {_save_err}")
                yield chunk
        finally:
            if not _saved:
                _dlog("sse_stream_disconnect", session_id=session_id, user_id=current_user_id,
                      phase=_phase, total_duration_s=round(time.time() - _stream_t0, 1),
                      tokens_collected=len(collected_tokens))
            else:
                _dlog("sse_stream_done", session_id=session_id, user_id=current_user_id,
                      path="single_pass", total_duration_s=round(time.time() - _stream_t0, 1))
            # Safety net: if stream ended without done/error (crash, timeout,
            # client disconnect), persist whatever tokens were collected.
            if not _saved and (collected_tokens or checkpoint_content):
                try:
                    with get_db_ctx() as db:
                        resp_id = str(uuid.uuid4())
                        fallback_text = "".join(collected_tokens).strip()
                        # ── Checkpoint recovery (session e4e9d098) ──────────
                        # A disconnect during the correction round used to
                        # save only visible chat tokens; a fully-resolved
                        # 10.2KB edit was lost.  If the pipeline emitted a
                        # resolution checkpoint, reconstruct the resolved
                        # edits as a readable message so the work survives.
                        if checkpoint_content:
                            try:
                                _ckpt = _json.loads(checkpoint_content)
                                _rec = _ckpt.get("resolved", [])
                                if _rec:
                                    _parts = [
                                        "\n\n---\n⚡ **Connection was interrupted**, "
                                        f"but I recovered {len(_rec)} completed "
                                        "edit(s) from before the drop:"
                                    ]
                                    for _r in _rec:
                                        _desc = _r.get("description") or "edit"
                                        _parts.append(
                                            f"\n**{_r.get('filename', '?')}** — "
                                            f"`{_r.get('symbol', '?')}`: {_desc}\n"
                                            f"```\n{_r.get('new_code', '')}\n```"
                                        )
                                    _parts.append(
                                        "\nRe-send the prompt to finish the "
                                        "remaining work, or apply these manually."
                                    )
                                    fallback_text = (
                                        fallback_text + "".join(_parts)
                                    ).strip()
                                    _dlog("safety_net_checkpoint_recovered",
                                          session_id=session_id,
                                          user_id=current_user_id,
                                          recovered_edits=len(_rec),
                                          recovered_chars=sum(
                                              len(_r.get("new_code", ""))
                                              for _r in _rec
                                          ))
                            except Exception as _ckpt_parse_err:
                                print(f"[STREAM] Checkpoint recovery failed: {_ckpt_parse_err}")
                        if fallback_text:
                            db.execute(
                                "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
                                (resp_id, session_id, "assistant", fallback_text)
                            )
                            db.execute(
                                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (session_id,)
                            )
                            db.commit()
                            print(f"[STREAM] Safety-net save: {len(fallback_text)} chars for session {session_id}")
                except Exception as _fallback_err:
                    print(f"[STREAM] Safety-net save failed: {_fallback_err}")

    return StreamingResponse(stream_and_save(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ──────────────────────────────────────────────────────────────────────────
# Per-task execution (v1.4)
#
# Task planning/creation happens in /smart-stream, which now ends right after
# persisting the plan. The client then calls /execute-task once per task, in
# sequence. Each task runs in its own short-lived SSE stream, so no single
# connection approaches the proxy/process time limit that previously killed
# long multi-task runs (a task dying now costs one task, not the whole run).
# ──────────────────────────────────────────────────────────────────────────

_VERDICT_ORDER = {"safe": 0, "skipped": 0, "verified_safe": 0, "warning": 1, "blocked": 2}


def _eval_task_result(parsed):
    """Return (min_qa_score, worst_verdict) across all changes in a result."""
    scores, worst = [], "safe"
    for _fd in (parsed or {}).get("changes_by_file", {}).values():
        for _ch in (_fd.get("changes", []) if isinstance(_fd, dict) else []):
            _qr = _ch.get("qa_result") or {}
            _s = _qr.get("qa_score")
            if isinstance(_s, (int, float)):
                scores.append(int(_s))
            _v = _qr.get("verdict", "")
            if _VERDICT_ORDER.get(_v, 0) > _VERDICT_ORDER.get(worst, 0):
                worst = _v
    return (min(scores) if scores else None, worst)



def _extract_edit_summary(parsed):
    """Extract a compact 'files -> symbols' summary from a parsed smart_result."""
    if not parsed:
        return ""
    parts = []
    for fname, fdata in (parsed.get("changes_by_file") or {}).items():
        symbols = []
        if isinstance(fdata, dict):
            for ch in fdata.get("changes", []):
                sym = ch.get("symbol") or {}
                name = sym.get("name") or sym.get("full_path", "")
                if name:
                    symbols.append(name)
        if symbols:
            parts.append(f"  {fname}: {', '.join(symbols)}")
        else:
            parts.append(f"  {fname}")
    return "\n".join(parts) if parts else ""


def _build_prior_work_context(session_id, run_id, current_seq):
    """Build a context block summarising what earlier tasks in this run already produced."""
    from services.task_planner import list_tasks as _lt
    tasks = _lt(session_id, run_id)
    done = [t for t in tasks if t.get("status") == "done" and t.get("seq", 999) < current_seq]
    if not done:
        _dlog("prior_work_empty", session_id=session_id, run_id=run_id, current_seq=current_seq)
        return ""
    lines = [
        "[PRIOR COMPLETED TASKS IN THIS RUN — do NOT redo work that is already done]",
        "",
    ]
    for t in sorted(done, key=lambda x: x.get("seq", 0)):
        summary = (t.get("result_summary") or "").strip()
        lines.append(f"Task {t['seq']+1}: {t['title']}")
        if summary:
            lines.append(f"  Result: {summary}")
        lines.append("")
    lines.append(
        "If a file or symbol listed above was already modified, "
        "build on that work — do NOT rewrite it from scratch."
    )
    ctx = "\n".join(lines)
    _dlog("prior_work_built", session_id=session_id, run_id=run_id,
          current_seq=current_seq, prior_task_count=len(done),
          context_len=len(ctx))
    return ctx


def _save_task_message(session_id, natural_text, parsed):
    """Persist a task's assistant message (natural text + optional diff result)."""
    with get_db_ctx() as db:
        rid = str(uuid.uuid4())
        if parsed:
            qa_warnings = []
            for _fd in parsed.get("changes_by_file", {}).values():
                for _ch in (_fd.get("changes", []) if isinstance(_fd, dict) else []):
                    _qr = _ch.get("qa_result") or {}
                    _vd = _qr.get("verdict", "")
                    _sm = (_qr.get("summary") or "").strip()
                    _sc = _qr.get("qa_score")
                    if _vd in ("warning", "blocked") and _sm:
                        _ic = "⚠️" if _vd == "warning" else "🚫"
                        _sy = (_ch.get("symbol") or {})
                        _nm = _sy.get("name") or _sy.get("full_path", "change")
                        qa_warnings.append(f"{_ic} **{_nm}** (QA {_sc}/10): {_sm}")
            _txt = natural_text
            if qa_warnings:
                _txt = (_txt + ("\n\n" if _txt else "") + "**QA Notes:**\n"
                        + "\n".join(f"- {w}" for w in qa_warnings))
            saved = "__NATURAL_AND_RESULT__:" + json.dumps({"text": _txt, "result": parsed})
        else:
            saved = natural_text
        db.execute(
            "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (rid, session_id, "assistant", saved),
        )
        db.execute("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
        db.commit()


def _save_run_note(session_id, text):
    """Persist a plain assistant note (run summary lines)."""
    with get_db_ctx() as db:
        rid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (rid, session_id, "assistant", text),
        )
        db.execute("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
        db.commit()


def _run_counts(session_id, run_id):
    from services.task_planner import list_tasks
    tasks = list_tasks(session_id, run_id)
    return {
        "completed": sum(1 for t in tasks if t.get("status") == "done"),
        "total": len(tasks),
    }


def _run_summary_note(session_id, run_id, status, blocked_title=None, reason="qa"):
    c = _run_counts(session_id, run_id)
    completed, total = c["completed"], c["total"]
    if status == "cancelled":
        return (f"⛔ Task run cancelled. Completed {completed} of {total} task(s); "
                f"the remaining were cancelled.")
    if status == "blocked":
        # Honest wording — a task pauses the run either because QA returned a
        # "blocked" verdict (hard issues found) or because execution errored.
        # There is no numeric 8/10 gate at the task level.
        _dlog("run_summary_note_blocked", session_id=session_id, run_id=run_id,
              blocked_title=blocked_title, reason=reason,
              completed=completed, total=total)
        if reason == "error":
            return (f"🚫 Task run paused. Completed {completed} of {total}. "
                    f"Task “{blocked_title}” hit an error, "
                    f"so the remaining tasks were halted for your review.")
        return (f"🚫 Task run paused. Completed {completed} of {total}. "
                f"QA flagged blocking issues in task “{blocked_title}”, "
                f"so the remaining tasks were halted for your review.")
    return f"✅ Completed all {total} task(s)."


@router.post("/execute-task")
async def execute_task(req: dict, request: Request):
    """
    Execute a single planned task in its own short-lived SSE stream.

    Body: { session_id, run_id, task_id }
    Emits the same task lifecycle events the old in-stream loop did
    (task_start / task_progress / smart_result / task_done | task_blocked |
    task_cancelled), plus tasks_complete when the run reaches a terminal state.
    """
    from fastapi.responses import StreamingResponse
    from services.pipeline import run_natural_pipeline_stream
    from services.task_planner import (
        update_task, list_tasks, cancel_requested_for_run, mark_pending_cancelled,
    )

    session_id = req.get("session_id")
    run_id = req.get("run_id")
    task_id = req.get("task_id")
    current_user_id = getattr(request.state, "user_id", "") or ""

    # Load fresh session state. Reloading per task means each task sees edits
    # and the compacted summary persisted by prior tasks in the run.
    with get_db_ctx() as conn:
        sess_row = conn.execute(
            "SELECT session_summary FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        session_summary = (sess_row["session_summary"] if sess_row and sess_row["session_summary"] else "") or ""
        history = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id = ? AND is_compacted = 0 ORDER BY created_at ASC",
            (session_id,)
        ).fetchall()
        conversation_history = [{"role": r["role"], "content": r["content"]} for r in history]
        file_rows = conn.execute(
            "SELECT id, filename, content, language, lines, symbol_count, file_type FROM session_files WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,)
        ).fetchall()
        session_files = [dict(r) for r in file_rows]
        project_memory = _load_effective_memory(conn, session_id)

    async def stream_one_task():
        _t0 = time.time()

        def _sse(obj):
            return "data: " + json.dumps(obj) + "\n\n"

        all_tasks = list_tasks(session_id, run_id)
        task = next((t for t in all_tasks if t["id"] == task_id), None)
        total = len(all_tasks)

        if task is None:
            _dlog("sse_exec_task_missing", session_id=session_id, user_id=current_user_id,
                  task_id=task_id, run_id=run_id)
            yield _sse({"type": "error", "content": "Task not found."})
            yield _sse({"type": "done"})
            return

        seq = task.get("seq", 0)
        _phase = f"exec_task:{seq+1}/{total}:{str(task_id)[:8]}"

        # Cancellation requested before this task ran → stop the whole run.
        if cancel_requested_for_run(session_id, run_id) or task.get("status") == "cancelled":
            update_task(task_id, status="cancelled")
            mark_pending_cancelled(session_id, run_id)
            _dlog("sse_exec_task_done", session_id=session_id, user_id=current_user_id,
                  task_id=task_id, task_seq=seq+1, status="cancelled",
                  duration_s=round(time.time() - _t0, 1))
            yield _sse({"type": "task_cancelled", "id": task_id})
            note = _run_summary_note(session_id, run_id, status="cancelled")
            _save_run_note(session_id, note)
            yield _sse({"type": "tasks_complete", "status": "cancelled",
                        **_run_counts(session_id, run_id), "summary": note})
            yield _sse({"type": "done"})
            return

        # ── Idempotency guard: only pending tasks may execute ───────────────
        # A task already running/done/blocked must never re-run — re-executing
        # would re-apply its edits (two open tabs, a retried stream after a
        # connection drop, etc.). The client reconciles the real status via
        # GET /tasks whenever a stream ends without a terminal task event.
        _cur_status = task.get("status")
        if _cur_status != "pending":
            _dlog("sse_exec_task_not_pending", session_id=session_id, user_id=current_user_id,
                  task_id=task_id, task_seq=seq+1, run_id=run_id, status=_cur_status)
            yield _sse({"type": "task_skipped", "id": task_id, "status": _cur_status})
            yield _sse({"type": "done"})
            return

        _dlog("sse_exec_task_start", session_id=session_id, user_id=current_user_id,
              task_id=task_id, task_seq=seq+1, task_total=total, title=task["title"][:80])
        update_task(task_id, status="running")
        # Disconnect guard: mark that THIS stream owns the running status, so
        # the wrapper below knows it may safely reset it if the client drops.
        _guard_state["owns_running"] = True
        yield _sse({"type": "task_start", "id": task_id})

        # --- Prior-work context injection (Issue 2 fix) ---
        try:
            _prior_ctx = _build_prior_work_context(session_id, run_id, seq)
        except Exception as _pex:
            _dlog("prior_work_error", session_id=session_id, task_id=task_id,
                  error=str(_pex)[:200])
            _prior_ctx = ""
        _task_request = (_prior_ctx + "\n\n" + task["detail"]) if _prior_ctx else task["detail"]

        # ── No-files guardrail ────────────────────────────────────────
        # If task is code-type but no files exist in this session, tell
        # the model explicitly so it can ask the user to upload files
        # instead of silently producing zero edits.
        _task_kind = task.get("kind", "code")
        if not session_files and _task_kind == "code":
            _task_request = (
                "IMPORTANT: There are NO files uploaded in this session. "
                "You cannot make code edits without files. Tell the user "
                "to upload the relevant file(s) first, then retry.\n\n"
                + _task_request
            )
            _dlog("exec_task_no_files", session_id=session_id, task_id=task_id,
                  task_seq=seq+1, task_kind=_task_kind)

        # File-awareness hint for task execution (mirrors smart-stream hint)
        if session_files:
            _fnames = [f['filename'] for f in session_files]
            _file_hint = (f"[{len(_fnames)} file(s) attached: {', '.join(_fnames)}. "
                          "Examine their contents before responding.]")
            _task_request = f"{_file_hint}\n\n{_task_request}"

        _dlog("sse_exec_task_context", session_id=session_id, task_id=task_id,
              task_seq=seq+1, has_prior_ctx=bool(_prior_ctx),
              has_files=bool(session_files), num_files=len(session_files),
              request_len=len(_task_request))

        collected, result_content, poll, aborted = [], None, 0, False
        _think_parts: list = []  # accumulated extended-thinking text for this task
        try:
            async for chunk in _with_heartbeat(run_natural_pipeline_stream(
                session_files=session_files,
                user_request=_task_request,
                conversation_history=conversation_history,
                session_id=session_id,
                project_memory=project_memory,
                session_summary=session_summary,
                user_id=current_user_id,
            )):
                poll += 1
                if poll % 20 == 0 and cancel_requested_for_run(session_id, run_id):
                    aborted = True
                    break
                if chunk.startswith(": "):
                    yield chunk  # forward keepalive comment to client
                    continue
                if chunk.startswith("data: "):
                    try:
                        _d = json.loads(chunk[6:])
                        _ct = _d.get("type", "")
                        if _ct in ("token", "chat"):
                            collected.append(_d.get("content", ""))
                        elif _ct == "smart_result":
                            result_content = _d.get("content", "")
                        elif _ct == "progress":
                            yield _sse({"type": "task_progress", "id": task_id, "content": _d.get("content", "")})
                        elif _ct == "thinking":
                            # Forward the model's extended thinking so the UI can
                            # show a per-task expandable reasoning trail (parity
                            # with chat mode). Previously these events were
                            # silently dropped by this filter.
                            _tc = _d.get("content", "")
                            if _tc:
                                _think_parts.append(_tc)
                                yield _sse({"type": "task_thinking", "id": task_id, "content": _tc})
                        elif _ct == "error":
                            yield _sse({"type": "task_progress", "id": task_id, "content": "⚠️ " + _d.get("content", "")})
                    except Exception:
                        pass
        except Exception as _ee:
            _dlog("sse_exec_task_error", session_id=session_id, user_id=current_user_id,
                  task_id=task_id, task_seq=seq+1, phase=_phase,
                  duration_s=round(time.time() - _t0, 1), error=str(_ee))
            update_task(task_id, status="blocked", verdict="error",
                        result_summary=("".join(collected))[:500],
                        thinking=("".join(_think_parts))[:24000])
            mark_pending_cancelled(session_id, run_id)
            yield _sse({"type": "task_blocked", "id": task_id, "qa_score": None, "verdict": "error"})
            note = _run_summary_note(session_id, run_id, status="blocked", blocked_title=task["title"], reason="error")
            _save_run_note(session_id, note)
            yield _sse({"type": "tasks_complete", "status": "blocked",
                        **_run_counts(session_id, run_id), "summary": note})
            yield _sse({"type": "done"})
            return

        if aborted:
            update_task(task_id, status="cancelled")
            mark_pending_cancelled(session_id, run_id)
            _dlog("sse_exec_task_done", session_id=session_id, user_id=current_user_id,
                  task_id=task_id, task_seq=seq+1, status="cancelled",
                  duration_s=round(time.time() - _t0, 1))
            yield _sse({"type": "task_cancelled", "id": task_id})
            note = _run_summary_note(session_id, run_id, status="cancelled")
            _save_run_note(session_id, note)
            yield _sse({"type": "tasks_complete", "status": "cancelled",
                        **_run_counts(session_id, run_id), "summary": note})
            yield _sse({"type": "done"})
            return

        natural_text = "".join(collected).strip()
        parsed = None
        if result_content:
            try:
                parsed = json.loads(result_content)
            except Exception:
                parsed = None
        score, worst = _eval_task_result(parsed) if parsed else (None, "safe")
        _save_task_message(session_id, natural_text, parsed)

        # Non-code ("answer") tasks edit nothing → skip the QA verdict gate.
        _is_answer = task.get("kind") == "answer"
        if parsed and worst == "blocked" and not _is_answer:
            update_task(task_id, status="blocked", qa_score=score,
                        verdict=worst, result_summary=natural_text[:500],
                        thinking=("".join(_think_parts))[:24000])
            mark_pending_cancelled(session_id, run_id)
            _dlog("sse_exec_task_done", session_id=session_id, user_id=current_user_id,
                  task_id=task_id, task_seq=seq+1, status="blocked",
                  qa_score=score, duration_s=round(time.time() - _t0, 1))
            yield _sse({"type": "task_blocked", "id": task_id, "qa_score": score, "verdict": worst})
            note = _run_summary_note(session_id, run_id, status="blocked", blocked_title=task["title"], reason="qa")
            _save_run_note(session_id, note)
            yield _sse({"type": "tasks_complete", "status": "blocked",
                        **_run_counts(session_id, run_id), "summary": note})
            yield _sse({"type": "done"})
            return

        # Honest QA status: a code-kind task that produced zero edits never
        # went through the QA gate, so it must not surface as a "safe" pass.
        _has_edits = False
        if parsed:
            for _fd in (parsed.get("changes_by_file") or {}).values():
                if isinstance(_fd, dict) and _fd.get("changes"):
                    _has_edits = True
                    break
        if _is_answer:
            _verdict = "skipped"
        elif not _has_edits:
            _verdict = "no_edits"
            _dlog("sse_exec_task_no_edits", session_id=session_id,
                  task_id=task_id, task_seq=seq+1, had_parsed=bool(parsed))
        else:
            _verdict = worst or "safe"
        _score = None if _is_answer else score
        _edit_summary = _extract_edit_summary(parsed) if parsed else ""
        _rsummary = natural_text[:500]
        if _edit_summary:
            _rsummary += "\nEdited:\n" + _edit_summary
            _dlog("sse_exec_task_edit_summary", session_id=session_id,
                  task_id=task_id, edit_summary=_edit_summary[:300])
        update_task(task_id, status="done", qa_score=_score,
                    verdict=_verdict, result_summary=_rsummary[:800],
                    thinking=("".join(_think_parts))[:24000])
        _dlog("sse_exec_task_done", session_id=session_id, user_id=current_user_id,
              task_id=task_id, task_seq=seq+1, status="done",
              qa_score=_score, verdict=_verdict, duration_s=round(time.time() - _t0, 1))
        if parsed and not _is_answer:
            _rp = dict(parsed)
            _rp["natural_text"] = natural_text
            yield _sse({"type": "smart_result", "content": json.dumps(_rp)})
        yield _sse({"type": "task_done", "id": task_id, "qa_score": _score, "verdict": _verdict})

        # Run is complete once no pending tasks remain.
        remaining = [t for t in list_tasks(session_id, run_id) if t.get("status") == "pending"]
        if not remaining:
            note = _run_summary_note(session_id, run_id, status="done")
            _save_run_note(session_id, note)
            yield _sse({"type": "tasks_complete", "status": "done",
                        **_run_counts(session_id, run_id), "summary": note})

        _dlog("sse_exec_stream_done", session_id=session_id, user_id=current_user_id,
              task_id=task_id, task_seq=seq+1, duration_s=round(time.time() - _t0, 1))
        yield _sse({"type": "done"})

    # Shared flag: set by stream_one_task once it flips the task to "running".
    # Lets the disconnect guard distinguish a task WE started (safe to reset)
    # from one another stream owns (guard-skipped duplicate — never touch it).
    _guard_state = {"owns_running": False}

    async def stream_with_disconnect_guard():
        # If the client vanishes mid-task (tab close, refresh, network drop),
        # this generator is closed at the current yield via GeneratorExit /
        # CancelledError — BaseExceptions the pipeline's `except Exception`
        # can never catch. Without this guard the task would be orphaned in
        # status="running" forever and the run could never be resumed.
        try:
            async for _chunk in stream_one_task():
                yield _chunk
        finally:
            try:
                if _guard_state["owns_running"]:
                    _rows = list_tasks(session_id, run_id)
                    _row = next((t for t in _rows if t["id"] == task_id), None)
                    if _row and _row.get("status") == "running":
                        update_task(task_id, status="pending")
                        _dlog("sse_exec_task_orphan_reset", session_id=session_id,
                              user_id=current_user_id, task_id=task_id,
                              from_status="running", to_status="pending")
            except Exception as _ox:
                _dlog("sse_exec_task_orphan_reset_error", session_id=session_id,
                      task_id=task_id, error=str(_ox)[:200])

    return StreamingResponse(stream_with_disconnect_guard(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
