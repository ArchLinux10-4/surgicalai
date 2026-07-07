"""
Server-side multi-agent task runner (v2.0).

Executes a planned task run entirely on the backend, replacing the
browser-driven queue when the `server_task_runner` feature flag is ON.

Architecture
────────────
  supervisor (one asyncio task per run)
      │  builds "waves" of file-disjoint tasks (seq order preserved)
      ▼
  workers (one per task in the wave, run concurrently, capped)
      │  each worker = the exact same pipeline + QA gate as /execute-task
      ▼
  integration QA (one Sonnet pass over the combined run result)

Safety properties
─────────────────
- SCALING WARNING: the `_runs` registry below is an in-memory dict and
  assumes exactly one backend process. If this service is ever
  horizontally scaled to 2+ instances, a run tracked by one instance is
  invisible to another — status polling could 404 or show stale state
  depending on which instance handles the request. Fine for one instance;
  revisit with a shared store (e.g. DB-backed run table or Redis) before
  scaling out.
- Feature-flagged OFF by default (`server_task_runner` setting or
  SERVER_TASK_RUNNER env). When OFF, start_run() refuses and the client
  falls back to the existing browser-driven queue — zero behaviour change.
- Workers reuse the per-task idempotency guard semantics: only a task in
  status "pending" may execute. Claiming is atomic (single UPDATE ... WHERE
  status='pending'), so a duplicate supervisor can never double-run a task.
- Waves only parallelise tasks whose file targets are provably disjoint
  (plan_validator already merges same-file tasks at plan time). A task with
  no detectable file target runs alone. Seq order is never violated: wave
  building stops at the first conflict instead of skipping ahead.
- Every branch is _dlog'd. Every external call is wrapped; a failure
  degrades to the same blocked/halt semantics the client queue has today.
- The in-process registry prevents double-starting a run. Orphaned
  "running" tasks (process restart mid-run) are reset to pending on the
  next start_run() call, mirroring the SSE disconnect guard.
"""

import asyncio
import json
import os
import time

# ── Feature flag / config ──────────────────────────────────────────────────

_TRUTHY = ("1", "true", "on", "yes")


def server_runner_enabled() -> bool:
    """Feature flag: DB setting `server_task_runner`, else env SERVER_TASK_RUNNER."""
    try:
        from database import get_setting
        v = (get_setting("server_task_runner", "") or "").strip().lower()
        if not v:
            v = (os.environ.get("SERVER_TASK_RUNNER", "") or "").strip().lower()
        return v in _TRUTHY
    except Exception:
        return False


def _max_parallel() -> int:
    """Worker cap per wave. Defaults conservative; clamped to 1..4."""
    try:
        from database import get_setting
        raw = (get_setting("task_runner_max_parallel", "") or
               os.environ.get("TASK_RUNNER_MAX_PARALLEL", "") or "2")
        return max(1, min(4, int(raw)))
    except Exception:
        return 2


# ── In-process run registry ────────────────────────────────────────────────
# Maps run_id -> live state for /runs/status and double-start protection.
# In-process only (single backend service); a process restart clears it,
# which is exactly why start_run() also does orphan recovery.

_runs: dict = {}
_lock = asyncio.Lock()


def run_status(run_id: str) -> dict:
    entry = _runs.get(run_id)
    if not entry:
        return {"active": False, "run_id": run_id}
    return {"active": True, **{k: v for k, v in entry.items() if k != "task"}}


# ── Wave planning ──────────────────────────────────────────────────────────

def _task_file_basenames(task: dict) -> set:
    """Extract target-file basenames from a task's detail text.

    Reuses plan_validator's battle-tested extraction regex and its
    basename normalisation ("src/Foo.vue" vs "Foo.vue" are the same file).
    Returns an empty set when no file targets are detectable.
    """
    try:
        from services.plan_validator import _extract_files
        return {f.rsplit("/", 1)[-1] for f in _extract_files(task.get("detail", ""))}
    except Exception:
        return set()


