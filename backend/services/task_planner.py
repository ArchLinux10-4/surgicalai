"""
Agentic task planner + task store.

This module powers the "create tasks from my prompt" feature:
  1. Decide whether a user message warrants an agentic task breakdown.
  2. Ask Claude to decompose the request into an ordered task list.
  3. Persist tasks and expose CRUD + cancellation helpers.

It deliberately does NOT touch the execution pipeline. The chat router wraps
`run_natural_pipeline_stream` once per task; this module only plans and tracks.

Cancellation is DB-backed (a `cancel_requested` flag) so it works across
multiple workers/instances — the running loop polls the flag.
"""
import json
import re
import uuid

from database import get_db, get_setting

# Reuse the exact same key-resolution + model guard the pipeline uses.
from services.pipeline import _get_anthropic_key, _is_claude_model, _dlog

TERMINAL_STATUSES = ("done", "blocked", "cancelled", "error")

# Explicit, unambiguous cues that the user wants an agentic task breakdown.
#
# IMPORTANT: every cue here must clearly mean "create a task list" on its own.
# We intentionally do NOT include conversational phrases like "step by step",
# "one by one", or "do all of these" — those appear constantly in ordinary
# coding prompts (e.g. "walk me through this step by step and fix the null
# check"). Including them hijacked normal single-pass edits into the multi-task
# planner, which re-decomposed the request and failed to produce the single
# coherent edit the user expected. The gate must be strictly opt-in so ordinary
# prompts always flow through the proven single-pass pipeline untouched.
# Unambiguous list/decomposition cues. On their own these always mean "produce
# a task list" and effectively never appear in ordinary descriptive prose, so
# they may trigger anywhere in the message.
_STRONG_CUE_PATTERNS = [
    r"\btask list\b",
    r"\blist of tasks\b",
    r"\b(?:break|split|divide|decompose)\s+(?:this|it|that|them|these|the following|the request|the work)?\s*(?:down |up )?into\s+(?:separate |individual |distinct )?tasks?\b",
    r"\bturn\s+(?:this|it|that|these|the following|the request)?\s*into\s+(?:separate |individual )?tasks?\b",
    r"\bas\s+(?:separate|individual|distinct)\s+tasks?\b",
    r"\bplan\s+(?:out\s+)?(?:the\s+)?(?:work|tasks?)\b",
    r"\b(?:create|make|generate)\s+(?:a |an )?task list\b",
    r"\b(?:create|make|generate)\s+(?:the following|these)\s+tasks?\b",
]
_STRONG_CUE_RE = re.compile("|".join(_STRONG_CUE_PATTERNS), re.IGNORECASE)

# Bare verb cues ("create/make/generate tasks") are fragile: the exact same
# words appear in DESCRIPTIONS of features ("a tool that lets users create tasks
# from a prompt", "build a button to create tasks"). Matching those hijacked
# ordinary coding prompts into the planner — the production incident. So a verb
# cue only counts when it sits in INSTRUCTION position: at the start of the
# message/clause, or right after a polite/imperative lead-in. It must never
# match mid-clause after a connector like "to / that / which / allows ...".
_VERB_CUE_RE = re.compile(
    r"(?:^|[.!?:;\n]\s*|\b(?:please|pls|kindly|can you|could you|would you|will you|"
    r"i want you to|i'd like you to|i would like you to|let's|lets)\s+)"
    r"(?:create|make|generate)\s+(?:a |an |the |some |several |multiple )?(?:new )?tasks?\b",
    re.IGNORECASE,
)


def wants_task_breakdown(message: str) -> bool:
    """Cheap gate: only run the planner when the user explicitly asks for tasks.

    Strictly opt-in. Unambiguous list/decomposition phrases trigger anywhere;
    bare verb cues ("create tasks") trigger only in instruction position so that
    descriptive mentions ("a feature that lets users create tasks") never hijack
    an ordinary coding prompt into the planner.
    """
    if not message:
        return False
    if _STRONG_CUE_RE.search(message):
        return True
    return bool(_VERB_CUE_RE.search(message))


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = """You are a planning assistant for a coding agent. The user wants their request broken into an ordered list of concrete, independently-executable tasks.

Rules:
- Each task must be a single, self-contained unit of work the coding agent can do in one pass.
- Order tasks so dependencies come first.
- Prefer 2–8 tasks. Never exceed 12. If the request is genuinely a single step, return exactly one task.
- "title" = short imperative label (max ~8 words). "detail" = a clear, standalone instruction the agent will receive as its prompt for that task (describe the goal/outcome, no line numbers).
- "kind" = "code" if the task edits/creates code or files; "answer" if it only researches, explains, summarizes, or plans with no file changes. When unsure, use "code".
- NEVER split edits to the same file across multiple tasks. All changes to one file belong in one task.
- NEVER split work on the same function, class, or symbol across tasks. Rewriting one function is one task, not many.
- Fewer tasks is better. If the whole request touches 1–2 files, prefer 1–2 tasks. Only create many tasks for genuinely independent pieces of work across different files.
- Do NOT include meta tasks like "review" or "test" unless the user explicitly asked for them.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{"preamble": "one short sentence to the user about the plan", "tasks": [{"title": "...", "detail": "...", "kind": "code"}]}"""


