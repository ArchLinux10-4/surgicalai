"""Chat router — standard AI chat with file context."""
import uuid
import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from models.schemas import ChatRequest, NewSessionRequest, ChatSession
from database import get_db, get_setting
from services.pipeline import run_chat, run_chat_stream

router = APIRouter()


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

        turns_text = "\n".join([
            f"{dict(r).get('role','user').upper()}: {str(dict(r).get('content',''))[:600]}"
            for r in to_compact
        ])
        prompt_parts = []
        if existing:
            prompt_parts.append(f"Previous summary:\n{existing}\n")
        prompt_parts.append(f"New conversation turns to add:\n{turns_text}")

        openai_key = get_setting("openai_api_key") or (get_user_api_key(user_id, "openai") if user_id else "")
        if not openai_key:
            db.close()
            return existing

        client = _openai_mod.AsyncOpenAI(api_key=openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": (
                    "Summarize this coding assistant conversation history. "
                    "Focus on: what files were discussed, what code changes were made or planned, "
                    "key decisions, and any patterns or conventions established. "
                    "Be concise but complete. Under 400 words. Use bullet points."
                )},
                {"role": "user", "content": "\n".join(prompt_parts)}
            ],
            max_tokens=500,
        )
        new_summary = resp.choices[0].message.content or ""

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
        if d.get("content", "").startswith("__SURGICAL_RESULT__:"):
            d["message_type"] = "surgical_result"
            d["surgical_data"] = d["content"][len("__SURGICAL_RESULT__:"):]
            d["content"] = ""  # don't render the raw JSON as text
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
    workspace = session_id  # Use session_id as workspace for per-session memory
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

    memory_row = conn.execute(
        "SELECT content FROM project_memory WHERE workspace_path = ? LIMIT 1", (workspace,)
    ).fetchone() if workspace else None
    project_memory = memory_row["content"] if memory_row else None

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
    workspace = session_id  # Use session_id as workspace for per-session memory
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

    memory_row = conn.execute(
        "SELECT content FROM project_memory WHERE workspace_path = ? LIMIT 1", (workspace,)
    ).fetchone() if workspace else None
    project_memory = memory_row["content"] if memory_row else None

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
    from services.pipeline import run_smart_pipeline_stream

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

    # Project memory — keyed by session_id for cloud users (workspacePath is per-session)
    memory_row = conn.execute(
        "SELECT content FROM project_memory WHERE workspace_path = ? LIMIT 1", (session_id,)
    ).fetchone() if session_id else None
    project_memory = memory_row["content"] if memory_row else None

    conn.commit()
    conn.close()

    async def stream_and_save():
        collected_tokens = []
        result_content = None
        current_summary = session_summary

        # --- Rolling compaction ---
        if needs_compaction:
            yield "data: " + __import__("json").dumps({"type": "compacting", "content": "Compacting conversation history..."}) + "\n\n"
            try:
                new_sum = await _compact_session(session_id, current_user_id)
                if new_sum:
                    current_summary = new_sum
            except Exception as _ce:
                print(f"[compact] failed: {_ce}")
            yield "data: " + __import__("json").dumps({"type": "compacting_done", "content": "History compacted"}) + "\n\n"

        async for chunk in run_smart_pipeline_stream(
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
                    import json as _json
                    data = _json.loads(chunk[6:])
                    if data.get("type") == "token":
                        collected_tokens.append(data.get("content", ""))
                    elif data.get("type") == "smart_result":
                        result_content = data.get("content", "")
                    elif data.get("type") == "done":
                        # Save assistant message
                        db = get_db()
                        resp_id = str(uuid.uuid4())
                        if result_content:
                            # Store surgical result reference
                            import json as _j
                            res = _j.loads(result_content)
                            saved_content = f"__SURGICAL_RESULT__:{result_content}"
                        else:
                            saved_content = "".join(collected_tokens)
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
