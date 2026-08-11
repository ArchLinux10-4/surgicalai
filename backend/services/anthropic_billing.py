"""Anthropic credit-exhaustion detect → pause → persist → probe → resume.

WHY THIS MODULE EXISTS (session cb380321, Retry with QA)
------------------------------------------------------
Plan→Execute calls Claude via ``aclient.messages.stream``. When the Anthropic
account is out of credits, Anthropic returns HTTP 400 ``invalid_request_error``
with message ``Your credit balance is too low to access the Anthropic API``.

Evidence from ``surgical_debug_cb380321.jsonl`` (final Retry with QA run):
  * L1168/L1171 — Grok produced 2 valid ``write_surgical_edit`` calls
  * L1173 — plan gate held those writes (compound request)
  * L1198 — forced plan captured (3 steps)
  * L1204/L1206/L1208 — all 3 ``plan_execute_error`` = credit balance too low
  * L1209 — ``no_edits_produced`` with misleading
            ``Focused edit call produced no valid edit block``

The inner ``_execute_single_edit`` catch-all returned ``None``, so the real
billing error was logged then discarded. Credits were already dying earlier
in the same session (L1037 ``grok_second_qa_error``, same Anthropic message).

This module:
  1. Classifies Anthropic credit/billing exhaustion (stable exception type).
  2. Persists a resumeable ``credit_pauses`` row (remaining plan + any
     completed edit blocks + held Grok writes + chained file snapshot).
  3. Probes Anthropic with a tiny messages.create so the UI can enable Resume
     only when balance is no longer zero.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

try:  # pragma: no cover - import guard only
    from database import _dlog as _db_dlog, get_db_ctx, USE_POSTGRES
except Exception:  # pragma: no cover
    _db_dlog = None
    get_db_ctx = None
    USE_POSTGRES = False


# Substring markers matched against the rendered Anthropic error string / body.
# Evidence: session cb380321 used the literal phrase
# "Your credit balance is too low to access the Anthropic API".
_CREDIT_MARKERS = (
    "credit balance is too low",
    "purchase credits",
    "plans & billing",
    "plans and billing",
    "insufficient credits",
    "exceeded your credit",
)


def _dlog(event: str, dlog=None, **kwargs):
    try:
        if dlog is not None:
            dlog(event, **kwargs)
            return
        if _db_dlog is not None:
            _db_dlog(event, **kwargs)
    except Exception:
        pass


class AnthropicCreditExhaustedError(Exception):
    """Raised when Anthropic rejects a call due to zero/low credit balance.

    Stable marker for ``_friendly_error`` and the plan-exec pause path —
    not a fragile substring check at every call site.
    """

    def __init__(self, message: str = "", *, original: Exception | None = None):
        super().__init__(
            message
            or "Anthropic credit balance is too low to continue editing."
        )
        self.original = original


def is_anthropic_credit_error(exc: Exception | None) -> bool:
    """True when ``exc`` is (or wraps) an Anthropic credit-balance failure.

    Anthropic surfaces this as HTTP 400 ``invalid_request_error`` with a
    billing message — NOT as a 429 — so GPT/Grok 429 classifiers miss it.
    """
    if exc is None:
        return False
    if isinstance(exc, AnthropicCreditExhaustedError):
        return True

    # Walk a shallow cause chain (SDK sometimes wraps).
    seen = set()
    cur: Any = exc
    for _ in range(4):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        text = str(cur).lower()
        if any(m in text for m in _CREDIT_MARKERS):
            return True
        body = getattr(cur, "body", None)
        if isinstance(body, dict):
            try:
                blob = json.dumps(body).lower()
            except Exception:
                blob = str(body).lower()
            if any(m in blob for m in _CREDIT_MARKERS):
                return True
        cur = getattr(cur, "original", None) or getattr(cur, "__cause__", None)
    return False


def raise_if_anthropic_credit_error(exc: Exception, dlog=None,
                                    session_id: str = "",
                                    user_id: str = "") -> None:
    """Re-raise ``exc`` as ``AnthropicCreditExhaustedError`` when classified."""
    if is_anthropic_credit_error(exc):
        _dlog("anthropic_credit_exhausted_classified", dlog=dlog,
              session_id=session_id, user_id=user_id,
              error_preview=str(exc)[:300])
        if isinstance(exc, AnthropicCreditExhaustedError):
            raise exc
        raise AnthropicCreditExhaustedError(str(exc)[:500], original=exc) from exc


def anthropic_credit_user_message() -> str:
    return (
        "Anthropic credits are exhausted — planned edits are paused and saved. "
        "Add credits at console.anthropic.com/settings/billing, then click "
        "**Resume** when this banner shows credits are available. Retrying "
        "will not help until the balance is restored."
    )


async def probe_anthropic_credits(api_key: str, dlog=None,
                                  session_id: str = "",
                                  user_id: str = "") -> dict:
    """Tiny Anthropic round-trip to test whether credits work again.

    Uses ``claude-sonnet-5`` with ``max_tokens=1`` and a one-token prompt.
    Returns ``{"ok": bool, "error": str|None}``. Never raises.
    """
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key configured"}
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)
        await client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        _dlog("anthropic_credit_probe_ok", dlog=dlog,
              session_id=session_id, user_id=user_id)
        return {"ok": True, "error": None}
    except Exception as e:
        exhausted = is_anthropic_credit_error(e)
        _dlog("anthropic_credit_probe_failed", dlog=dlog,
              session_id=session_id, user_id=user_id,
              credit_exhausted=exhausted,
              error_type=type(e).__name__,
              error=str(e)[:300])
        return {
            "ok": False,
            "error": (
                anthropic_credit_user_message()
                if exhausted
                else f"Anthropic probe failed: {str(e)[:200]}"
            ),
            "credit_exhausted": exhausted,
        }


def _json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def save_credit_pause(
    *,
    session_id: str,
    user_id: str,
    user_request: str,
    remaining_plan: list,
    completed_edit_blocks: list | None = None,
    completed_new_file_blocks: list | None = None,
    held_grok_writes: list | None = None,
    file_content_snapshot: dict | None = None,
    error_message: str = "",
    dlog=None,
) -> Optional[str]:
    """Persist a resumeable pause. Returns pause_id or None on failure.

    Always supersedes any prior ``paused`` row for the same session so a
    session has at most one active credit pause.
    """
    if get_db_ctx is None:
        return None
    pause_id = str(uuid.uuid4())
    try:
        with get_db_ctx() as conn:
            # Dismiss prior active pauses for this session — only the latest
            # remaining plan is meaningful.
            if USE_POSTGRES:
                conn.execute(
                    "UPDATE credit_pauses SET status = 'superseded', "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE session_id = %s AND status = 'paused'",
                    (session_id,),
                )
                conn.execute(
                    """INSERT INTO credit_pauses (
                           id, session_id, user_id, status, user_request,
                           remaining_plan_json, completed_edit_blocks_json,
                           completed_new_file_blocks_json, held_grok_writes_json,
                           file_content_snapshot_json, error_message
                       ) VALUES (
                           %s, %s, %s, 'paused', %s, %s, %s, %s, %s, %s, %s
                       )""",
                    (
                        pause_id, session_id, user_id or "", user_request or "",
                        _json_dump(remaining_plan or []),
                        _json_dump(completed_edit_blocks or []),
                        _json_dump(completed_new_file_blocks or []),
                        _json_dump(held_grok_writes or []),
                        _json_dump(file_content_snapshot or {}),
                        (error_message or "")[:1000],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE credit_pauses SET status = 'superseded', "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE session_id = ? AND status = 'paused'",
                    (session_id,),
                )
                conn.execute(
                    """INSERT INTO credit_pauses (
                           id, session_id, user_id, status, user_request,
                           remaining_plan_json, completed_edit_blocks_json,
                           completed_new_file_blocks_json, held_grok_writes_json,
                           file_content_snapshot_json, error_message
                       ) VALUES (
                           ?, ?, ?, 'paused', ?, ?, ?, ?, ?, ?, ?
                       )""",
                    (
                        pause_id, session_id, user_id or "", user_request or "",
                        _json_dump(remaining_plan or []),
                        _json_dump(completed_edit_blocks or []),
                        _json_dump(completed_new_file_blocks or []),
                        _json_dump(held_grok_writes or []),
                        _json_dump(file_content_snapshot or {}),
                        (error_message or "")[:1000],
                    ),
                )
            conn.commit()
        _dlog("credit_pause_saved", dlog=dlog,
              session_id=session_id, user_id=user_id, pause_id=pause_id,
              remaining_count=len(remaining_plan or []),
              completed_edits=len(completed_edit_blocks or []),
              held_writes=len(held_grok_writes or []))
        return pause_id
    except Exception as e:
        _dlog("credit_pause_save_error", dlog=dlog,
              session_id=session_id, user_id=user_id,
              error_type=type(e).__name__, error=str(e)[:300])
        return None


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


def _parse_json_field(raw, default):
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def get_credit_pause(pause_id: str) -> Optional[dict]:
    if get_db_ctx is None or not pause_id:
        return None
    try:
        with get_db_ctx() as conn:
            row = conn.execute(
                "SELECT * FROM credit_pauses WHERE id = ?",
                (pause_id,),
            ).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        return {
            "id": d.get("id"),
            "session_id": d.get("session_id"),
            "user_id": d.get("user_id") or "",
            "status": d.get("status"),
            "user_request": d.get("user_request") or "",
            "remaining_plan": _parse_json_field(d.get("remaining_plan_json"), []),
            "completed_edit_blocks": _parse_json_field(
                d.get("completed_edit_blocks_json"), []),
            "completed_new_file_blocks": _parse_json_field(
                d.get("completed_new_file_blocks_json"), []),
            "held_grok_writes": _parse_json_field(
                d.get("held_grok_writes_json"), []),
            "file_content_snapshot": _parse_json_field(
                d.get("file_content_snapshot_json"), {}),
            "error_message": d.get("error_message") or "",
            "created_at": str(d.get("created_at") or ""),
            "updated_at": str(d.get("updated_at") or ""),
        }
    except Exception as e:
        _dlog("credit_pause_get_error", pause_id=pause_id,
              error_type=type(e).__name__, error=str(e)[:200])
        return None


def get_active_credit_pause(session_id: str) -> Optional[dict]:
    if get_db_ctx is None or not session_id:
        return None
    try:
        with get_db_ctx() as conn:
            row = conn.execute(
                "SELECT * FROM credit_pauses WHERE session_id = ? "
                "AND status = 'paused' ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return get_credit_pause(_row_to_dict(row).get("id"))
    except Exception as e:
        _dlog("credit_pause_active_get_error", session_id=session_id,
              error_type=type(e).__name__, error=str(e)[:200])
        return None


def update_credit_pause_status(pause_id: str, status: str, dlog=None) -> bool:
    if get_db_ctx is None or not pause_id:
        return False
    try:
        with get_db_ctx() as conn:
            if USE_POSTGRES:
                conn.execute(
                    "UPDATE credit_pauses SET status = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (status, pause_id),
                )
            else:
                conn.execute(
                    "UPDATE credit_pauses SET status = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (status, pause_id),
                )
            conn.commit()
        _dlog("credit_pause_status_updated", dlog=dlog,
              pause_id=pause_id, status=status)
        return True
    except Exception as e:
        _dlog("credit_pause_status_error", dlog=dlog, pause_id=pause_id,
              status=status, error=str(e)[:200])
        return False


def credit_pause_public_view(pause: dict | None) -> dict | None:
    """Client-safe view (no full file contents / edit bodies)."""
    if not pause:
        return None
    remaining = pause.get("remaining_plan") or []
    return {
        "pause_id": pause.get("id"),
        "session_id": pause.get("session_id"),
        "status": pause.get("status"),
        "remaining_count": len(remaining),
        "remaining_symbols": [
            {"filename": s.get("filename"), "symbol": s.get("symbol")}
            for s in remaining[:40]
            if isinstance(s, dict)
        ],
        "completed_edit_count": len(pause.get("completed_edit_blocks") or []),
        "held_write_count": len(pause.get("held_grok_writes") or []),
        "message": anthropic_credit_user_message(),
        "error_message": (pause.get("error_message") or "")[:300],
        "created_at": pause.get("created_at"),
    }