async def plan_tasks(message: str, session_files: list, user_id: str = "") -> dict:
    """
    Call Claude to decompose `message` into an ordered task list.

    Returns {"preamble": str, "tasks": [{"title","detail"}, ...]}.
    On any failure, returns {"preamble": "", "tasks": []} so the caller can
    cleanly fall back to the normal single-pass pipeline.
    """
    from anthropic import AsyncAnthropic

    try:
        anthropic_key = _get_anthropic_key(user_id)
    except Exception as exc:
        _dlog("task_planner_no_key", user_id=user_id, error=str(exc))
        return {"preamble": "", "tasks": []}

    model = get_setting("architect_model", "claude-sonnet-5")
    if not _is_claude_model(model):
        model = "claude-sonnet-5"

    file_hint = ""
    if session_files:
        names = [sf.get("filename", "") for sf in session_files if sf.get("filename")]
        if names:
            file_hint = "\n\nFiles available in this session: " + ", ".join(names[:25])

    user_block = f"User request:\n{message}{file_hint}"

    _dlog("task_planner_call_start", model=model, num_files=len(session_files),
          files=[sf.get("filename", "") for sf in session_files][:10],
          msg_len=len(message))

    try:
        client = AsyncAnthropic(api_key=anthropic_key)
        resp = await client.messages.create(
            model=model,
            # 2000 → 6000: proven in run dd543a3a (pipeline QA site, same model)
            # that claude-sonnet-5 can spend the entire budget on a thinking
            # block and return ZERO text blocks, which here would silently
            # yield an empty plan and disable Agent Mode for the request.
            max_tokens=6000,
            system=_PLANNER_SYSTEM,
            messages=[{"role": "user", "content": user_block}],
        )
        raw = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        _block_types = [getattr(b, "type", "?") for b in resp.content]
        _stop = getattr(resp, "stop_reason", None)
        _otoks = getattr(getattr(resp, "usage", None), "output_tokens", None)
        _dlog("task_planner_call_done", stop_reason=_stop, output_tokens=_otoks,
              block_types=_block_types, raw_len=len(raw))
        if not raw.strip():
            # Loud, attributable failure instead of a silent empty plan.
            _dlog("task_planner_empty_text", stop_reason=_stop, block_types=_block_types,
                  detail="planner degraded to no-task fallback")
    except Exception as exc:
        _dlog("task_planner_call_failed", error=str(exc))
        return {"preamble": "", "tasks": []}

    parsed = _extract_json(raw)
    if not parsed:
        if raw.strip():
            _dlog("task_planner_unparseable_json", raw_len=len(raw), head=raw[:120])
        return {"preamble": "", "tasks": []}

    tasks = []
    for t in (parsed.get("tasks") or []):
        if not isinstance(t, dict):
            continue
        title = (t.get("title") or "").strip()
        detail = (t.get("detail") or "").strip()
        if not title and not detail:
            continue
        kind = (t.get("kind") or "code").strip().lower()
        kind = "answer" if kind in ("answer", "non_code", "non-code", "noncode") else "code"
        tasks.append({"title": title or detail[:60], "detail": detail or title, "kind": kind})

    if len(tasks) > 12:
        tasks = tasks[:12]

    return {"preamble": (parsed.get("preamble") or "").strip(), "tasks": tasks}


def _extract_json(text: str) -> dict:
    """Best-effort JSON object extraction from a model response."""
    if not text:
        return {}
    # Strip code fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    # Fall back to the first balanced-looking object.
    if not fenced:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        return json.loads(candidate)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Task store (CRUD + cancellation)
# ---------------------------------------------------------------------------

