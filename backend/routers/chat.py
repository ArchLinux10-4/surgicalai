"""Chat router — standard AI chat with file context."""
import asyncio
import uuid
import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from models.schemas import ChatRequest, NewSessionRequest, ChatSession
from database import get_db, get_setting, get_user_api_key, GLOBAL_MEMORY_KEY
from services.pipeline import run_chat, run_chat_stream

router = APIRouter()


async def _with_heartbeat(aiter, interval: int = 20):
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
        except (asyncio.CancelledError, Exception):
            pass


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
    conn = get_db()
    conn.execute(
        "INSERT INTO chat_sessions (id, title, file_path, model, user_id) VALUES (?, ?, ?, ?, ?)",
        (session_id, req.title, req.file_path, req.model or get_setting("architect_model", "gpt-4.1"), user_id)
    )
    conn.commit()
    conn.close()
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
    conn = get_db()
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

async def _compact_session(session_id: str, user_id: str) -> str:
    """
    Compact oldest 14 uncompacted messages into session_summary.
    Uses GPT-4.1-mini (fast + cheap).
    Returns the new summary string.
    """
    db = get_db()
    try:
        to_compact = db.execute(
            "SELECT id, role, content FROM chat_messages "
            "WHERE session_id = ? AND is_compacted = 0 "
            "ORDER BY created_at ASC LIMIT 14",
            (session_id,)
        ).fetchall()
        if not to_compact:
            db.close()
            return ""

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
            db.close()
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
        db.close()
        return new_summary
    except Exception as exc:
        try:
            db.rollback()
            db.close()
        except Exception:
            pass
        print(f"[compact] Error: {exc}")
        return ""


@router.get("/sessions")
def list_sessions(request: Request):
    user_id = getattr(request.state, "user_id", None)
    conn = get_db()
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
    conn.close()
    return [dict(r) for r in rows]


@router.get("/sessions/{session_id}/messages")
def get_messages(session_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,)
    ).fetchall()
    conn.close()
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

    conn = get_db()

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
    conn.close()

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
    conn = get_db()
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
    conn.close()

    return {"id": resp_id, "role": "assistant", "content": response}


