"""Grok (xAI) native tool-calling adapter for Ask/Plan mode streaming.

WHY THIS MODULE EXISTS
-----------------------
``run_chat_stream``'s Ask/Plan tool loop (services/pipeline.py,
``run_chat_stream``) drives every non-Claude backend (GPT, Gemini, Grok) through
the SAME text-tag contract Edit mode originally used before PR #116:
``<search_request>``/``<file_request>``/``<github_request>`` written as plain
text into the model's stream, scanned out with a regex.

PR #116 already proved — and fixed, for Edit/Agent mode only — that Grok
(grok-4.5) *narrates instead of emitting the tags* ("I would search for X..."
in prose, never a parseable ``<search_request>`` block), so the tag scanner
never fires and Grok effectively loses its lookup tools. That module
(``services/grok_agent_tools.py``) already ships fully generic, ask/plan-aware
native-tool schemas and instruction builders (``build_grok_agent_tools``,
``build_grok_system_suffix``, ``build_grok_agent_instruction`` all accept
``mode="ask"``/``mode="plan"``) — they were simply never wired into the Ask/Plan
streaming branch. This module is that wiring, kept in its own file so nothing
here touches the Claude or GPT/Gemini tag-loop code paths in pipeline.py.

SECOND, RELATED BUG THIS MODULE FIXES: Ask mode's only defense against Grok
"jumping to code" is a single soft negative line
(``chat.py:_ASK_DIRECTIVE``/``_PLAN_DIRECTIVE``) prepended once to the user's
turn. Documented prompt-engineering practice (primacy + recency: place a key
constraint at BOTH the start and the end of the prompt, not once in the
middle) and xAI's own reasoning-model behavior (more proactive/agentic than
Claude, per xAI docs + community reports) make a single soft mid-prompt
instruction weaker for Grok specifically. This module appends a short, blunt
Grok-only reinforcement at the very END of the outgoing message (see
``_GROK_ASK_PLAN_REINFORCEMENT`` below) — Claude/GPT/Gemini prompts are
completely untouched.

INTEGRATION CONTRACT (verified against the real pipeline.py at integration
time — see ``run_chat_stream``, ~line 3990 in services/pipeline.py)
---------------------------------------------------------------------------
Only reachable from an ``if _is_grok_model(chat_model):`` branch. Callers must
inject (no imports of services.pipeline here, to avoid a circular import):

* ``client``               — an OpenAI-SDK client already routed to xAI
                              (``get_grok_client``/``_get_client_for_model``).
* ``chat_create_fn``        — production passes a wrapper around
                              ``grok_chat_create_with_billing_guard``. Tests
                              inject a fake. This function builds the kwargs
                              (no temperature, ``tool_choice="auto"``).
* ``iter_chunks_fn``        — pipeline.py's ``_iter_openai_stream_chunks``
                              (empty-``choices[]`` guard, gap #5).
* ``execute_tool_round_fn`` — pipeline.py's ``_ask_plan_execute_tool_round``,
                              called with duck-typed ``_FakeMatch`` wrappers so
                              it runs completely unmodified (its real regex
                              path is untouched).

LOGGING
-------
Every branch calls ``dlog`` (the caller's ``_dlog``) — required by project
convention, and essential here since this is a brand-new code path.
"""
import asyncio
import json
import os
import re
import time
from typing import Callable, Optional

from services.grok_agent_tools import (
    build_grok_agent_tools,
    build_grok_system_suffix,
    build_grok_agent_instruction,
    StreamedToolCallAccumulator,
    translate_tool_calls,
    build_native_followup_messages,
    format_tool_call_progress,
    aiter_sync,
    extract_reasoning_delta,
    summarize_translation_progress,
)


def _noop_dlog(*args, **kwargs):
    pass


class _FakeMatch:
    """Duck-types ``re.Match.group(1)`` so ``_ask_plan_execute_tool_round``
    (shared with the Claude-native and GPT/Gemini tag loops) can be reused
    completely unmodified — it never learns whether its input came from a
    regex or a native tool call.
    """

    __slots__ = ("_body",)

    def __init__(self, body: str):
        self._body = body

    def group(self, _n):
        return self._body


