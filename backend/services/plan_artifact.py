"""Structured Chat-Plan artifact: parse, persist, revise, coverage.

Chat Plan mode streams markdown. This module extracts a trailing
``implementation_plan`` JSON fence (Claude / Grok / OpenAI — no provider
gate), stores steps as ``agent_tasks`` with ``source='plan'``, and later
compares planned ``(filename, symbol)`` keys to what Implement produced.

Does NOT call ``plan_tasks()`` and does not emit Agent ``task_plan`` SSE.
"""
from __future__ import annotations

import json
import re
import uuid

from database import get_db
from services.task_planner import create_tasks, list_tasks, update_task

_FENCE_RE = re.compile(
    r"```(?:implementation_plan|plan-json)\s*([\s\S]*?)```",
    re.IGNORECASE,
)

# Cap so one huge plan cannot blow the prompt (outside the 4000-char history trim).
PLAN_MARKDOWN_PROMPT_CAP = 48_000


def parse_implementation_plan(text: str) -> list[dict] | None:
    """Return normalized steps or None if the fence is missing/invalid.

    Accepts a fenced object ``{"steps": [...]}`` or a bare JSON array.
    Each step needs filename + symbol (``symbol_path`` is an alias).
    """
    if not (text or "").strip():
        return None
    m = _FENCE_RE.search(text)
    if not m:
        return None
    raw = (m.group(1) or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None

    items = []
    if isinstance(payload, dict):
        items = payload.get("steps") or payload.get("edits") or []
    elif isinstance(payload, list):
        items = payload
    if not isinstance(items, list) or not items:
        return None

    steps = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        symbol = str(item.get("symbol") or item.get("symbol_path") or "").strip()
        description = str(item.get("description") or item.get("detail") or "").strip()
        if not filename or not symbol:
            continue
        key = _norm_key(filename, symbol)
        if key in seen:
            continue
        seen.add(key)
        steps.append({
            "filename": filename,
            "symbol": symbol,
            "description": description or f"Update {symbol} in {filename}",
        })
    return steps or None


def steps_to_task_payloads(steps: list[dict]) -> list[dict]:
    """Shape create_tasks() rows. Default source=plan; Agent callers unused."""
    out = []
    for s in steps:
        filename = s.get("filename") or ""
        symbol = s.get("symbol") or ""
        desc = s.get("description") or ""
        out.append({
            "title": f"{filename} · {symbol}".strip(" ·"),
            "detail": desc,
            "kind": "code",
            "filename": filename,
            "symbol": symbol,
            "source": "plan",
        })
    return out


def serialize_plan_task(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "seq": row.get("seq", 0),
        "title": row.get("title") or "",
        "detail": row.get("detail") or "",
        "kind": row.get("kind") or "code",
        "status": row.get("status") or "pending",
        "filename": row.get("filename") or "",
        "symbol": row.get("symbol") or "",
        "source": row.get("source") or "plan",
        "run_id": row.get("run_id"),
        "qa_score": row.get("qa_score"),
        "verdict": row.get("verdict"),
        "result_summary": row.get("result_summary") or "",
    }


def _norm_key(filename: str, symbol: str) -> tuple[str, str]:
    return (_norm_filename(filename), _norm_symbol(symbol))


def _norm_filename(filename: str) -> str:
    fn = (filename or "").strip().replace("\\", "/").casefold()
    while fn.startswith("./"):
        fn = fn[2:]
    return fn


def _norm_symbol(symbol: str) -> str:
    return (symbol or "").strip().casefold()


def _files_compatible(a: str, b: str) -> bool:
    a, b = _norm_filename(a), _norm_filename(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return a.endswith("/" + b) or b.endswith("/" + a)


def _symbols_compatible(a: str, b: str) -> bool:
    a, b = _norm_symbol(a), _norm_symbol(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return (
        a.endswith("." + b) or b.endswith("." + a)
        or a.endswith("/" + b) or b.endswith("/" + a)
    )


def _keys_compatible(left: tuple[str, str], right: tuple[str, str]) -> bool:
    return _files_compatible(left[0], right[0]) and _symbols_compatible(left[1], right[1])


def _task_key(row: dict) -> tuple[str, str]:
    return _norm_key(row.get("filename") or "", row.get("symbol") or "")


def _extract_symbol(item: dict) -> str:
    symbol = item.get("symbol")
    if isinstance(symbol, dict):
        return str(
            symbol.get("full_path")
            or symbol.get("name")
            or symbol.get("symbol_path")
            or ""
        )
    if symbol:
        return str(symbol)
    return str(item.get("symbol_path") or item.get("name") or "")


def plan_run_phase(tasks: list[dict]) -> str:
    if not tasks:
        return "idle"
    statuses = [t.get("status") or "pending" for t in tasks]
    if any(s == "running" for s in statuses):
        return "implementing"
    if all(s == "pending" for s in statuses):
        return "ready"
    terminal = {"done", "blocked", "cancelled", "error"}
    if all(s in terminal for s in statuses):
        if any(s == "blocked" for s in statuses):
            return "blocked"
        return "complete"
    return "ready"


def latest_plan_run(session_id: str) -> dict | None:
    """Most recent source=plan run for this session (any phase).

    Excludes fully superseded runs, then picks max(created_at).
    ``create_tasks`` writes a sub-second UTC stamp so same-second
    complete-then-revise does not need a phase-rank tiebreak.
    """
    rows = [r for r in list_tasks(session_id) if (r.get("source") or "agent") == "plan"]
    if not rows:
        return None
    by_run: dict[str, list] = {}
    for r in rows:
        rid = r.get("run_id")
        if rid:
            by_run.setdefault(rid, []).append(r)
    if not by_run:
        return None

    def _is_superseded(tasks: list) -> bool:
        return bool(tasks) and all(
            (t.get("status") == "cancelled" and (t.get("result_summary") or "") == "superseded")
            for t in tasks
        )

    def _stamp(tasks: list) -> str:
        return max(str(t.get("created_at") or "") for t in tasks)

    live = [rid for rid, ts in by_run.items() if not _is_superseded(ts)]
    candidates = live or list(by_run.keys())
    run_id = max(candidates, key=lambda rid: _stamp(by_run[rid]))
    tasks = by_run[run_id]
    tasks.sort(key=lambda t: t.get("seq") or 0)
    return {"run_id": run_id, "tasks": tasks, "phase": plan_run_phase(tasks)}


def current_plan_json_for_prompt(session_id: str) -> str:
    """JSON the model should revise — Ready / blocked / complete checklists.

    Ready is the primary revise path. Blocked and complete still inject the
    last step list so a follow-up Plan turn can see what was planned.
    Implementing stays empty (checklist is locked).
    """
    latest = latest_plan_run(session_id)
    if not latest or latest["phase"] not in ("ready", "blocked", "complete"):
        return ""
    steps = []
    for t in latest["tasks"]:
        steps.append({
            "filename": t.get("filename") or "",
            "symbol": t.get("symbol") or "",
            "description": t.get("detail") or "",
        })
    if not steps:
        return ""
    return (
        "\n\nCurrent implementation_plan (revise this list and emit a FULL "
        "replacement ```implementation_plan JSON fence — do not patch in prose):\n"
        "```implementation_plan\n"
        + json.dumps({"steps": steps}, indent=2)
        + "\n```"
    )


def _title_from_markdown(markdown: str) -> str:
    for line in (markdown or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            title = s[2:].strip()
            if title:
                return title[:200]
    return "Plan"


def load_session_plan(session_id: str) -> dict | None:
    """Return the living session_plans row for this session, or None."""
    if not session_id:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, session_id, user_id, title, markdown, version, "
            "created_at, updated_at FROM session_plans WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_session_plan(
    session_id: str,
    markdown: str,
    user_id: str | None = None,
) -> dict:
    """Insert or replace the session plan markdown. Bumps version on change.

    Previous body is copied into session_plan_revisions before overwrite.
    """
    body = (markdown or "").strip()
    if not session_id or not body:
        raise ValueError("session_id and markdown required")
    title = _title_from_markdown(body)
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id, markdown, version, user_id FROM session_plans "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if existing:
            plan_id = existing["id"]
            prev_md = existing["markdown"] or ""
            version = int(existing["version"] or 1)
            if prev_md.strip() != body:
                conn.execute(
                    "INSERT INTO session_plan_revisions "
                    "(id, plan_id, session_id, version, markdown, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        plan_id,
                        session_id,
                        version,
                        prev_md,
                        user_id or existing["user_id"] or "",
                    ),
                )
                version += 1
            conn.execute(
                "UPDATE session_plans SET title = ?, markdown = ?, version = ?, "
                "user_id = COALESCE(?, user_id), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (title, body, version, user_id or None, plan_id),
            )
            conn.commit()
            return {
                "id": plan_id,
                "session_id": session_id,
                "user_id": user_id or existing["user_id"],
                "title": title,
                "markdown": body,
                "version": version,
            }

        plan_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO session_plans "
            "(id, session_id, user_id, title, markdown, version) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (plan_id, session_id, user_id or None, title, body),
        )
        conn.commit()
        return {
            "id": plan_id,
            "session_id": session_id,
            "user_id": user_id,
            "title": title,
            "markdown": body,
            "version": 1,
        }
    finally:
        conn.close()


def current_plan_markdown_for_prompt(session_id: str) -> str:
    """Full saved plan markdown for Plan / Implement injection (capped)."""
    row = load_session_plan(session_id)
    if not row:
        return ""
    md = (row.get("markdown") or "").strip()
    if not md:
        return ""
    if len(md) > PLAN_MARKDOWN_PROMPT_CAP:
        md = md[:PLAN_MARKDOWN_PROMPT_CAP] + "\n\n…(plan truncated for prompt size)"
    ver = row.get("version") or 1
    title = row.get("title") or "Plan"
    return (
        f"\n\n## Current session plan (v{ver}: {title})\n"
        "This is the saved Plan document for this session. Revise it in full "
        "when the approach changes; emit a FULL replacement "
        "```implementation_plan JSON fence at the end.\n\n"
        f"{md}"
    )


def supersede_plan_run(run_id: str, session_id: str) -> None:
    for t in list_tasks(session_id, run_id):
        if (t.get("source") or "") != "plan":
            continue
        if (t.get("status") or "") in ("pending", "running"):
            update_task(t["id"], status="cancelled", result_summary="superseded")


def persist_plan_from_assistant_text(
    session_id: str,
    text: str,
    user_id: str | None = None,
) -> dict | None:
    """Parse + persist. Returns an SSE payload or None (no tracker).

    - Implementing: do not rewrite rows; emit ``plan_locked``.
    - Invalid JSON + existing Ready plan: keep it; emit ``plan_unchanged``.
    - Invalid JSON + no Ready plan: None (first-plan fail-loud).
    - Valid JSON + Ready plan: supersede + ``plan_updated``.
    - Valid JSON otherwise (none / complete / blocked): new run ``plan_ready``.

    On a valid fence, also upserts the full assistant text into session_plans.
    """
    latest = latest_plan_run(session_id)
    if latest and latest["phase"] == "implementing":
        doc = load_session_plan(session_id)
        payload = {
            "type": "plan_locked",
            "run_id": latest["run_id"],
            "phase": "implementing",
            "tasks": [serialize_plan_task(t) for t in latest["tasks"]],
        }
        if doc:
            payload["markdown"] = doc.get("markdown") or ""
            payload["title"] = doc.get("title") or "Plan"
            payload["version"] = doc.get("version") or 1
        return payload

    steps = parse_implementation_plan(text)
    if not steps:
        if latest and latest["phase"] == "ready":
            doc = load_session_plan(session_id)
            payload = {
                "type": "plan_unchanged",
                "run_id": latest["run_id"],
                "phase": "ready",
                "reason": "invalid_json",
                "tasks": [serialize_plan_task(t) for t in latest["tasks"]],
            }
            if doc:
                payload["markdown"] = doc.get("markdown") or ""
                payload["title"] = doc.get("title") or "Plan"
                payload["version"] = doc.get("version") or 1
            return payload
        return None

    event = "plan_ready"
    if latest and latest["phase"] == "ready":
        supersede_plan_run(latest["run_id"], session_id)
        event = "plan_updated"

    run_id = str(uuid.uuid4())
    created = create_tasks(session_id, run_id, steps_to_task_payloads(steps))
    doc = upsert_session_plan(session_id, text or "", user_id=user_id)
    return {
        "type": event,
        "run_id": run_id,
        "phase": "ready",
        "tasks": [serialize_plan_task(t) for t in created],
        "markdown": doc.get("markdown") or "",
        "title": doc.get("title") or "Plan",
        "version": doc.get("version") or 1,
    }


def mark_plan_implementing(session_id: str, run_id: str) -> list[dict]:
    tasks = [t for t in list_tasks(session_id, run_id) if (t.get("source") or "") == "plan"]
    for t in tasks:
        if (t.get("status") or "") == "pending":
            update_task(t["id"], status="running")
            t["status"] = "running"
    return tasks


def revert_plan_after_implement_error(session_id: str, run_id: str, reason: str) -> list[dict]:
    """Unlock a crashed Implement. Pending/running → pending. No coverage."""
    note = (reason or "Implement failed").strip()[:300]
    updated = []
    for t in list_tasks(session_id, run_id):
        if (t.get("source") or "") != "plan":
            continue
        if (t.get("status") or "") in ("running", "pending"):
            update_task(t["id"], status="pending", result_summary=note)
            t["status"] = "pending"
            t["result_summary"] = note
        updated.append(t)
    updated.sort(key=lambda r: r.get("seq") or 0)
    return updated


def forced_edit_plan_from_run(session_id: str, run_id: str) -> list[dict]:
    tasks = [t for t in list_tasks(session_id, run_id) if (t.get("source") or "") == "plan"]
    tasks.sort(key=lambda t: t.get("seq") or 0)
    plan = []
    for t in tasks:
        if (t.get("status") or "") in ("done", "cancelled"):
            continue
        filename = t.get("filename") or ""
        symbol = t.get("symbol") or ""
        if filename and symbol:
            plan.append({
                "filename": filename,
                "symbol": symbol,
                "description": t.get("detail") or "",
            })
    return plan


def _keys_from_changes_by_file(changes_by_file) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    if not isinstance(changes_by_file, dict):
        return keys
    for filename, fd in changes_by_file.items():
        fn = filename if isinstance(filename, str) else ""
        if isinstance(fd, dict):
            fn = fd.get("filename") or fn
            changes = fd.get("changes") or []
        elif isinstance(fd, list):
            changes = fd
        else:
            continue
        for ch in changes:
            if not isinstance(ch, dict):
                continue
            symbol = _extract_symbol(ch)
            pair = (str(fn or ""), str(symbol or ""))
            if pair[0] or pair[1]:
                keys.append(pair)
    return keys


def _keys_from_skipped(skipped) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    if not isinstance(skipped, list):
        return keys
    for item in skipped:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            continue
        fn = str(item.get("filename") or "")
        symbol = _extract_symbol(item)
        if fn or symbol:
            keys.append((fn, symbol))
    return keys


def compute_plan_coverage(planned_steps: list[dict], result: dict) -> dict:
    """Deterministic planned vs produced. No LLM.

    A planned key is covered if any produced or skipped-with-reason key is
    file/symbol-compatible (path suffix, dotted symbol suffix). Extra
    produced keys are allowed.
    """
    planned: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for s in planned_steps or []:
        if not isinstance(s, dict):
            continue
        pair = (str(s.get("filename") or ""), str(s.get("symbol") or ""))
        key = _norm_key(pair[0], pair[1])
        if not (key[0] and key[1]) or key in seen:
            continue
        seen.add(key)
        planned.append(pair)

    produced = _keys_from_changes_by_file((result or {}).get("changes_by_file"))
    skipped = _keys_from_skipped((result or {}).get("skipped_changes"))
    pool = produced + skipped

    covered = [p for p in planned if any(_keys_compatible(p, q) for q in pool)]
    missing = [p for p in planned if p not in covered]
    extra = [q for q in produced if not any(_keys_compatible(q, p) for p in planned)]
    skipped_hit = [q for q in skipped if any(_keys_compatible(q, p) for p in planned)]
    return {
        "ok": len(missing) == 0,
        "covered": [{"filename": fn, "symbol": sym} for fn, sym in covered],
        "missing": [{"filename": fn, "symbol": sym} for fn, sym in missing],
        "extra": [{"filename": fn, "symbol": sym} for fn, sym in extra],
        "skipped": [{"filename": fn, "symbol": sym} for fn, sym in skipped_hit],
    }


def apply_coverage_to_run(session_id: str, run_id: str, coverage: dict) -> list[dict]:
    """Mark each source=plan task done or blocked from coverage keys."""
    missing = {
        _norm_key(m.get("filename") or "", m.get("symbol") or "")
        for m in (coverage or {}).get("missing") or []
    }
    updated = []
    for t in list_tasks(session_id, run_id):
        if (t.get("source") or "") != "plan":
            continue
        key = _task_key(t)
        if key in missing:
            update_task(
                t["id"],
                status="blocked",
                verdict="plan_coverage",
                result_summary="Planned step was not produced and was not skipped with a reason.",
            )
            t["status"] = "blocked"
            t["verdict"] = "plan_coverage"
        else:
            update_task(t["id"], status="done", verdict="covered")
            t["status"] = "done"
            t["verdict"] = "covered"
        updated.append(t)
    updated.sort(key=lambda r: r.get("seq") or 0)
    return updated
