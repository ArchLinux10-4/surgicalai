"""Chat router — standard AI chat with file context."""
import uuid
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from models.schemas import ChatRequest, NewSessionRequest, ChatSession
from database import get_db, get_setting
from services.pipeline import run_chat, run_chat_stream

router = APIRouter()


@router.post("/sessions")
def create_session(req: NewSessionRequest):
    session_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO chat_sessions (id, title, file_path, model) VALUES (?, ?, ?, ?)",
        (session_id, req.title, req.file_path, req.model or get_setting("architect_model", "gpt-4.1"))
    )
    conn.commit()
    conn.close()
    return {"id": session_id, "title": req.title}


@router.get("/sessions")
def list_sessions():
    conn = get_db()
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
    return [dict(r) for r in rows]


@router.post("/send")
def send_message(req: ChatRequest):
    if not get_setting("openai_api_key") and get_setting("ollama_enabled") != "true":
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
    workspace = get_setting("workspace_path", "")
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
    if not get_setting("openai_api_key") and get_setting("ollama_enabled") != "true":
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
    workspace = get_setting("workspace_path", "")
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