def _build_wave(pending_sorted: list, cap: int) -> list:
    """Pick the next wave of tasks that may run concurrently.

    Rules (conservative by design):
    - Tasks are considered strictly in seq order; we stop at the first task
      that cannot join, never skip ahead (preserves plan ordering).
    - A task with NO detectable file targets always runs alone (we cannot
      prove it is independent, so we do not parallelise it).
    - A task joins the wave only if its file set is disjoint from every
      file already claimed by the wave.
    """
    wave, wave_files = [], set()
    for t in pending_sorted:
        files = _task_file_basenames(t)
        if not files:
            if not wave:
                wave.append(t)  # solo barrier task
            break
        if wave and not files.isdisjoint(wave_files):
            break  # first conflict ends the wave — no skipping ahead
        wave.append(t)
        wave_files |= files
        if len(wave) >= cap:
            break
    return wave


# ── Atomic task claim ──────────────────────────────────────────────────────

def _claim_task(task_id: str) -> bool:
    """Atomically flip a task pending -> running. Returns False if the task
    was not pending (already claimed/done/blocked/cancelled) — the server-side
    equivalent of the /execute-task idempotency guard."""
    from database import get_db_ctx
    with get_db_ctx() as conn:
        cur = conn.execute(
            "UPDATE agent_tasks SET status = 'running', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'pending'",
            (task_id,),
        )
        conn.commit()
        return (cur.rowcount or 0) > 0


# ── Worker: execute exactly one task ───────────────────────────────────────