@router.post("/stream")
async def stream_message(req: ChatRequest):
    """Streaming version of chat send. Returns SSE stream."""
    if not get_setting("openai_api_key") and not get_setting("anthropic_api_key") and get_setting("ollama_enabled") != "true":
        raise HTTPException(status_code=401, detail="No AI backend configured.")

    conn = get_db()

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
    conn.close()

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
                        db = get_db()
                        db.execute(
                            "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
                            (resp_id, session_id, "assistant", full_text)
                        )
                        db.execute(
                            "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (session_id,)
                        )
                        db.commit()
                        db.close()
                except Exception:
                    pass
            yield chunk

    return StreamingResponse(stream_and_collect(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    conn = get_db()
    # Cascade: delete files and messages before session row
    conn.execute("DELETE FROM session_files WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@router.patch("/sessions/{session_id}")
def rename_session(session_id: str, body: dict):
    title = body.get("title", "")
    if not title:
        raise HTTPException(status_code=400, detail="Title required")
    conn = get_db()
    conn.execute("UPDATE chat_sessions SET title = ? WHERE id = ?", (title, session_id))
    conn.commit()
    conn.close()
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
    current_user_id = getattr(request.state, "user_id", "") or ""

    # Check per-user encrypted keys AND global settings
    from database import get_user_api_key
    has_openai = bool(get_setting("openai_api_key")) or bool(get_user_api_key(current_user_id, "openai") if current_user_id else "")
    has_anthropic = bool(get_setting("anthropic_api_key")) or bool(get_user_api_key(current_user_id, "anthropic") if current_user_id else "")
    if not has_openai and not has_anthropic and get_setting("ollama_enabled") != "true":
        raise HTTPException(status_code=401, detail="No AI backend configured. Go to Settings.")

    conn = get_db()

    # Save user message
    msg_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
        (msg_id, session_id, "user", message)
    )

    # Check if compaction is needed (>= 20 uncompacted messages)
    uc_count = conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE session_id = ? AND is_compacted = 0",
        (session_id,)
    ).fetchone()
    needs_compaction = int(list(uc_count.values())[0] if hasattr(uc_count, 'values') else uc_count[0]) >= 20

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

    # Load session files
    file_rows = conn.execute(
        "SELECT id, filename, content, language, lines, symbol_count, file_type FROM session_files WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,)
    ).fetchall()
    session_files = [dict(r) for r in file_rows]

    # Project memory — GLOBAL team conventions (every prompt) + any per-session memory
    project_memory = _load_effective_memory(conn, session_id)

    conn.commit()
    conn.close()

    async def stream_and_save():
        import json as _json

        collected_tokens = []
        result_content = None
        current_summary = session_summary

        # --- Rolling compaction ---
        if needs_compaction:
            yield "data: " + _json.dumps({"type": "compacting", "content": "Compacting conversation history..."}) + "\n\n"
            try:
                new_sum = await _compact_session(session_id, current_user_id)
                if new_sum:
                    current_summary = new_sum
            except Exception as _ce:
                print(f"[compact] failed: {_ce}")
            yield "data: " + _json.dumps({"type": "compacting_done", "content": "History compacted"}) + "\n\n"

        # Use natural pipeline (Claude talks naturally, embeds <surgical_edit> tags)
        # Fall back to legacy pipeline for non-Claude models
        from database import get_setting as _gs
        _arch_model = _gs("architect_model", "gpt-4.1")
        _use_natural = _arch_model.startswith("claude-")

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

        if _use_natural and wants_task_breakdown(message):
            yield _sse({"type": "progress", "content": "Planning tasks..."})
            try:
                _plan = await plan_tasks(message, session_files, current_user_id)
            except Exception as _pe:
                print(f"[tasks] planning failed: {_pe}")
                _plan = {"tasks": []}

            _planned = _plan.get("tasks", []) or []
            if len(_planned) >= 2:
                # ---- helpers (closure over session_id) ----
                _VERDICT_ORDER = {"safe": 0, "skipped": 0, "verified_safe": 0, "warning": 1, "blocked": 2}

                def _eval_result(parsed):
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

                def _save_task_message(natural_text, parsed):
                    db = get_db()
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
                        saved = "__NATURAL_AND_RESULT__:" + _json.dumps({"text": _txt, "result": parsed})
                    else:
                        saved = natural_text
                    db.execute(
                        "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
                        (rid, session_id, "assistant", saved),
                    )
                    db.execute("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
                    db.commit()
                    db.close()

                def _save_note(text):
                    db = get_db()
                    rid = str(uuid.uuid4())
                    db.execute(
                        "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
                        (rid, session_id, "assistant", text),
                    )
                    db.execute("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
                    db.commit()
                    db.close()

                run_id = str(uuid.uuid4())
                tasks = create_tasks(session_id, run_id, _planned)
                yield _sse({
                    "type": "task_plan",
                    "run_id": run_id,
                    "preamble": _plan.get("preamble", ""),
                    "tasks": [
                        {"id": t["id"], "seq": t["seq"], "title": t["title"],
                         "detail": t["detail"], "kind": t.get("kind", "code"),
                         "status": "pending"}
                        for t in tasks
                    ],
                })

                completed, blocked_task, cancelled = 0, None, False
                total = len(tasks)

                for t in tasks:
                    if cancel_requested_for_run(session_id, run_id):
                        cancelled = True
                        break
                    update_task(t["id"], status="running")
                    yield _sse({"type": "task_start", "id": t["id"]})

                    collected, result_content, poll, aborted = [], None, 0, False
                    async for chunk in _with_heartbeat(run_natural_pipeline_stream(
                        session_files=session_files,
                        user_request=t["detail"],
                        conversation_history=conversation_history,
                        session_id=session_id,
                        project_memory=project_memory,
                        session_summary=current_summary,
                        user_id=current_user_id,
                    )):
                        poll += 1
                        if poll % 20 == 0 and cancel_requested_for_run(session_id, run_id):
                            aborted = True
                            break
                        if chunk.startswith(": "):
                            yield chunk  # forward SSE keepalive comment to client
                            continue
                        if chunk.startswith("data: "):
                            try:
                                _d = _json.loads(chunk[6:])
                                _ct = _d.get("type", "")
                                if _ct in ("token", "chat"):
                                    collected.append(_d.get("content", ""))
                                elif _ct == "smart_result":
                                    result_content = _d.get("content", "")
                                elif _ct == "progress":
                                    yield _sse({"type": "task_progress", "id": t["id"], "content": _d.get("content", "")})
                                elif _ct == "error":
                                    yield _sse({"type": "task_progress", "id": t["id"], "content": "⚠️ " + _d.get("content", "")})
                            except Exception:
                                pass

                    if aborted:
                        update_task(t["id"], status="cancelled")
                        yield _sse({"type": "task_cancelled", "id": t["id"]})
                        cancelled = True
                        break

                    natural_text = "".join(collected).strip()
                    parsed = None
                    if result_content:
                        try:
                            parsed = _json.loads(result_content)
                        except Exception:
                            parsed = None
                    score, worst = _eval_result(parsed) if parsed else (None, "safe")
                    _save_task_message(natural_text, parsed)

                    # Non-code ("answer") tasks edit nothing, so they skip the
                    # 8/10 QA gate entirely and report a "skipped" verdict.
                    _is_answer = t.get("kind") == "answer"
                    if parsed and worst == "blocked" and not _is_answer:
                        update_task(t["id"], status="blocked", qa_score=score,
                                    verdict=worst, result_summary=natural_text[:500])
                        yield _sse({"type": "task_blocked", "id": t["id"], "qa_score": score, "verdict": worst})
                        blocked_task = t
                        break
                    else:
                        _verdict = "skipped" if _is_answer else (worst or "safe")
                        _score = None if _is_answer else score
                        update_task(t["id"], status="done", qa_score=_score,
                                    verdict=_verdict, result_summary=natural_text[:500])
                        yield _sse({"type": "task_done", "id": t["id"], "qa_score": _score, "verdict": _verdict})
                        completed += 1

                if cancelled:
                    mark_pending_cancelled(session_id, run_id)
                    note = f"⛔ Task run cancelled. Completed {completed} of {total} task(s); the remaining were cancelled."
                    _save_note(note)
                    yield _sse({"type": "tasks_complete", "status": "cancelled",
                                "completed": completed, "total": total, "summary": note})
                elif blocked_task is not None:
                    mark_pending_cancelled(session_id, run_id)
                    note = (f"🚫 Task run paused. Completed {completed} of {total}. "
                            f"Task “{blocked_task['title']}” did not pass the 8/10 QA gate, "
                            f"so the remaining tasks were halted for your review.")
                    _save_note(note)
                    yield _sse({"type": "tasks_complete", "status": "blocked",
                                "completed": completed, "total": total, "summary": note})
                else:
                    note = f"✅ Completed all {total} task(s)."
                    _save_note(note)
                    yield _sse({"type": "tasks_complete", "status": "done",
                                "completed": completed, "total": total, "summary": note})

                yield _sse({"type": "done"})
                return
        # ── End agentic task branch ───────────────────────────────────────

        _pipeline = run_natural_pipeline_stream if _use_natural else run_smart_pipeline_stream

        async for chunk in _pipeline(
            session_files=session_files,
            user_request=message,
            conversation_history=conversation_history,
            session_id=session_id,
            project_memory=project_memory,
            session_summary=current_summary,
            user_id=current_user_id,
        ):
            if chunk.startswith("data: "):
                try:
                    data = _json.loads(chunk[6:])
                    chunk_type = data.get("type", "")
                    if chunk_type in ("token", "chat"):
                        collected_tokens.append(data.get("content", ""))
                    elif chunk_type == "smart_result":
                        result_content = data.get("content", "")
                    elif chunk_type == "done":
                        # Save assistant message — store both natural text AND result
                        db = get_db()
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
                        db.close()
                except Exception:
                    pass
            yield chunk

    return StreamingResponse(stream_and_save(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