_GROK_ASK_PLAN_REINFORCEMENT_TMPL = (
    "\n\n[REMINDER — {label} MODE, read this last instruction carefully] "
    "This is a discussion, not an implementation request. Do not write "
    "diffs or <surgical_edit>/<new_file> tags in your final answer — explain, "
    "analyze, and cite the relevant code instead.{plan_extra} If the user "
    "actually wants the change made, tell them to use Implement on the Plan "
    "tracker or switch to Edit/Agent mode."
    "{plan_json}"
)


def build_grok_ask_plan_reinforcement(mode: str) -> str:
    """Return the Grok-only trailing reinforcement for Ask/Plan mode.

    Placed at the very END of the outgoing user turn (recency effect) as a
    second copy of the "don't write code" constraint already prepended once
    by ``chat.py:_mode_directive`` (primacy effect) — the sandwich pattern.
    Pure string builder, no side effects; safe to unit test directly.
    """
    label = "PLAN" if (mode or "").strip().lower() == "plan" else "ASK"
    plan_json = ""
    plan_extra = (
        " A short illustrative snippet is fine if the user needs it to follow "
        "the explanation, but you are not implementing anything here."
    )
    if label == "PLAN":
        plan_extra = (
            " Include ## Best practices and fenced code examples of the code "
            "that should be written under each step (in the markdown body, "
            "before the fence). Those examples are illustrative only."
        )
        plan_json = (
            " Unless you are asking clarifying questions as allowed in the "
            "PLAN QUESTIONS section, you MUST end with a fenced "
            "```implementation_plan JSON block "
            "listing every step as {\"filename\",\"symbol\",\"description\"}. "
            "No edits — the fence is the machine-readable plan only."
        )
    return _GROK_ASK_PLAN_REINFORCEMENT_TMPL.format(
        label=label, plan_json=plan_json, plan_extra=plan_extra,
    )


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


_LOOKUP_PROMISE = re.compile(
    r"\b(i can|i'll|i will|let me)\b.{0,48}\b(check|look|search|find)\b",
    re.IGNORECASE,
)


def is_lookup_promise(text: str) -> bool:
    """A short reply that only promises to look, with no file or answer in it."""
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 240:
        return False
    if "/" in cleaned or cleaned.endswith(".py") or ".tsx" in cleaned or ".jsx" in cleaned:
        return False
    return _LOOKUP_PROMISE.search(cleaned) is not None


def _dispatch_context_request(
    tag_kind: str,
    body: str,
    *,
    execute_tool_round_fn: Callable,
    symbol_maps_by_name: dict,
    file_content_lookup: dict,
    user_id: str,
    session_id: str,
    tool_round: int,
    activity=None,
) -> str:
    """Route a translated native context request through the existing,
    unmodified ``_ask_plan_execute_tool_round`` via a duck-typed match.

    All-modes rollout (additive): ``"history"`` is the tag_kind
    ``services/grok_agent_tools.py``'s ``_context_body()`` already emits for
    ``TOOL_REQUEST_HISTORY`` (that module was ALWAYS history-aware — it just
    was never called with ``history_enabled=True`` from this file until now,
    see ``run_grok_ask_plan_native_stream`` below). Mapped to ``hr_match``,
    the same duck-typed contract ``_ask_plan_execute_tool_round`` already
    accepts for the Claude/GPT/Gemini tag loop.
    """
    match = _FakeMatch(body)
    sr_match = match if tag_kind == "search" else None
    fr_match = match if tag_kind == "filereq" else None
    gr_match = match if tag_kind == "github" else None
    hr_match = match if tag_kind == "history" else None
    rr_match = match if tag_kind == "repo" else None
    tool_parts = execute_tool_round_fn(
        sr_match=sr_match, fr_match=fr_match, gr_match=gr_match,
        hr_match=hr_match, rr_match=rr_match,
        symbol_maps_by_name=symbol_maps_by_name,
        file_content_lookup=file_content_lookup,
        user_id=user_id, session_id=session_id, tool_round=tool_round,
        **({"activity": activity} if activity is not None else {}),
    )
    return "\n\n".join(tool_parts) if tool_parts else (
        f"No result for this {tag_kind} request."
    )


