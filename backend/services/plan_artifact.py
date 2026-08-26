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

from services.task_planner import create_tasks, list_tasks, update_task

_FENCE_RE = re.compile(
    r"```(?:implementation_plan|plan-json)\s*([\s\S]*?)```",
    re.IGNORECASE,
)


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
    return ((filename or "").strip().lower(), (symbol or "").strip())


def _task_key(row: dict) -> tuple[str, str]:
    return _norm_key(row.get("filename") or "", row.get("symbol") or "")


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

    Prefers a run that is not fully superseded so a same-second revise
    does not lose to the cancelled previous run (SQLite CURRENT_TIMESTAMP
    is 1s resolution).
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
    # Same-second SQLite CURRENT_TIMESTAMP: prefer an active checklist
    # (implementing/ready/blocked) over a just-completed history run.
    _phase_rank = {
        "implementing": 4,
        "ready": 3,
        "blocked": 2,
        "complete": 1,
        "idle": 0,
    }
    run_id = max(
        candidates,
        key=lambda rid: (
            _phase_rank.get(plan_run_phase(by_run[rid]), 0),
            _stamp(by_run[rid]),
        ),
    )
    tasks = by_run[run_id]
    tasks.sort(key=lambda t: t.get("seq") or 0)
    return {"run_id": run_id, "tasks": tasks, "phase": plan_run_phase(tasks)}


def current_plan_json_for_prompt(session_id: str) -> str:
    """JSON the model should revise — only a Ready checklist."""
    latest = latest_plan_run(session_id)
    if not latest or latest["phase"] != "ready":
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


def supersede_plan_run(run_id: str, session_id: str) -> None:
    for t in list_tasks(session_id, run_id):
        if (t.get("source") or "") != "plan":
            continue
        if (t.get("status") or "") in ("pending", "running"):
            update_task(t["id"], status="cancelled", result_summary="superseded")


def persist_plan_from_assistant_text(session_id: str, text: str) -> dict | None:
    """Parse + persist. Returns an SSE payload or None (no tracker).

    - Implementing: do not rewrite rows; emit ``plan_locked``.
    - Invalid JSON + existing Ready plan: keep it; emit ``plan_unchanged``.
    - Invalid JSON + no Ready plan: None (first-plan fail-loud).
    - Valid JSON + Ready plan: supersede + ``plan_updated``.
    - Valid JSON otherwise (none / complete / blocked): new run ``plan_ready``.
    """
    latest = latest_plan_run(session_id)
    if latest and latest["phase"] == "implementing":
        return {
            "type": "plan_locked",
            "run_id": latest["run_id"],
            "phase": "implementing",
            "tasks": [serialize_plan_task(t) for t in latest["tasks"]],
        }

    steps = parse_implementation_plan(text)
    if not steps:
        if latest and latest["phase"] == "ready":
            return {
                "type": "plan_unchanged",
                "run_id": latest["run_id"],
                "phase": "ready",
                "reason": "invalid_json",
                "tasks": [serialize_plan_task(t) for t in latest["tasks"]],
            }
        return None

    event = "plan_ready"
    if latest and latest["phase"] == "ready":
        supersede_plan_run(latest["run_id"], session_id)
        event = "plan_updated"

    run_id = str(uuid.uuid4())
    created = create_tasks(session_id, run_id, steps_to_task_payloads(steps))
    return {
        "type": event,
        "run_id": run_id,
        "phase": "ready",
        "tasks": [serialize_plan_task(t) for t in created],
    }


def mark_plan_implementing(session_id: str, run_id: str) -> list[dict]:
    tasks = [t for t in list_tasks(session_id, run_id) if (t.get("source") or "") == "plan"]
    for t in tasks:
        if (t.get("status") or "") == "pending":
            update_task(t["id"], status="running")
            t["status"] = "running"
    return tasks


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


def _keys_from_changes_by_file(changes_by_file) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
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
            symbol = ch.get("symbol")
            if isinstance(symbol, dict):
                symbol = symbol.get("full_path") or symbol.get("name") or ""
            keys.add(_norm_key(str(fn or ""), str(symbol or "")))
    return {k for k in keys if k[0] or k[1]}


def _keys_from_skipped(skipped) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if not isinstance(skipped, list):
        return keys
    for item in skipped:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            continue
        keys.add(_norm_key(item.get("filename") or "", item.get("symbol") or ""))
    return {k for k in keys if k[0] or k[1]}


def compute_plan_coverage(planned_steps: list[dict], result: dict) -> dict:
    """Deterministic planned vs produced. No LLM.

    A planned key is covered if it appears in changes_by_file OR in
    skipped_changes with a reason. Extra produced keys are allowed.
    """
    planned = []
    planned_keys: set[tuple[str, str]] = set()
    for s in planned_steps or []:
        if not isinstance(s, dict):
            continue
        key = _norm_key(s.get("filename") or "", s.get("symbol") or "")
        if not (key[0] and key[1]):
            continue
        planned_keys.add(key)
        planned.append({"filename": s.get("filename") or "", "symbol": s.get("symbol") or ""})

    produced = _keys_from_changes_by_file((result or {}).get("changes_by_file"))
    skipped = _keys_from_skipped((result or {}).get("skipped_changes"))
    covered = planned_keys & (produced | skipped)
    missing = [
        {"filename": fn, "symbol": sym}
        for fn, sym in sorted(planned_keys - covered)
    ]
    extra = [
        {"filename": fn, "symbol": sym}
        for fn, sym in sorted(produced - planned_keys)
        if fn or sym
    ]
    return {
        "ok": len(missing) == 0,
        "covered": [{"filename": fn, "symbol": sym} for fn, sym in sorted(covered)],
        "missing": missing,
        "extra": extra,
        "skipped": [{"filename": fn, "symbol": sym} for fn, sym in sorted(skipped & planned_keys)],
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