def create_tasks(session_id: str, run_id: str, tasks: list) -> list:
    """Persist a planned list of tasks. Returns rows as dicts (with ids/seq).

    Extra keys ``filename``, ``symbol``, ``source`` are optional so existing
    Agent callers (title/detail/kind only) stay byte-compatible. Default
    source is ``agent``.
    """
    conn = get_db()
    created = []
    try:
        for i, t in enumerate(tasks):
            tid = str(uuid.uuid4())
            kind = "answer" if t.get("kind") == "answer" else "code"
            filename = (t.get("filename") or "") if isinstance(t, dict) else ""
            symbol = (t.get("symbol") or t.get("symbol_path") or "") if isinstance(t, dict) else ""
            source = (t.get("source") or "agent") if isinstance(t, dict) else "agent"
            if source not in ("plan", "agent"):
                source = "agent"
            conn.execute(
                "INSERT INTO agent_tasks (id, session_id, run_id, seq, title, detail, kind, status, "
                "filename, symbol, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (tid, session_id, run_id, i, t.get("title", ""), t.get("detail", ""), kind,
                 filename, symbol, source),
            )
            created.append({
                "id": tid,
                "session_id": session_id,
                "run_id": run_id,
                "seq": i,
                "title": t.get("title", ""),
                "detail": t.get("detail", ""),
                "kind": kind,
                "status": "pending",
                "qa_score": None,
                "verdict": None,
                "filename": filename,
                "symbol": symbol,
                "source": source,
            })
        conn.commit()
    finally:
        conn.close()
    return created


def update_task(task_id: str, **fields):
    """Update mutable task columns. Always bumps updated_at."""
    allowed = {"status", "qa_score", "verdict", "result_summary", "thinking"}
    sets = [f"{k} = ?" for k in fields if k in allowed]
    vals = [fields[k] for k in fields if k in allowed]
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    conn = get_db()
    try:
        conn.execute(
            f"UPDATE agent_tasks SET {', '.join(sets)} WHERE id = ?",
            (*vals, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_tasks(session_id: str, run_id: str = None) -> list:
    conn = get_db()
    try:
        if run_id:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE session_id = ? AND run_id = ? ORDER BY seq ASC",
                (session_id, run_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE session_id = ? ORDER BY created_at DESC, seq ASC",
                (session_id,),
            ).fetchall()
        result = [dict(r) for r in rows]
        # Heal tasks stuck as running/pending with cancel_requested set — this
        # happens when the SSE loop died (network error) before it could check
        # the flag and update the status itself.
        stale_ids = [
            r["id"] for r in result
            if r.get("cancel_requested") and r.get("status") in ("running", "pending")
        ]
        if stale_ids:
            placeholders = ",".join("?" * len(stale_ids))
            conn.execute(
                f"UPDATE agent_tasks SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP "
                f"WHERE id IN ({placeholders})",
                stale_ids,
            )
            conn.commit()
            for r in result:
                if r["id"] in stale_ids:
                    r["status"] = "cancelled"
        return result
    finally:
        conn.close()


def request_cancel(task_id: str) -> bool:
    """Cancel a single non-terminal task immediately.

    Sets both the flag (for the running loop to detect) and the status (so
    re-entry after a dropped connection shows the correct state rather than
    restarting polling on a stuck 'running' task).
    """
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE agent_tasks "
            "SET cancel_requested = 1, status = 'cancelled', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status NOT IN ('done','blocked','cancelled','error')",
            (task_id,),
        )
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def request_cancel_all(session_id: str, run_id: str = None) -> int:
    """Cancel every non-terminal task (optionally within a run) immediately.

    Sets both the flag (for the running loop) and the status (so the DB
    reflects the true state even if the SSE loop died before checking the flag).
    """
    conn = get_db()
    try:
        if run_id:
            cur = conn.execute(
                "UPDATE agent_tasks "
                "SET cancel_requested = 1, status = 'cancelled', updated_at = CURRENT_TIMESTAMP "
                "WHERE session_id = ? AND run_id = ? "
                "AND status NOT IN ('done','blocked','cancelled','error')",
                (session_id, run_id),
            )
        else:
            cur = conn.execute(
                "UPDATE agent_tasks "
                "SET cancel_requested = 1, status = 'cancelled', updated_at = CURRENT_TIMESTAMP "
                "WHERE session_id = ? AND status NOT IN ('done','blocked','cancelled','error')",
                (session_id,),
            )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def cancel_requested_for_run(session_id: str, run_id: str) -> bool:
    """True if any task in the run has been flagged for cancellation."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM agent_tasks "
            "WHERE session_id = ? AND run_id = ? AND cancel_requested = 1",
            (session_id, run_id),
        ).fetchone()
        n = list(row.values())[0] if hasattr(row, "values") else row[0]
        return int(n) > 0
    finally:
        conn.close()


def mark_pending_cancelled(session_id: str, run_id: str):
    """After a cancel/block, move all still-pending/running tasks to cancelled."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE agent_tasks SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP "
            "WHERE session_id = ? AND run_id = ? AND status IN ('pending','running')",
            (session_id, run_id),
        )
        conn.commit()
    finally:
        conn.close()
