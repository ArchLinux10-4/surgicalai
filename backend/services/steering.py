"""Mid-run steering. A follow-up sent while the agent works lands on its own
queue, separate from the approval inbox, and is attached at the next tool
boundary.

The approval waiters drain ``client_inbox`` with ``get_nowait()`` before they
block. A steer message on that queue would be thrown away. ``steer_inbox`` is
only read here.
"""
from __future__ import annotations

import json
import uuid

STEER_MAX_PER_DRAIN = 5
STEER_MAX_CHARS = 4000

CLIENT_MESSAGE_TYPES = frozenset({
    "file_response",
    "aws_cli_response",
    "git_cli_response",
    "local_cli_response",
    "preview_check_response",
    "mcp_call_response",
})

_INTRO = (
    "The user sent a follow-up while you were working. "
    "Treat it as updated instructions and adapt:"
)


def route_ws_message(msg) -> str | None:
    """Which inbox a client frame belongs in. Unknown shapes are dropped."""
    if not isinstance(msg, dict):
        return None
    kind = msg.get("type")
    if kind == "steer":
        return "steer"
    if kind in CLIENT_MESSAGE_TYPES:
        return "client"
    return None


def steer_ready_event() -> dict:
    return {"type": "steer_ready"}


def steer_closed_event() -> dict:
    return {"type": "steer_closed"}


def drain_steer(steer_inbox, limit: int | None = STEER_MAX_PER_DRAIN) -> list[dict]:
    """Pull pending steer frames. Empty text is ignored. Text is capped.

    ``limit`` is the most items one model turn should absorb. ``None`` drains
    whatever is left (used when the run is over and nothing was applied).
    ``steer_inbox is None`` (HTTP/SSE, offline) is a no-op.
    """
    if steer_inbox is None:
        return []
    items: list[dict] = []
    while limit is None or len(items) < limit:
        try:
            msg = steer_inbox.get_nowait()
        except Exception:
            break
        if not isinstance(msg, dict) or msg.get("type") != "steer":
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        if len(text) > STEER_MAX_CHARS:
            text = text[:STEER_MAX_CHARS]
        items.append({
            "id": str(msg.get("id") or uuid.uuid4()),
            "text": text,
        })
    return items


def format_steer_block(items: list[dict]) -> str:
    if not items:
        return ""
    parts = [_INTRO]
    parts.extend(item["text"] for item in items if item.get("text"))
    return "\n\n".join(parts)


def _applied_events(items: list[dict]) -> list[dict]:
    return [
        {"type": "steer_applied", "id": item["id"], "text": item["text"]}
        for item in items
    ]


def flush_unapplied(steer_inbox) -> list[dict]:
    """Everything still waiting after the last safe point. Not persisted."""
    return [
        {"type": "steer_unapplied", "id": item["id"], "text": item["text"]}
        for item in drain_steer(steer_inbox, limit=None)
    ]


def persist_applied_steer(session_id: str, items: list[dict]) -> None:
    """Store each applied follow-up as a user message so a reload shows it."""
    if not session_id or not items:
        return
    try:
        from database import get_db_ctx
        with get_db_ctx() as conn:
            for item in items:
                conn.execute(
                    "INSERT INTO chat_messages (id, session_id, role, content, metadata) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        session_id,
                        "user",
                        item["text"],
                        json.dumps({"steered": True}),
                    ),
                )
            conn.commit()
    except Exception:
        return


def take_steer(steer_inbox, session_id: str = "") -> tuple[str, list[dict]]:
    """Drain one boundary's worth, persist it, and return (block, SSE events)."""
    items = drain_steer(steer_inbox)
    if not items:
        return "", []
    persist_applied_steer(session_id, items)
    return format_steer_block(items), _applied_events(items)


def fold_steer_into_messages(messages: list, block: str) -> list:
    """Attach ``block`` to the latest user turn, or add one.

    A string user turn grows. An Anthropic content-block user turn gains a
    text block after the tool results, so roles still alternate. A turn that
    ends on an assistant or tool message gets a new user message.
    """
    if not block:
        return messages
    if not messages:
        return [{"role": "user", "content": block}]
    last = messages[-1]
    if not isinstance(last, dict):
        return list(messages) + [{"role": "user", "content": block}]
    content = last.get("content")
    if last.get("role") == "user" and isinstance(content, str):
        updated = dict(last)
        updated["content"] = f"{content}\n\n{block}" if content else block
        return list(messages[:-1]) + [updated]
    if last.get("role") == "user" and isinstance(content, list):
        updated = dict(last)
        updated["content"] = list(content) + [{"type": "text", "text": block}]
        return list(messages[:-1]) + [updated]
    return list(messages) + [{"role": "user", "content": block}]


def apply_steer_at_boundary(messages: list, steer_inbox, session_id: str = "") -> tuple[list, list[dict]]:
    """One tool boundary. A second call in the same turn finds the inbox empty."""
    block, events = take_steer(steer_inbox, session_id)
    if not block:
        return messages, []
    return fold_steer_into_messages(messages, block), events


def attach_steer_to_tool_results(results: list, steer_inbox, session_id: str = "") -> tuple[list, list[dict]]:
    """Anthropic tool_result list: the follow-up is a text block in the same turn."""
    block, events = take_steer(steer_inbox, session_id)
    if not block:
        return results, []
    return list(results) + [{"type": "text", "text": block}], events
