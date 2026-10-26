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
        action = str(item.get("action") or "").strip().lower()
        if action not in ("create", "edit"):
            action = ""
        if action == "create" and filename and not symbol:
            symbol = filename
        if not filename or not symbol:
            continue
        key = _norm_key(filename, symbol)
        if key in seen:
            continue
        seen.add(key)
        step = {
            "filename": filename,
            "symbol": symbol,
            "description": description or f"Update {symbol} in {filename}",
        }
        if action == "create":
            step["action"] = "create"
        steps.append(step)
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
            "kind": "create" if s.get("action") == "create" else "code",
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


def module_assignment_window(content: str, symbol: str, radius: int = 20) -> str | None:
    """Numbered window around a module-level `SYMBOL =` line, or None.

    About 40 lines. Used when the symbol is not in the AST map, which is
    normal for a constant.
    """
    if not content or not symbol:
        return None
    match = re.search(rf"^[ \t]*{re.escape(symbol)}\s*=", content, re.M)
    if not match:
        return None
    lines = content.splitlines()
    line = content[:match.start()].count("\n")
    start = max(0, line - radius)
    end = min(len(lines), line + radius + 1)
    return "\n".join(f"{i + 1:5d}: {lines[i]}" for i in range(start, end))


_LINE_REF_RE = re.compile(r"\blines?\s+(\d+)(?:\s*[–-]\s*(\d+))?", re.I)
_VERSION_TOKEN_RE = re.compile(r"`([^`\n]{2,80})`|\"([^\"\n]{3,80})\"|“([^”\n]{3,80})”|([A-Za-z][\w\-]*\d[\w.]*)")


def _anchor_terms(symbol: str, description: str) -> list[str]:
    terms: list[str] = []
    sym = (symbol or "").strip()
    if sym:
        terms.append(sym)
    for match in _VERSION_TOKEN_RE.finditer(description or ""):
        term = next((g for g in match.groups() if g), "").strip().rstrip(".,;:")
        if len(term) >= 3 and term not in terms:
            terms.append(term)
    return terms


def text_anchor_window(
    content: str, symbol: str, description: str = "", radius: int = 30,
) -> dict | None:
    """Numbered window around text the step names, for non-symbol targets.

    An HTML id, a sentence in a template, or a markdown note is never in the
    symbol map. The planned name, a line number in the step, or a quoted or
    versioned token from the step (`gpt-4.1`) usually is in the file. Returns
    {"text", "start", "end", "line", "anchor", "other_lines"} with 1-based lines, or
    None when nothing the step names is in the file.
    """
    if not content:
        return None
    lines = content.splitlines()
    total = len(lines)
    if not total:
        return None
    low = content.casefold()
    center = None
    anchor = ""
    other: list[int] = []
    for term in _anchor_terms(symbol, description):
        idx = low.find(term.casefold())
        if idx < 0:
            continue
        center = content[:idx].count("\n")
        anchor = term
        pos = idx + len(term)
        while len(other) < 12:
            nxt = low.find(term.casefold(), pos)
            if nxt < 0:
                break
            line = content[:nxt].count("\n") + 1
            if line != center + 1 and line not in other:
                other.append(line)
            pos = nxt + len(term)
        break
    if center is None:
        match = _LINE_REF_RE.search(description or "")
        if match:
            first = int(match.group(1))
            if 1 <= first <= total:
                center = first - 1
                anchor = f"line {first}"
    if center is None:
        return None
    start = max(0, center - radius)
    end = min(total, center + radius + 1)
    text = "\n".join(f"{i + 1:5d}: {lines[i]}" for i in range(start, end))
    return {
        "text": text, "start": start + 1, "end": end, "line": center + 1,
        "anchor": anchor, "other_lines": other,
    }


