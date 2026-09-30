"""Per-user memory and earlier-session recall.

Facts stay in user_memory. Recall reads session_summary rows for that user
only. Transcripts are not copied. This module does not import routers.chat
at load time.
"""
from __future__ import annotations

import asyncio
import re

from database import _dlog, get_db_ctx

MAX_BULLETS = 25
MAX_MEMORY_CHARS = 4000
MAX_SUMMARY_CHARS = 400
RECALL_LIMIT = 5
RECENT_FALLBACK = 3
CANDIDATE_CAP = 30
_TURN_CAP = 2000

_SECRET_MARKERS = ("sk-", "ghp_", "xox")
_WORD_RE = re.compile(r"[^a-z]+")

_EXTRACT_SYSTEM = (
    "Update the user's durable memory. Return only bullet lines starting with \"- \".\n"
    "Keep a fact only if it would still matter in a different chat next week: stack, conventions, decisions, names, preferences.\n"
    "Drop one-off code, debugging steps, and anything that looks like a key or password.\n"
    "If a new fact contradicts an old bullet, replace the old bullet. 25 bullets maximum."
)


def _tokens(text: str) -> set[str]:
    words = _WORD_RE.split((text or "").lower())
    return {w for w in words if len(w) > 2}


def clip_memory_text(text: str) -> str:
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(marker in low for marker in _SECRET_MARKERS):
            continue
        lines.append(line)
        if len(lines) >= MAX_BULLETS:
            break
    return "\n".join(lines)[:MAX_MEMORY_CHARS]


def recall_lines(rows: list[dict], current_message: str) -> list[str]:
    want = _tokens(current_message)
    scored = []
    for row in rows:
        title = row.get("title") or ""
        summary = row.get("session_summary") or ""
        score = len(want & _tokens(f"{title} {summary}"))
        scored.append((score, row))
    best = max((s for s, _ in scored), default=0)
    if best > 0:
        chosen = [row for score, row in sorted(scored, key=lambda item: item[0], reverse=True) if score > 0][:RECALL_LIMIT]
    else:
        chosen = [row for _, row in scored][:RECENT_FALLBACK]
    lines = []
    for row in chosen:
        title = row.get("title") or "Chat"
        summary = (row.get("session_summary") or "")[:MAX_SUMMARY_CHARS]
        lines.append(f"{title}: {summary}")
    return lines


def load_user_memory_block(user_id: str, session_id: str, current_message: str) -> str:
    if not user_id:
        return ""
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT content, enabled FROM user_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        memory = ""
        if row and int(row["enabled"] or 0) == 1:
            memory = clip_memory_text(row["content"] or "")
        session_rows = conn.execute(
            "SELECT id, title, session_summary FROM chat_sessions "
            "WHERE user_id = ? AND id != ? "
            "AND session_summary IS NOT NULL AND TRIM(session_summary) != '' "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, session_id or "", CANDIDATE_CAP),
        ).fetchall()
    rows = [dict(r) for r in session_rows]
    lines = recall_lines(rows, current_message or "")
    parts = []
    if memory:
        parts.append(
            "USER MEMORY (this user only; earlier chats, not the current transcript):\n"
            + memory
        )
    if lines:
        parts.append(
            "EARLIER SESSIONS (summaries only, this user only):\n"
            + "\n".join(f"- {line}" for line in lines)
        )
    return "\n\n".join(parts)


def attach_user_memory(project_memory, user_id: str, session_id: str, current_message: str):
    try:
        block = load_user_memory_block(user_id, session_id, current_message)
    except Exception as exc:
        _dlog("user_memory_load_failed", user_id=user_id, error=str(exc)[:300])
        return project_memory
    if not block:
        return project_memory
    base = (project_memory or "").rstrip()
    if not base:
        return block
    return base + "\n\n" + block


def schedule_memory_update(user_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
    if not user_id or not (user_text or "").strip() or not (assistant_text or "").strip():
        return
    try:
        with get_db_ctx() as conn:
            row = conn.execute(
                "SELECT enabled FROM user_memory WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is not None and int(row["enabled"] or 0) == 0:
            return
    except Exception as exc:
        _dlog("user_memory_extract_failed", user_id=user_id, error=str(exc)[:300])
        return
    asyncio.create_task(_extract_and_save(user_id, session_id, user_text, assistant_text))


async def _extract_and_save(user_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
    try:
        from routers.chat import _resolve_chat_key

        with get_db_ctx() as conn:
            existing = conn.execute(
                "SELECT content FROM user_memory WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        prior = (existing["content"] if existing else "") or ""
        turn = (
            f"Previous memory:\n{prior}\n\n"
            f"User: {(user_text or '')[:_TURN_CAP]}\n\n"
            f"Assistant: {(assistant_text or '')[:_TURN_CAP]}"
        )
        anthropic_key = _resolve_chat_key(user_id, "anthropic")
        openai_key = _resolve_chat_key(user_id, "openai")
        new_text = ""
        if anthropic_key:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=anthropic_key)
            resp = await client.messages.create(
                model="claude-sonnet-5",
                max_tokens=800,
                system=_EXTRACT_SYSTEM,
                messages=[{"role": "user", "content": turn}],
            )
            new_text = resp.content[0].text if resp.content else ""
        elif openai_key:
            import openai
            client = openai.AsyncOpenAI(api_key=openai_key)
            resp = await client.chat.completions.create(
                model="gpt-4.1-mini",
                max_tokens=800,
                messages=[
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": turn},
                ],
            )
            new_text = resp.choices[0].message.content or ""
        else:
            return
        clipped = clip_memory_text(new_text)
        if not clipped:
            return
        with get_db_ctx() as conn:
            conn.execute(
                "INSERT INTO user_memory (user_id, content, enabled, updated_at) "
                "VALUES (?, ?, 1, CURRENT_TIMESTAMP) "
                "ON CONFLICT(user_id) DO UPDATE SET content = excluded.content, "
                "updated_at = CURRENT_TIMESTAMP WHERE user_memory.enabled = 1",
                (user_id, clipped),
            )
            conn.commit()
    except Exception as exc:
        _dlog("user_memory_extract_failed", user_id=user_id, session_id=session_id, error=str(exc)[:300])