def _ask_plan_call_kwargs(chat_model, tools, dlog, session_id, user_id):
    """Edit-shaped Grok request: no temperature, tool_choice auto, output cap.

    ``strip_unsupported_params`` drops ``stop`` and the penalty params xAI
    rejects. Temperature is omitted on purpose — Edit mode does not send it.
    """
    from services.grok_provider import strip_unsupported_params

    raw = {"tools": tools, "stream": True, "tool_choice": "auto"}
    try:
        from services.pipeline import _max_output_tokens
        cap = _max_output_tokens(chat_model)
        if cap:
            raw["max_tokens"] = cap
    except Exception as exc:
        dlog("grok_ask_plan_max_tokens_unavailable", session_id=session_id,
             user_id=user_id, error_type=type(exc).__name__, error=str(exc)[:200])
    return strip_unsupported_params(
        raw, dlog=dlog, session_id=session_id, user_id=user_id)


async def run_grok_ask_plan_native_stream(
    *,
    client,
    chat_model: str,
    all_messages: list,
    mode: str,
    gh_nat_enabled: bool,
    gh_known_repos: list,
    symbol_maps_by_name: dict,
    file_content_lookup: dict,
    execute_tool_round_fn: Callable,
    chat_create_fn: Callable,
    iter_chunks_fn: Callable,
    max_rounds: int,
    deadline_s: float,
    session_id: str,
    user_id: str,
    dlog: Optional[Callable] = None,
    history_enabled: bool = False,
    aws_enabled: bool = False,
    websearch_enabled: bool = False,
    mcp_enabled: bool = False,
    repo_enabled: bool = False,
    git_enabled: bool = False,
    local_enabled: bool = False,
    openai_client=None,
    web_search_sources: Optional[list] = None,
    client_inbox=None,
    steer_inbox=None,
):
    """Native-tool-calling Ask/Plan loop for Grok. Yields raw SSE strings.

    Structurally mirrors the GPT/Gemini tag loop in ``run_chat_stream``
    (same round cap, same deadline, same progress/token event shape) but
    drives Grok through real ``tools=[...]`` function calling instead of the
    tag protocol PR #116 proved unreliable for this model family.

    ``history_enabled`` (all-modes rollout, additive, defaults False so any
    existing caller/test that omits it is byte-for-byte unaffected): gates
    the native ``request_history`` tool exactly like ``gh_nat_enabled`` gates
    ``request_github`` below. ``build_grok_agent_tools``/``build_grok_system_suffix``
    (services/grok_agent_tools.py) were ALREADY fully history-aware — Edit/
    Agent mode has called them with a real ``history_enabled`` value all
    along — this module was simply the one remaining caller still hardcoding
    ``False``.
    """
    dlog = dlog or _noop_dlog
    t0 = time.time()

    tools = build_grok_agent_tools(
        mode=mode, github_enabled=gh_nat_enabled, history_enabled=history_enabled,
        aws_enabled=aws_enabled, websearch_enabled=websearch_enabled,
        mcp_enabled=mcp_enabled, repo_enabled=repo_enabled,
        git_enabled=git_enabled,
        local_enabled=local_enabled,
        dlog=dlog, session_id=session_id, user_id=user_id,
    )

    # Rewrite the system message: append the native-tool suffix that tells
    # Grok to CALL tools instead of writing the XML tags the shared
    # system prompt describes (that shared text targets Claude/GPT and is
    # left completely untouched).
    messages = list(all_messages)
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            suffix = build_grok_system_suffix(
                mode=mode, github_enabled=gh_nat_enabled, history_enabled=history_enabled,
                aws_enabled=aws_enabled, websearch_enabled=websearch_enabled,
                mcp_enabled=mcp_enabled, repo_enabled=repo_enabled,
                git_enabled=git_enabled,
                local_enabled=local_enabled,
                dlog=dlog, session_id=session_id, user_id=user_id,
            )
            _sys = m.get("content") or ""
            _sys = re.sub(
                r"Emit ONLY ONE tag at a time, then STOP your response immediately —"
                r"[\s\S]*?no tags in your final answer\)\.",
                "Use the function tools. Do not write XML tags.",
                _sys,
            )
            _sys = _sys.replace(
                "Emit one tag per turn. You get the result next turn. ",
                "",
            )
            messages[i] = {**m, "content": _sys + suffix}
            break

    # Sandwich reinforcement: append to the END of the last user turn.
    # chat.py already prepended _ASK_DIRECTIVE/_PLAN_DIRECTIVE to the front
    # of this same message (primacy) — this is the matching recency copy,
    # Grok-only, built fresh in this file so Claude/GPT/Gemini prompts never
    # change shape.
    reinforcement = build_grok_ask_plan_reinforcement(mode)
    if messages and messages[-1].get("role") == "user":
        messages[-1] = {
            **messages[-1],
            "content": (messages[-1].get("content") or "") + reinforcement,
        }
    else:
        messages.append({"role": "user", "content": reinforcement})

    dlog("grok_ask_plan_native_start", session_id=session_id, user_id=user_id,
         model=chat_model, mode=mode, github_enabled=gh_nat_enabled,
         known_repos=gh_known_repos, tool_count=len(tools),
         max_rounds=max_rounds, deadline_s=deadline_s)

    tool_round = 0
    _aws_calls = 0
    _git_calls = 0
    _local_calls = 0
    _mcp_calls = 0
    _promise_nudged = False
    _prose_break_pending = False
    _prose_needs_break = False

    def _emit_prose(content: str) -> str:
        nonlocal _prose_break_pending, _prose_needs_break
        from services.prose_break import separate_turn_prose
        content, _prose_break_pending, _prose_needs_break = separate_turn_prose(
            content, break_pending=_prose_break_pending, needs_break=_prose_needs_break,
        )
        return content
    if steer_inbox is not None:
        yield _sse({"type": "steer_ready"})
    _ws_budget = None
    if websearch_enabled:
        from services.openai_web_search import SearchBudget as _WsBudget
        _ws_budget = _WsBudget()
    from services.tool_activity import ToolTracker as _ToolTracker
    _tracker = _ToolTracker(agent=chat_model, dlog=dlog)
    _end_reason = "incomplete"
    _stream_retries = 0
    try:
        while True:
            if tool_round > 0 and _prose_needs_break:
                _prose_break_pending = True
            elapsed = time.time() - t0
            if elapsed >= deadline_s or tool_round >= max_rounds:
                reason = "budget_exhausted_deadline" if elapsed >= deadline_s else "budget_exhausted_rounds"
                dlog("grok_ask_plan_native_budget_exhausted", session_id=session_id,
                     user_id=user_id, tool_round=tool_round, elapsed_s=round(elapsed, 1),
                     reason=reason)
                yield _sse({"type": "token", "content": _emit_prose(
                    "I looked at the available code but couldn't fully answer within "
                    "my lookup budget — try asking about a more specific file or function."
                )})
                _end_reason = "budget"
                break

            if tool_round > 0 and steer_inbox is not None:
                from services.steering import apply_steer_at_boundary as _apply_steer
                messages, _steer_evs = _apply_steer(messages, steer_inbox, session_id or "")
                for _sev in _steer_evs:
                    yield _sse(_sev)

            dlog("grok_ask_plan_native_round_start", session_id=session_id,
                 user_id=user_id, model=chat_model, mode=mode, tool_round=tool_round)

            if tool_round > 0:
                yield _sse({"type": "progress",
                            "content": f"Continuing… round {tool_round + 1}"})

            from services.grok_retry import (
                grok_api_error_fields as _err_fields,
                grok_request_fields as _req_fields,
                is_transient_grok_error as _transient,
            )
            call_kwargs = _ask_plan_call_kwargs(
                chat_model, tools, dlog, session_id, user_id)
            dlog("grok_request_built", session_id=session_id, user_id=user_id,
                 **_req_fields(messages, call_kwargs, model=chat_model,
                               mode=mode, turn=tool_round))

            round_text = ""
            reasoning_text = ""
            acc = StreamedToolCallAccumulator(dlog=dlog, session_id=session_id, user_id=user_id)
            round_t0 = time.time()
            last_activity_ts = round_t0
            last_silence_progress_ts = 0.0
            thinking_open = False
            thinking_deltas = 0
            finish_reason = None
            silence_s = int(os.getenv("GROK_SILENCE_PROGRESS_S", "3"))
            phase = "create"
            try:
                stream = chat_create_fn(
                    client, model=chat_model, messages=messages, **call_kwargs,
                )
                phase = "stream"
                async for chunk in aiter_sync(iter_chunks_fn(
                        stream, model=chat_model, session_id=session_id,
                        user_id=user_id)):
                    now = time.time()
                    if ((now - last_activity_ts) >= silence_s
                            and (now - last_silence_progress_ts) >= silence_s):
                        last_silence_progress_ts = now
                        yield _sse({"type": "progress",
                                    "content": f"Reasoning… {int(now - round_t0)}s"})
                        dlog("grok_agent_silence_heartbeat", session_id=session_id,
                             user_id=user_id, tool_round=tool_round,
                             elapsed_s=int(now - round_t0), mode=mode)

                    choice = chunk.choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    rc = extract_reasoning_delta(delta)
                    if rc:
                        last_activity_ts = time.time()
                        thinking_deltas += 1
                        reasoning_text = (reasoning_text + rc)[:2000]
                        if not thinking_open:
                            thinking_open = True
                            yield _sse({"type": "thinking_start", "content": ""})
                        yield _sse({"type": "thinking", "content": rc})

                    content = getattr(delta, "content", None)
                    if content:
                        last_activity_ts = time.time()
                        if thinking_open:
                            thinking_open = False
                            yield _sse({"type": "thinking_end", "content": ""})
                        round_text += content
                    tc_delta = getattr(delta, "tool_calls", None)
                    if tc_delta:
                        named = acc.add_delta(tc_delta) or []
                        if named:
                            last_activity_ts = time.time()
                            if thinking_open:
                                thinking_open = False
                                yield _sse({"type": "thinking_end", "content": ""})
                            for tn in named:
                                yield _sse({"type": "progress",
                                            "content": format_tool_call_progress(tn)})
                                dlog("grok_agent_tool_named", session_id=session_id,
                                     user_id=user_id, tool_round=tool_round,
                                     tool_name=tn, mode=mode)
            except (GeneratorExit, asyncio.CancelledError) as disc:
                _end_reason = "disconnect"
                dlog("grok_round_end", session_id=session_id, user_id=user_id,
                     model=chat_model, mode=mode, turn=tool_round, outcome="disconnect",
                     elapsed_s=round(time.time() - round_t0, 1),
                     text_chars=len(round_text), thinking_chars=len(reasoning_text),
                     tool_names=[], finish_reason=finish_reason,
                     error_type=type(disc).__name__, phase=phase)
                raise
            except Exception as api_err:
                produced = bool(round_text or reasoning_text or acc.has_calls())
                dlog("grok_round_api_error", session_id=session_id, user_id=user_id,
                     model=chat_model, mode=mode, turn=tool_round, phase=phase,
                     **_err_fields(api_err))
                if (not produced and phase == "stream" and _stream_retries < 2
                        and _transient(api_err)):
                    _stream_retries += 1
                    wait_s = (1, 3)[min(_stream_retries - 1, 1)]
                    dlog("grok_ask_plan_pre_output_retry", session_id=session_id,
                         user_id=user_id, model=chat_model, mode=mode,
                         tool_round=tool_round, attempt=_stream_retries, wait_s=wait_s)
                    await asyncio.sleep(wait_s)
                    continue
                _end_reason = "error"
                dlog("grok_round_end", session_id=session_id, user_id=user_id,
                     model=chat_model, mode=mode, turn=tool_round, outcome="error",
                     elapsed_s=round(time.time() - round_t0, 1),
                     text_chars=len(round_text), thinking_chars=len(reasoning_text),
                     tool_names=[], finish_reason=finish_reason, phase=phase)
                raise

            _stream_retries = 0
            if thinking_open:
                yield _sse({"type": "thinking_end", "content": ""})
            if reasoning_text:
                dlog("grok_reasoning_preview", session_id=session_id, user_id=user_id,
                     model=chat_model, mode=mode, turn=tool_round,
                     chars=len(reasoning_text), preview=reasoning_text[:400])

            if not acc.has_calls():
                # Plain-text answer, no tool calls this round — done, unless the
                # model only promised to look. Session dff697c8: gpt-6-luna said
                # "I can check" and the tag loop treated that as the answer.
                final_text = round_text.strip() or (
                    "I don't have anything more to add — try rephrasing your question."
                )
                if (not _promise_nudged and (gh_nat_enabled or repo_enabled)
                        and is_lookup_promise(final_text)):
                    _promise_nudged = True
                    from services.github_natural_tag import LOOKUP_BEFORE_ANSWER
                    dlog("ask_plan_lookup_promise_rejected", session_id=session_id,
                         user_id=user_id, tool_round=tool_round, chars=len(final_text))
                    dlog("grok_round_end", session_id=session_id, user_id=user_id,
                         model=chat_model, mode=mode, turn=tool_round, outcome="nudge",
                         elapsed_s=round(time.time() - round_t0, 1),
                         text_chars=len(round_text), thinking_chars=len(reasoning_text),
                         tool_names=[], finish_reason=finish_reason)
                    yield _sse({"type": "progress", "content": "Looking it up…"})
                    messages.append({"role": "assistant", "content": final_text})
                    messages.append({
                        "role": "user",
                        "content": "That is not an answer. " + LOOKUP_BEFORE_ANSWER,
                    })
                    continue
                dlog("grok_ask_plan_native_final_answer", session_id=session_id,
                     user_id=user_id, tool_round=tool_round, chars=len(final_text),
                     thinking_deltas=thinking_deltas)
                dlog("grok_round_end", session_id=session_id, user_id=user_id,
                     model=chat_model, mode=mode, turn=tool_round, outcome="answer",
                     elapsed_s=round(time.time() - round_t0, 1),
                     text_chars=len(round_text), thinking_chars=len(reasoning_text),
                     tool_names=[], finish_reason=finish_reason)
                _end_reason = "answer"
                yield _sse({"type": "token", "content": _emit_prose(final_text)})
                break

            calls = acc.finalize()
            dlog("grok_round_end", session_id=session_id, user_id=user_id,
                 model=chat_model, mode=mode, turn=tool_round, outcome="tool",
                 elapsed_s=round(time.time() - round_t0, 1),
                 text_chars=len(round_text), thinking_chars=len(reasoning_text),
                 tool_names=[c.get("name") for c in calls], finish_reason=finish_reason)
            translated = translate_tool_calls(calls, dlog=dlog, session_id=session_id,
                                              user_id=user_id)
            sum_prog = summarize_translation_progress(translated)
            if sum_prog:
                yield _sse({"type": "progress", "content": sum_prog})
                dlog("grok_agent_tool_progress", session_id=session_id,
                     user_id=user_id, tool_round=tool_round, summary=sum_prog[:200],
                     mode=mode)

            if translated.blocked_reason:
                dlog("grok_ask_plan_native_blocked", session_id=session_id,
                     user_id=user_id, tool_round=tool_round,
                     reason=translated.blocked_reason[:200])
                blocked_text = round_text.strip()
                blocked_text = (
                    f"{blocked_text}\n\n{translated.blocked_reason}" if blocked_text
                    else translated.blocked_reason
                )
                yield _sse({"type": "token", "content": _emit_prose(blocked_text)})
                _end_reason = "blocked"
                break

            context_result = None
            _tool_open = None
            if translated.context_request is not None:
                tag_kind, body = translated.context_request
                dlog("agent_tool_dispatch_start", session_id=session_id, user_id=user_id,
                     mode=mode, turn=tool_round, kind=tag_kind,
                     arg_preview=str(body)[:180])
                _tool_open = _tracker.start(tag_kind, body, turn=tool_round)
                if _tool_open:
                    yield _sse(_tool_open)
                if tag_kind == "websearch":
                    from services.openai_web_search import (
                        INVALID_NOTE as _ws_invalid,
                        feedback_text as _ws_feedback,
                        merge_sources as _ws_merge,
                        parse_web_search_request as _parse_ws,
                        perform_search as _ws_perform,
                        search_model as _ws_model,
                        sse_events as _ws_events,
                        web_result_log_fields as _ws_log_fields,
                    )
                    _ws_parsed = _parse_ws(body if isinstance(body, str) else "")
                    if not _ws_parsed:
                        context_result = _ws_invalid
                    elif openai_client is None or _ws_budget is None:
                        context_result = "Web search is not available."
                    else:
                        _ws_query = _ws_parsed["query"]
                        _ws_status, _ws_note = _ws_budget.check(_ws_query)
                        if _ws_status != "ok":
                            context_result = _ws_note or "Web search was not run."
                            dlog("grok_ask_plan_web_search_skipped", session_id=session_id,
                                 user_id=user_id, tool_round=tool_round, status=_ws_status,
                                 query=_ws_query[:200])
                        else:
                            dlog("grok_ask_plan_web_search_requested", session_id=session_id,
                                 user_id=user_id, tool_round=tool_round, query=_ws_query[:200])
                            dlog("web_search_emitted", session_id=session_id, user_id=user_id,
                                 provider="grok", mode="ask_plan", tool_round=tool_round)
                            for _ws_ev in _ws_events(_ws_query, {"sources": [], "error": None})[:2]:
                                yield _sse(_ws_ev)
                            try:
                                _ws_result = await asyncio.to_thread(
                                    _ws_perform, openai_client, _ws_query, _ws_budget)
                            except Exception as _ws_err:
                                _ws_result = {
                                    "text": "", "queries": [], "sources": [],
                                    "error": str(_ws_err)[:200],
                                }
                                _ws_budget.record(False, _ws_query)
                                dlog("grok_ask_plan_web_search_error", session_id=session_id,
                                     user_id=user_id, error=str(_ws_err)[:200])
                            for _ws_ev in _ws_events(_ws_query, _ws_result)[2:]:
                                yield _sse(_ws_ev)
                            if isinstance(web_search_sources, list):
                                web_search_sources[:] = _ws_merge(
                                    web_search_sources, _ws_result.get("sources") or [])
                            context_result = _ws_feedback(_ws_query, _ws_result)
                            dlog("grok_ask_plan_web_search_done", session_id=session_id,
                                 user_id=user_id, tool_round=tool_round,
                                 source_count=len(_ws_result.get("sources") or []),
                                 error=(_ws_result.get("error") or "")[:200],
                                 **_ws_log_fields(_ws_result, _ws_model()))
                elif tag_kind == "mcp":
                    from services.mcp_client import resolve_mcp_call
                    _mcp_done = {"note": "", "counted": False}
                    async for _mcp_ev in resolve_mcp_call(
                        body, user_id=user_id, client_inbox=client_inbox,
                        calls_so_far=_mcp_calls, dlog=dlog, session_id=session_id or "",
                    ):
                        if _mcp_ev.get("type") == "mcp_done":
                            _mcp_done = _mcp_ev
                        else:
                            _o = _tracker.observe(_mcp_ev)
                            if _o:
                                yield _sse(_o)
                            yield _sse(_mcp_ev)
                    if _mcp_done.get("counted"):
                        _mcp_calls += 1
                    context_result = _mcp_done.get("note") or ""
                elif tag_kind == "git":
                    from services.git_cli import resolve_git_call
                    _git_done = {"note": "", "counted": False, "run": False}
                    async for _git_ev in resolve_git_call(
                        str(body or "").strip(), mode, _git_calls, client_inbox,
                        user_id=user_id, dlog=dlog, session_id=session_id or "",
                    ):
                        if _git_ev.get("type") == "git_done":
                            _git_done = _git_ev
                        else:
                            _o = _tracker.observe(_git_ev)
                            if _o:
                                yield _sse(_o)
                            yield _sse(_git_ev)
                    if _git_done.get("counted"):
                        _git_calls += 1
                    context_result = _git_done.get("note") or ""
                    if _git_done.get("stop"):
                        if _tool_open:
                            _end = _tracker.end(context_result)
                            if _end:
                                yield _sse(_end)
                            _tool_open = None
                        from services.git_cli import render_git_outcome as _render_git_outcome
                        _git_block = _render_git_outcome([_git_done])
                        if _git_block:
                            yield _sse({"type": "token", "content": _emit_prose("\n\n" + _git_block)})
                        _end_reason = "git_stop"
                        break
                elif tag_kind == "aws":
                    from services.aws_cli import resolve_aws_call, split_intent
                    _aws_intent, _aws_cmd = split_intent(str(body))
                    _aws_done = {"note": "", "counted": False, "run": False}
                    async for _aws_ev in resolve_aws_call(
                        _aws_cmd, _aws_intent, mode, _aws_calls, client_inbox,
                        user_id=user_id, dlog=dlog, session_id=session_id or "",
                    ):
                        if _aws_ev.get("type") == "aws_done":
                            _aws_done = _aws_ev
                        else:
                            _o = _tracker.observe(_aws_ev)
                            if _o:
                                yield _sse(_o)
                            yield _sse(_aws_ev)
                    if _aws_done.get("counted"):
                        _aws_calls += 1
                    context_result = _aws_done.get("note") or ""
                elif tag_kind == "local":
                    from services.local_exec import resolve_local_call
                    _local_root = ""
                    try:
                        from services.repo_tools import session_repo as _session_repo
                        _local_repo = _session_repo(session_id)
                        _local_root = (_local_repo or {}).get("root_path") or ""
                    except Exception:
                        _local_root = ""
                    _local_done = {"note": "", "counted": False, "run": False}
                    async for _local_ev in resolve_local_call(
                        str(body or ""), mode, _local_calls, client_inbox,
                        user_id=user_id, dlog=dlog, session_id=session_id or "",
                        root=_local_root,
                    ):
                        if _local_ev.get("type") == "local_done":
                            _local_done = _local_ev
                        else:
                            _o = _tracker.observe(_local_ev)
                            if _o:
                                yield _sse(_o)
                            yield _sse(_local_ev)
                    if _local_done.get("counted"):
                        _local_calls += 1
                    context_result = _local_done.get("note") or ""
                else:
                    yield _sse({"type": "progress", "content": "🔍 Looking at the code…"})
                    context_result = await asyncio.to_thread(
                        _dispatch_context_request,
                        tag_kind, body, execute_tool_round_fn=execute_tool_round_fn,
                        symbol_maps_by_name=symbol_maps_by_name,
                        file_content_lookup=file_content_lookup,
                        user_id=user_id, session_id=session_id, tool_round=tool_round,
                    )
                if _tool_open:
                    _end = _tracker.end(context_result or "")
                    if _end:
                        yield _sse(_end)
                dlog("grok_ask_plan_native_context_dispatched", session_id=session_id,
                     user_id=user_id, tool_round=tool_round, tag_kind=tag_kind,
                     result_chars=len(context_result))

            followup = build_native_followup_messages(
                round_text, calls, translated.results_by_id,
                context_call_id=translated.context_call_id,
                context_result=context_result, dlog=dlog,
                session_id=session_id, user_id=user_id,
            )
            messages.extend(followup)
            tool_round += 1
    finally:
        dlog("grok_ask_plan_native_end", session_id=session_id, user_id=user_id,
             model=chat_model, mode=mode, tool_round=tool_round, reason=_end_reason)
    for _ev in _tracker.close_all():
        yield _sse(_ev)
    if steer_inbox is not None:
        yield _sse({"type": "steer_closed"})
        from services.steering import flush_unapplied as _flush_steer
        for _uev in _flush_steer(steer_inbox):
            yield _sse(_uev)