async def _execute_one_task(session_id: str, run_id: str, task: dict,
                            total: int, user_id: str) -> str:
    """Run one task through the full pipeline + QA gate.

    Mirrors /execute-task's stream_one_task step for step, minus SSE.
    Returns the terminal status: done | blocked | cancelled | skipped.
    """
    from services.pipeline import run_natural_pipeline_stream, _dlog
    from services.task_planner import (
        update_task, cancel_requested_for_run, mark_pending_cancelled,
    )
    from routers.chat import (
        _eval_task_result, _extract_edit_summary, _build_prior_work_context,
        _save_task_message, _load_effective_memory,
    )
    from database import get_db_ctx

    task_id = task["id"]
    seq = task.get("seq", 0)
    t0 = time.time()

    # Atomic claim — the only door into execution. A task another worker or
    # a stray client stream already owns can never be double-run.
    if not _claim_task(task_id):
        _dlog("runner_task_claim_lost", session_id=session_id, run_id=run_id,
              task_id=task_id, task_seq=seq + 1)
        return "skipped"

    _dlog("runner_task_start", session_id=session_id, run_id=run_id,
          task_id=task_id, task_seq=seq + 1, task_total=total,
          title=(task.get("title") or "")[:80], user_id=user_id)

    try:
        # Fresh per-task session state — each task sees edits and the
        # compacted summary persisted by prior tasks (same as /execute-task).
        with get_db_ctx() as conn:
            sess_row = conn.execute(
                "SELECT session_summary FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            session_summary = (sess_row["session_summary"] if sess_row and sess_row["session_summary"] else "") or ""
            history = conn.execute(
                "SELECT role, content FROM chat_messages WHERE session_id = ? AND is_compacted = 0 ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            conversation_history = [{"role": r["role"], "content": r["content"]} for r in history]
            file_rows = conn.execute(
                "SELECT id, filename, content, language, lines, symbol_count, file_type FROM session_files WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            session_files = [dict(r) for r in file_rows]
            project_memory = _load_effective_memory(conn, session_id)

        # Prior-work context injection (same as /execute-task).
        try:
            prior_ctx = _build_prior_work_context(session_id, run_id, seq)
        except Exception as pex:
            _dlog("runner_prior_work_error", session_id=session_id,
                  task_id=task_id, error=str(pex)[:200])
            prior_ctx = ""
        task_request = (prior_ctx + "\n\n" + task["detail"]) if prior_ctx else task["detail"]

        task_kind = task.get("kind", "code")
        if not session_files and task_kind == "code":
            task_request = (
                "IMPORTANT: There are NO files uploaded in this session. "
                "You cannot make code edits without files. Tell the user "
                "to upload the relevant file(s) first, then retry.\n\n"
                + task_request
            )
            _dlog("runner_task_no_files", session_id=session_id, task_id=task_id,
                  task_seq=seq + 1, task_kind=task_kind)

        if session_files:
            fnames = [f["filename"] for f in session_files]
            task_request = (f"[{len(fnames)} file(s) attached: {', '.join(fnames)}. "
                            "Examine their contents before responding.]\n\n" + task_request)

        _dlog("runner_task_context", session_id=session_id, task_id=task_id,
              task_seq=seq + 1, has_prior_ctx=bool(prior_ctx),
              num_files=len(session_files), request_len=len(task_request))

        # Drive the pipeline. No SSE client — instead, persist a throttled
        # live progress line so the UI's 2.5s poll shows real-time status.
        collected, result_content, poll, aborted = [], None, 0, False
        think_parts: list = []  # accumulated extended-thinking text for this task
        last_progress_write = 0.0
        last_thinking_write = 0.0
        try:
            async for chunk in run_natural_pipeline_stream(
                session_files=session_files,
                user_request=task_request,
                conversation_history=conversation_history,
                session_id=session_id,
                project_memory=project_memory,
                session_summary=session_summary,
                user_id=user_id,
            ):
                poll += 1
                if poll % 20 == 0 and cancel_requested_for_run(session_id, run_id):
                    aborted = True
                    break
                if not chunk.startswith("data: "):
                    continue
                try:
                    d = json.loads(chunk[6:])
                    ct = d.get("type", "")
                    if ct in ("token", "chat"):
                        collected.append(d.get("content", ""))
                    elif ct == "smart_result":
                        result_content = d.get("content", "")
                    elif ct == "thinking":
                        # No SSE client on this path — persist the reasoning
                        # trail (throttled) so the UI's poll can show it live
                        # and it survives for post-run inspection.
                        tc = d.get("content", "")
                        if tc:
                            think_parts.append(tc)
                            now = time.time()
                            if now - last_thinking_write >= 3.0:
                                last_thinking_write = now
                                try:
                                    update_task(task_id, thinking=("".join(think_parts))[:24000])
                                except Exception as twx:
                                    _dlog("runner_thinking_write_error", task_id=task_id,
                                          error=str(twx)[:120])
                    elif ct == "progress":
                        now = time.time()
                        if now - last_progress_write >= 3.0:
                            last_progress_write = now
                            try:
                                update_task(task_id, result_summary=("⏳ " + d.get("content", ""))[:300])
                            except Exception as pwx:
                                _dlog("runner_progress_write_error", task_id=task_id,
                                      error=str(pwx)[:120])
                except Exception:
                    pass
        except Exception as ee:
            _dlog("runner_task_pipeline_error", session_id=session_id, run_id=run_id,
                  task_id=task_id, task_seq=seq + 1,
                  duration_s=round(time.time() - t0, 1), error=str(ee)[:300])
            update_task(task_id, status="blocked", verdict="error",
                        result_summary=("".join(collected))[:500],
                        thinking=("".join(think_parts))[:24000])
            mark_pending_cancelled(session_id, run_id)
            return "blocked"

        if aborted:
            update_task(task_id, status="cancelled")
            mark_pending_cancelled(session_id, run_id)
            _dlog("runner_task_cancelled", session_id=session_id, run_id=run_id,
                  task_id=task_id, task_seq=seq + 1,
                  duration_s=round(time.time() - t0, 1))
            return "cancelled"

        # Evaluate + persist — identical semantics to /execute-task.
        natural_text = "".join(collected).strip()
        parsed = None
        if result_content:
            try:
                parsed = json.loads(result_content)
            except Exception:
                parsed = None
        score, worst = _eval_task_result(parsed) if parsed else (None, "safe")
        try:
            _save_task_message(session_id, natural_text, parsed)
        except Exception as smx:
            _dlog("runner_save_message_error", session_id=session_id,
                  task_id=task_id, error=str(smx)[:200])

        is_answer = task.get("kind") == "answer"
        if parsed and worst == "blocked" and not is_answer:
            update_task(task_id, status="blocked", qa_score=score,
                        verdict=worst, result_summary=natural_text[:500],
                        thinking=("".join(think_parts))[:24000])
            mark_pending_cancelled(session_id, run_id)
            _dlog("runner_task_blocked", session_id=session_id, run_id=run_id,
                  task_id=task_id, task_seq=seq + 1, qa_score=score,
                  duration_s=round(time.time() - t0, 1))
            return "blocked"

        has_edits = False
        if parsed:
            for fd in (parsed.get("changes_by_file") or {}).values():
                if isinstance(fd, dict) and fd.get("changes"):
                    has_edits = True
                    break
        if is_answer:
            verdict = "skipped"
        elif not has_edits:
            verdict = "no_edits"
            _dlog("runner_task_no_edits", session_id=session_id,
                  task_id=task_id, task_seq=seq + 1, had_parsed=bool(parsed))
        else:
            verdict = worst or "safe"
        final_score = None if is_answer else score
        edit_summary = _extract_edit_summary(parsed) if parsed else ""
        rsummary = natural_text[:500]
        if edit_summary:
            rsummary += "\nEdited:\n" + edit_summary
        update_task(task_id, status="done", qa_score=final_score,
                    verdict=verdict, result_summary=rsummary[:800],
                    thinking=("".join(think_parts))[:24000])
        _dlog("runner_task_done", session_id=session_id, run_id=run_id,
              task_id=task_id, task_seq=seq + 1, qa_score=final_score,
              verdict=verdict, has_edits=has_edits,
              duration_s=round(time.time() - t0, 1))
        return "done"

    except Exception as ox:
        # Belt-and-braces: no exception may leave a task stuck in "running".
        _dlog("runner_task_outer_error", session_id=session_id, run_id=run_id,
              task_id=task_id, task_seq=seq + 1, error=str(ox)[:300])
        try:
            from services.task_planner import update_task as _ut, mark_pending_cancelled as _mpc
            _ut(task_id, status="blocked", verdict="error",
                result_summary=f"Internal runner error: {str(ox)[:300]}")
            _mpc(session_id, run_id)
        except Exception as fx:
            _dlog("runner_task_outer_error_unrecovered", task_id=task_id,
                  error=str(fx)[:200])
        return "blocked"


# ── Integration QA (post-run, cross-task review) ───────────────────────────

_INTEGRATION_QA_SYSTEM = """You are a senior integration reviewer. Several coding tasks were \
executed independently against the same project. Per-task QA already reviewed each change in \
isolation. Your ONLY job is to catch CROSS-TASK integration problems: one task renaming or \
removing something another task still uses, duplicated/conflicting additions, or a task that \
contradicts the overall goal.

Respond with ONLY a JSON object:
{"verdict": "pass" | "warning", "summary": "<one sentence>", "issues": ["<specific issue>", ...]}

Rules:
- "pass" with empty issues when the tasks compose cleanly.
- "warning" ONLY for concrete, evidence-based cross-task conflicts you can point to.
- Never invent issues. If unsure, pass."""


async def _run_integration_qa(session_id: str, run_id: str, user_id: str):
    """One Sonnet pass over the combined run result. Advisory only — it can
    surface warnings in chat but never blocks or reverts completed work.
    Any failure degrades to a silent skip (fully logged)."""
    from services.pipeline import _dlog
    from services.task_planner import list_tasks
    t0 = time.time()
    try:
        tasks = [t for t in list_tasks(session_id, run_id) if t.get("status") == "done"]
        edited = [t for t in tasks if "Edited:" in (t.get("result_summary") or "")]
        if len(edited) < 2:
            _dlog("runner_iqa_skipped", session_id=session_id, run_id=run_id,
                  done_tasks=len(tasks), edited_tasks=len(edited),
                  reason="fewer than 2 tasks produced edits")
            return

        lines = ["The run's goal, as its ordered task plan:"]
        for t in sorted(tasks, key=lambda x: x.get("seq", 0)):
            lines.append(f"\nTask {t.get('seq', 0) + 1}: {t.get('title', '')}")
            summary = (t.get("result_summary") or "").strip()
            if summary:
                lines.append(f"Result:\n{summary[:800]}")
        user_block = "\n".join(lines)

        from services.task_planner import _get_anthropic_key
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
        resp = await client.messages.create(
            model="claude-sonnet-5",  # QA is always Sonnet (upgraded from 4.5, matches pipeline QA sites)
            # 1000 → 4000: proven in run dd543a3a (pipeline QA, same model) that
            # a small budget can be fully consumed by a thinking block, leaving
            # zero text blocks. Here that would have parsed to {} and defaulted
            # the verdict to "pass" — a false green light.
            max_tokens=4000,
            system=_INTEGRATION_QA_SYSTEM,
            messages=[{"role": "user", "content": user_block}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        _block_types = [getattr(b, "type", "?") for b in resp.content]
        _dlog("runner_iqa_response", session_id=session_id, run_id=run_id,
              stop_reason=getattr(resp, "stop_reason", None),
              output_tokens=getattr(getattr(resp, "usage", None), "output_tokens", None),
              block_types=_block_types, raw_len=len(raw))

        from services.task_planner import _extract_json
        parsed = _extract_json(raw) or {}
        if not parsed:
            # Do NOT default an unparseable/empty QA response to "pass".
            _dlog("runner_iqa_unparseable", session_id=session_id, run_id=run_id,
                  raw_len=len(raw), raw_head=raw[:200])
            from routers.chat import _save_run_note
            _save_run_note(session_id,
                           "🔍 Integration QA could not run (model returned no "
                           "parseable result). Advisory only — run is unaffected.")
            return
        verdict = (parsed.get("verdict") or "pass").strip().lower()
        issues = [str(i).strip() for i in (parsed.get("issues") or []) if str(i).strip()]
        summary = (parsed.get("summary") or "").strip()
        _dlog("runner_iqa_done", session_id=session_id, run_id=run_id,
              verdict=verdict, issue_count=len(issues),
              duration_s=round(time.time() - t0, 1))

        from routers.chat import _save_run_note
        if verdict == "warning" and issues:
            note = ("🔍 **Integration QA** — potential cross-task issues to review:\n"
                    + "\n".join(f"- ⚠️ {i}" for i in issues[:6]))
            if summary:
                note += f"\n\n_{summary}_"
        else:
            note = "🔍 Integration QA passed — the tasks' changes are consistent with each other."
        _save_run_note(session_id, note)
    except Exception as ex:
        _dlog("runner_iqa_error", session_id=session_id, run_id=run_id,
              duration_s=round(time.time() - t0, 1), error=str(ex)[:300])
        # Advisory feature — never let it affect the completed run.


# ── Supervisor ─────────────────────────────────────────────────────────────

async def _supervise_run(session_id: str, run_id: str, user_id: str):
    """Drive the whole run: build waves, execute them, halt on block/cancel,
    write the final run note, then run integration QA."""
    from services.pipeline import _dlog
    from services.task_planner import list_tasks, cancel_requested_for_run, mark_pending_cancelled
    from routers.chat import _save_run_note, _run_summary_note

    t0 = time.time()
    cap = _max_parallel()
    wave_no = 0
    outcome = "done"
    blocked_title = None
    _dlog("runner_run_start", session_id=session_id, run_id=run_id,
          user_id=user_id, max_parallel=cap)

    try:
        while True:
            if cancel_requested_for_run(session_id, run_id):
                mark_pending_cancelled(session_id, run_id)
                outcome = "cancelled"
                _dlog("runner_run_cancel_flag", session_id=session_id, run_id=run_id,
                      wave=wave_no)
                break

            all_tasks = list_tasks(session_id, run_id)
            total = len(all_tasks)
            pending = sorted([t for t in all_tasks if t.get("status") == "pending"],
                             key=lambda t: t.get("seq", 0))
            if not pending:
                # Terminal: outcome depends on whether anything got blocked/cancelled.
                if any(t.get("status") == "blocked" for t in all_tasks):
                    outcome = "blocked"
                    bt = next((t for t in all_tasks if t.get("status") == "blocked"), None)
                    blocked_title = bt.get("title") if bt else None
                elif any(t.get("status") == "cancelled" for t in all_tasks):
                    outcome = "cancelled"
                _dlog("runner_run_no_pending", session_id=session_id, run_id=run_id,
                      wave=wave_no, outcome=outcome)
                break

            wave = _build_wave(pending, cap)
            if not wave:
                # Cannot happen (pending non-empty always yields ≥1), but never spin.
                _dlog("runner_wave_empty_guard", session_id=session_id, run_id=run_id,
                      wave=wave_no, pending=len(pending))
                outcome = "blocked"
                break
            wave_no += 1
            _dlog("runner_wave_start", session_id=session_id, run_id=run_id,
                  wave=wave_no, wave_size=len(wave), pending=len(pending),
                  task_seqs=[t.get("seq", 0) + 1 for t in wave],
                  parallel=len(wave) > 1)
            entry = _runs.get(run_id)
            if entry:
                entry.update({"wave": wave_no, "wave_size": len(wave),
                              "pending": len(pending), "total": total})

            wt0 = time.time()
            results = await asyncio.gather(
                *[_execute_one_task(session_id, run_id, t, total, user_id) for t in wave],
                return_exceptions=True,
            )
            statuses = []
            for t, r in zip(wave, results):
                if isinstance(r, BaseException):
                    # _execute_one_task's own belt-and-braces should make this
                    # unreachable, but a gather-level surprise must still halt safely.
                    _dlog("runner_wave_worker_exception", session_id=session_id,
                          run_id=run_id, wave=wave_no, task_id=t["id"],
                          error=str(r)[:300])
                    statuses.append("blocked")
                    blocked_title = blocked_title or t.get("title")
                else:
                    statuses.append(r)
                    if r == "blocked":
                        blocked_title = blocked_title or t.get("title")
            _dlog("runner_wave_done", session_id=session_id, run_id=run_id,
                  wave=wave_no, statuses=statuses,
                  duration_s=round(time.time() - wt0, 1))

            if "blocked" in statuses:
                outcome = "blocked"
                break
            if "cancelled" in statuses:
                outcome = "cancelled"
                break
            # done / skipped → next wave

        # Final run note — same wording/semantics as the client-driven path.
        try:
            note = _run_summary_note(session_id, run_id, status=outcome,
                                     blocked_title=blocked_title)
            _save_run_note(session_id, note)
        except Exception as nx:
            _dlog("runner_run_note_error", session_id=session_id, run_id=run_id,
                  error=str(nx)[:200])

        _dlog("runner_run_done", session_id=session_id, run_id=run_id,
              outcome=outcome, waves=wave_no,
              duration_s=round(time.time() - t0, 1))

        if outcome == "done":
            await _run_integration_qa(session_id, run_id, user_id)

    except Exception as sx:
        _dlog("runner_run_fatal", session_id=session_id, run_id=run_id,
              wave=wave_no, duration_s=round(time.time() - t0, 1),
              error=str(sx)[:300])
        try:
            mark_pending_cancelled(session_id, run_id)
            _save_run_note(session_id,
                           "🚫 Task run stopped by an internal error. "
                           "Completed tasks are saved; remaining tasks were cancelled.")
        except Exception:
            pass
    finally:
        _runs.pop(run_id, None)


# ── Public entry point ─────────────────────────────────────────────────────

async def start_run(session_id: str, run_id: str, user_id: str) -> dict:
    """Start (or resume) a run's server-side execution.

    Returns {ok, mode, ...}. mode: server | disabled | already_running | no_tasks.
    Safe to call on a fresh plan AND on an interrupted run (it simply executes
    whatever is still pending; the atomic claim makes re-execution impossible).
    """
    from services.pipeline import _dlog
    from services.task_planner import list_tasks, update_task

    if not server_runner_enabled():
        _dlog("runner_start_disabled", session_id=session_id, run_id=run_id)
        return {"ok": False, "mode": "disabled"}

    async with _lock:
        if run_id in _runs:
            _dlog("runner_start_already_running", session_id=session_id, run_id=run_id)
            return {"ok": False, "mode": "already_running"}

        tasks = list_tasks(session_id, run_id)
        # Orphan recovery: a process restart mid-run leaves tasks stuck in
        # "running" with no live supervisor. Since no supervisor for this run
        # exists in this process, those tasks are provably orphaned — reset
        # them so this start can pick them up (same rule as the SSE guard).
        orphans = [t for t in tasks if t.get("status") == "running"]
        for t in orphans:
            try:
                update_task(t["id"], status="pending")
                _dlog("runner_orphan_reset", session_id=session_id, run_id=run_id,
                      task_id=t["id"], task_seq=t.get("seq", 0) + 1)
            except Exception as ox:
                _dlog("runner_orphan_reset_error", session_id=session_id,
                      run_id=run_id, task_id=t["id"], error=str(ox)[:200])

        # Recompute after orphan resets so counts reflect reality.
        tasks = list_tasks(session_id, run_id)
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            _dlog("runner_start_no_tasks", session_id=session_id, run_id=run_id,
                  total=len(tasks))
            return {"ok": False, "mode": "no_tasks", "total": len(tasks)}

        sup = asyncio.create_task(_supervise_run(session_id, run_id, user_id))
        _runs[run_id] = {
            "run_id": run_id, "session_id": session_id, "user_id": user_id,
            "started_at": time.time(), "wave": 0, "wave_size": 0,
            "pending": len(pending), "total": len(tasks), "task": sup,
        }
        _dlog("runner_start_ok", session_id=session_id, run_id=run_id,
              user_id=user_id, total=len(tasks), pending=len(pending),
              orphans_reset=len(orphans), max_parallel=_max_parallel())
        return {"ok": True, "mode": "server", "total": len(tasks),
                "pending": len(pending), "max_parallel": _max_parallel()}