def flag_unresolved_plan_steps(session_id: str, tasks: list[dict]) -> list[dict]:
    """Mark steps whose file is already staged but whose symbol is not in it.

    Files that are not staged yet are left alone: Implement can still load
    them from the open folder. Create steps are left alone.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT filename, content FROM session_files WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    by_name = {}
    for row in rows:
        try:
            by_name[row["filename"]] = row["content"] or ""
        except Exception:
            by_name[row[0]] = row[1] or ""
    if not by_name:
        return tasks
    try:
        from services.ast_parser import ASTParser
        parser = ASTParser()
    except Exception:
        parser = None
    for task in tasks:
        if (task.get("kind") or "") == "create":
            continue
        filename = task.get("filename") or ""
        symbol = task.get("symbol") or ""
        content = by_name.get(filename)
        if content is None or not symbol:
            continue
        found = False
        if parser is not None:
            try:
                smap = parser.parse(content, filename)
                for sym in getattr(smap, "symbols", []) or []:
                    if symbol in (getattr(sym, "name", ""), getattr(sym, "full_path", "")):
                        found = True
                        break
            except Exception:
                found = False
        if not found and module_assignment_window(content, symbol):
            found = True
        if not found and text_anchor_window(content, symbol, task.get("detail") or ""):
            found = True
        if found:
            continue
        note = f"Could not find `{symbol}` in `{filename}`."
        update_task(task["id"], verdict="unresolved", result_summary=note)
        task["verdict"] = "unresolved"
        task["result_summary"] = note
    return tasks


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
    # `b` is the produced symbol when coverage asks "does this edit cover
    # the planned name". TitleMatcher.__init__ covers TitleMatcher.
    # The reverse does not: a class edit does not cover one method.
    return (
        a.endswith("." + b) or b.endswith("." + a)
        or a.endswith("/" + b) or b.endswith("/" + a)
        or a.endswith("#" + b) or b.endswith("#" + a)
        or b.startswith(a + ".")
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
    created = flag_unresolved_plan_steps(session_id, created)
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
            step = {
                "filename": filename,
                "symbol": symbol,
                "description": t.get("detail") or "",
            }
            if (t.get("kind") or "") == "create":
                step["action"] = "create"
            plan.append(step)
    return plan


_BUILD_RE = re.compile(
    r"\b(?:build|implement|execute|carry out)\b(?:\s+\w+){0,4}\s+\bplan\b",
    re.IGNORECASE,
)
_BUILD_NEG_RE = re.compile(
    r"\b(?:don't|do not|never)\b.{0,40}\b(?:build|implement|execute)\b",
    re.IGNORECASE,
)


def message_requests_plan_build(text: str) -> bool:
    """True for 'build the plan' / 'implement this plan' / 'build off the plan'.
    Search, not full match: the Edit path prepends a file-attachment hint."""
    raw = text or ""
    if _BUILD_NEG_RE.search(raw):
        return False
    return _BUILD_RE.search(raw) is not None


def plan_build_user_request(session_id: str, steps: list[dict]) -> str:
    """Authoritative plan text. Not passed through the 4000-char history trim."""
    md = current_plan_markdown_for_prompt(session_id)
    checklist = json.dumps({"steps": steps}, indent=2)
    return (
        (md + "\n\n" if md else "")
        + "Saved implementation checklist (execute every step; this list is authoritative):\n"
        + checklist
        + "\n\nImplement the attached implementation_plan steps exactly. "
        + "Do not invent a different plan and do not emit an implementation_plan fence."
    )


def build_saved_plan(session_id: str) -> dict | None:
    """None unless the latest plan phase is ready or blocked and has steps."""
    latest = latest_plan_run(session_id)
    if not latest or latest.get("phase") not in ("ready", "blocked"):
        return None
    steps = forced_edit_plan_from_run(session_id, latest["run_id"])
    if not steps:
        return None
    return {
        "run_id": latest["run_id"],
        "steps": steps,
        "user_request": plan_build_user_request(session_id, steps),
    }


def _plan_tasks(session_id: str, run_id: str) -> list[dict]:
    tasks = [t for t in list_tasks(session_id, run_id) if (t.get("source") or "") == "plan"]
    tasks.sort(key=lambda t: t.get("seq") or 0)
    return tasks


def _find_plan_task(session_id: str, run_id: str, filename: str, symbol: str) -> dict | None:
    want = _norm_key(filename, symbol)
    for t in _plan_tasks(session_id, run_id):
        if _norm_key(t.get("filename") or "", t.get("symbol") or "") == want:
            return t
    return None


def activate_first_plan_step(session_id: str, run_id: str) -> list[dict]:
    """Mark only the first pending/blocked step running. Returns all plan tasks."""
    tasks = _plan_tasks(session_id, run_id)
    for t in tasks:
        if (t.get("status") or "") in ("pending", "blocked"):
            update_task(t["id"], status="running", result_summary="")
            t["status"] = "running"
            t["result_summary"] = ""
            break
    return tasks


def note_plan_step(session_id: str, run_id: str, filename: str, symbol: str, status: str, reason: str = "") -> dict | None:
    """Set one plan task to running, done, or blocked. None if no matching row."""
    if status not in ("running", "done", "blocked"):
        return None
    task = _find_plan_task(session_id, run_id, filename, symbol)
    if not task:
        return None
    fields = {"status": status}
    if status == "done":
        fields["verdict"] = "edited"
        fields["result_summary"] = ""
    elif status == "blocked":
        fields["verdict"] = "edit_failed"
        if reason:
            fields["result_summary"] = str(reason)[:300]
    update_task(task["id"], **fields)
    task.update(fields)
    return task


def mark_next_plan_step_running(session_id: str, run_id: str) -> dict | None:
    """Keep phase 'implementing' between steps. No-op if one is already running."""
    tasks = _plan_tasks(session_id, run_id)
    if any((t.get("status") or "") == "running" for t in tasks):
        return None
    for t in tasks:
        # A step that just failed is blocked. Promoting it again clears the
        # reason and makes the live line stick on the failure.
        if (t.get("status") or "") == "pending":
            update_task(t["id"], status="running", result_summary="")
            t["status"] = "running"
            t["result_summary"] = ""
            return t
    return None


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
            targeted = _text_region_target(ch)
            if targeted:
                keys.append((str(fn or ""), targeted))
    return keys


_TARGETED_RE = re.compile(r"model targeted '([^']+)'")


def _text_region_target(change: dict) -> str:
    """The planned name behind a promoted `_text_region_L…` edit.

    Promotion widens the edit to a real file window that contains the symbol
    the model was asked to edit, and records that name in the signature.
    """
    sym = change.get("symbol")
    if not isinstance(sym, dict):
        return ""
    name = str(sym.get("name") or "")
    if not name.startswith("_text_region_L"):
        return ""
    match = _TARGETED_RE.search(str(sym.get("signature") or ""))
    return match.group(1) if match else ""


def plain_skip_reason(item: dict) -> str:
    """One sentence a person can act on. No score, no file counts."""
    filename = (item or {}).get("filename") or "that file"
    symbol = (item or {}).get("symbol") or "that step"
    reason = str((item or {}).get("reason") or "")
    low = reason.lower()
    if reason.lower().startswith("edited "):
        return reason
    if low == "symbol not found":
        return f"Could not find `{symbol}` in `{filename}`."
    if "timed out" in low:
        return f"`{symbol}` in `{filename}` timed out before it produced an edit."
    if low in ("symbol_not_found", "file_not_parseable") or (
        "symbol" in low and "not found" in low
    ):
        return (
            f"Could not add `{symbol}` to `{filename}`. "
            "The edit pointed at the whole file instead of a line next to where it goes."
        )
    if low.startswith("create ") or "meant to be created" in low:
        return f"Could not create `{filename}`. {reason}"
    if "file not found" in low or low == "file_not_in_session":
        return (
            f"Could not edit `{filename}`. "
            "That file is not in this chat and it is not in the open folder."
        )
    if "time budget" in low or "deadline" in low:
        return f"`{symbol}` in `{filename}` did not run. The time budget ran out first."
    if reason:
        return f"Could not change `{symbol}` in `{filename}`. {reason}"
    return f"Could not change `{symbol}` in `{filename}`."


def _skip_is_already_done(reason: str) -> bool:
    """A skip that means the step is finished, not one that failed to run."""
    text = (reason or "").lower()
    return any(mark in text for mark in ("already", "no-op", "noop", "no change", "unchanged"))


def _skipped_pairs(skipped) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if not isinstance(skipped, list):
        return rows
    for item in skipped:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            continue
        fn = str(item.get("filename") or "")
        symbol = _extract_symbol(item)
        if fn or symbol:
            rows.append((fn, symbol, reason))
    return rows


def _keys_from_skipped(skipped) -> list[tuple[str, str]]:
    return [(fn, symbol) for fn, symbol, _reason in _skipped_pairs(skipped)]


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

    def _pairs(rows) -> set[tuple[str, str]]:
        return {
            _norm_key(str(r.get("filename") or ""), str(r.get("symbol") or ""))
            for r in rows or [] if isinstance(r, dict)
        }

    answered = _pairs((result or {}).get("plan_answered_steps"))
    created = _pairs((result or {}).get("plan_created_steps"))
    create_steps = [
        (str(s.get("filename") or ""), str(s.get("symbol") or ""))
        for s in planned_steps or []
        if isinstance(s, dict) and (
            s.get("action") == "create"
            or _norm_key(str(s.get("filename") or ""), str(s.get("symbol") or "")) in created
        )
    ]
    for item in (result or {}).get("new_files") or []:
        if not isinstance(item, dict):
            continue
        fn = str(item.get("filename") or "")
        if not fn:
            continue
        for planned_fn, planned_sym in create_steps:
            if _norm_filename(planned_fn) == _norm_filename(fn):
                produced.append((planned_fn, planned_sym))
    skipped_rows = _skipped_pairs((result or {}).get("skipped_changes"))
    done_skips = [(fn, sym) for fn, sym, reason in skipped_rows if _skip_is_already_done(reason)]
    failed_skips = [
        (fn, sym, reason) for fn, sym, reason in skipped_rows if not _skip_is_already_done(reason)
    ]
    pool = produced + done_skips

    covered = [p for p in planned if any(_keys_compatible(p, q) for q in pool)]
    pending_rows = []
    for fn, sym, reason in failed_skips:
        pair = (fn, sym)
        if any(_keys_compatible(pair, p) for p in planned) and not any(
            _keys_compatible(pair, c) for c in covered
        ):
            pending_rows.append((fn, sym, reason))
    pending_pairs = [(fn, sym) for fn, sym, _reason in pending_rows]
    missing = [
        p for p in planned
        if p not in covered and not any(_keys_compatible(p, q) for q in pending_pairs)
    ]
    still_missing = []
    for planned_fn, planned_sym in missing:
        others = []
        for got_fn, got_sym in produced:
            if not _files_compatible(planned_fn, got_fn):
                continue
            if _symbols_compatible(planned_sym, got_sym):
                continue
            if got_sym and got_sym not in others:
                others.append(got_sym)
        # The step's own focused call returned this edit, so a change in that
        # file that no other planned step claims is this step's answer, even
        # when the model named the function it delegates to or a heading.
        unclaimed = [
            o for o in others
            if not any(
                _keys_compatible((planned_fn, o), p)
                for p in planned if p != (planned_fn, planned_sym)
            )
        ]
        if unclaimed and _norm_key(planned_fn, planned_sym) in answered:
            covered.append((planned_fn, planned_sym))
        elif others:
            pending_rows.append((
                planned_fn,
                planned_sym,
                f"Edited `{others[0]}` instead of `{planned_sym}`.",
            ))
        else:
            still_missing.append((planned_fn, planned_sym))
    missing = still_missing
    extra = [q for q in produced if not any(_keys_compatible(q, p) for p in planned)]
    skipped_hit = [q for q in done_skips if any(_keys_compatible(q, p) for p in planned)]
    return {
        "ok": len(missing) == 0 and len(pending_rows) == 0,
        "covered": [{"filename": fn, "symbol": sym} for fn, sym in covered],
        "missing": [{"filename": fn, "symbol": sym} for fn, sym in missing],
        "pending": [
            {"filename": fn, "symbol": sym, "reason": reason}
            for fn, sym, reason in pending_rows
        ],
        "extra": [{"filename": fn, "symbol": sym} for fn, sym in extra],
        "skipped": [{"filename": fn, "symbol": sym} for fn, sym in skipped_hit],
    }


def apply_coverage_to_run(session_id: str, run_id: str, coverage: dict) -> list[dict]:
    """Mark each source=plan task done or blocked from coverage keys."""
    missing = {
        _norm_key(m.get("filename") or "", m.get("symbol") or "")
        for m in (coverage or {}).get("missing") or []
    }
    pending = {}
    for item in (coverage or {}).get("pending") or []:
        pending[_norm_key(item.get("filename") or "", item.get("symbol") or "")] = (
            item.get("reason") or "This step still needs work."
        )
    updated = []
    for t in list_tasks(session_id, run_id):
        if (t.get("source") or "") != "plan":
            continue
        key = _task_key(t)
        if key in pending:
            reason = plain_skip_reason({
                "filename": t.get("filename") or "",
                "symbol": t.get("symbol") or "",
                "reason": pending[key],
            })
            update_task(t["id"], status="pending", verdict="", result_summary=reason)
            t["status"] = "pending"
            t["verdict"] = ""
            t["result_summary"] = reason
        elif key in missing:
            update_task(
                t["id"],
                status="blocked",
                verdict="plan_coverage",
                result_summary="Planned step was not produced and was not skipped with a reason.",
            )
            t["status"] = "blocked"
            t["verdict"] = "plan_coverage"
        else:
            update_task(t["id"], status="done", verdict="covered", result_summary="")
            t["status"] = "done"
            t["verdict"] = "covered"
            t["result_summary"] = ""
        updated.append(t)
    updated.sort(key=lambda r: r.get("seq") or 0)
    return updated


def coverage_after_crash(planned_steps: list[dict], result: dict, reason: str) -> dict:
    """Coverage for a build that crashed after some edits were checkpointed.

    Steps the checkpoint does not contain are pending with the crash reason.
    They are not "not produced".
    """
    note = (reason or "Build crashed before this step finished.").strip()[:300]
    cov = compute_plan_coverage(planned_steps, result or {})
    if not cov.get("missing"):
        return cov
    merged = dict(result or {})
    skipped = list(merged.get("skipped_changes") or [])
    for item in cov["missing"]:
        skipped.append({
            "filename": item.get("filename") or "",
            "symbol": item.get("symbol") or "",
            "reason": note,
        })
    merged["skipped_changes"] = skipped
    return compute_plan_coverage(planned_steps, merged)


def release_unshipped_plan_steps(session_id: str, run_id: str, reason: str) -> list[dict]:
    """Put a crashed build back to pending, including in-memory "edited" steps.

    note_plan_step marks a step done as soon as its edit resolves, before any
    diff is delivered. forced_edit_plan_from_run skips done steps, so leaving
    those marks would drop the edits on Retry. Verdict "covered" is a finished
    coverage pass and stays done.
    """
    note = (reason or "Build crashed before a result was delivered.").strip()[:300]
    updated = []
    for t in list_tasks(session_id, run_id):
        if (t.get("source") or "") != "plan":
            continue
        status = t.get("status") or ""
        verdict = t.get("verdict") or ""
        unshipped = status in ("running", "pending") or (
            status == "done" and verdict == "edited"
        )
        if unshipped:
            update_task(t["id"], status="pending", verdict="", result_summary=note)
            t["status"] = "pending"
            t["verdict"] = ""
            t["result_summary"] = note
        updated.append(t)
    updated.sort(key=lambda r: r.get("seq") or 0)
    return updated
