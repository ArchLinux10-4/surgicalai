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
* ``chat_create_fn``        — pipeline.py's ``_chat_create`` (handles retries).
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
import json
import os
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
    "This is a discussion, not an implementation request. Do not write full "
    "new code, diffs, or <surgical_edit>/<new_file> tags in your final "
    "answer — explain, analyze, and cite the relevant code instead. A short "
    "illustrative snippet is fine if the user needs it to follow the "
    "explanation, but you are not implementing anything here. If the user "
    "actually wants the change made, tell them to switch to Edit/Agent mode."
)


def build_grok_ask_plan_reinforcement(mode: str) -> str:
    """Return the Grok-only trailing reinforcement for Ask/Plan mode.

    Placed at the very END of the outgoing user turn (recency effect) as a
    second copy of the "don't write code" constraint already prepended once
    by ``chat.py:_mode_directive`` (primacy effect) — the sandwich pattern.
    Pure string builder, no side effects; safe to unit test directly.
    """
    label = "PLAN" if (mode or "").strip().lower() == "plan" else "ASK"
    return _GROK_ASK_PLAN_REINFORCEMENT_TMPL.format(label=label)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


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
    tool_parts = execute_tool_round_fn(
        sr_match=sr_match, fr_match=fr_match, gr_match=gr_match,
        hr_match=hr_match,
        symbol_maps_by_name=symbol_maps_by_name,
        file_content_lookup=file_content_lookup,
        user_id=user_id, session_id=session_id, tool_round=tool_round,
    )
    return "\n\n".join(tool_parts) if tool_parts else (
        f"No result for this {tag_kind} request."
    )


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
                dlog=dlog, session_id=session_id, user_id=user_id,
            )
            messages[i] = {**m, "content": (m.get("content") or "") + suffix}
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
    while True:
        elapsed = time.time() - t0
        if elapsed >= deadline_s or tool_round >= max_rounds:
            reason = "budget_exhausted_deadline" if elapsed >= deadline_s else "budget_exhausted_rounds"
            dlog("grok_ask_plan_native_budget_exhausted", session_id=session_id,
                 user_id=user_id, tool_round=tool_round, elapsed_s=round(elapsed, 1),
                 reason=reason)
            yield _sse({"type": "token", "content": (
                "I looked at the available code but couldn't fully answer within "
                "my lookup budget — try asking about a more specific file or function."
            )})
            break

        dlog("grok_ask_plan_native_round_start", session_id=session_id,
             user_id=user_id, model=chat_model, mode=mode, tool_round=tool_round)

        if tool_round > 0:
            yield _sse({"type": "progress",
                        "content": f"Continuing… round {tool_round + 1}"})

        stream = chat_create_fn(
            client, model=chat_model, messages=messages,
            temperature=0.0, tools=tools, stream=True,
        )

        round_text = ""
        acc = StreamedToolCallAccumulator(dlog=dlog, session_id=session_id, user_id=user_id)
        round_t0 = time.time()
        last_activity_ts = round_t0
        last_silence_progress_ts = 0.0
        thinking_open = False
        thinking_deltas = 0
        silence_s = int(os.getenv("GROK_SILENCE_PROGRESS_S", "3"))
        for chunk in iter_chunks_fn(stream, model=chat_model, session_id=session_id,
                                    user_id=user_id):
            now = time.time()
            if ((now - last_activity_ts) >= silence_s
                    and (now - last_silence_progress_ts) >= silence_s):
                last_silence_progress_ts = now
                yield _sse({"type": "progress",
                            "content": f"Reasoning… {int(now - round_t0)}s"})
                dlog("grok_agent_silence_heartbeat", session_id=session_id,
                     user_id=user_id, tool_round=tool_round,
                     elapsed_s=int(now - round_t0), mode=mode)

            delta = chunk.choices[0].delta
            rc = extract_reasoning_delta(delta)
            if rc:
                last_activity_ts = time.time()
                thinking_deltas += 1
                if not thinking_open:
                    thinking_open = True
                    yield _sse({"type": "thinking_start", "content": ""})
                yield _sse({"type": "thinking", "content": rc})
                dlog("grok_agent_reasoning_delta", session_id=session_id,
                     user_id=user_id, tool_round=tool_round, chars=len(rc), mode=mode)

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

        if thinking_open:
            yield _sse({"type": "thinking_end", "content": ""})

        if not acc.has_calls():
            # Plain-text answer, no tool calls this round — done.
            final_text = round_text.strip() or (
                "I don't have anything more to add — try rephrasing your question."
            )
            dlog("grok_ask_plan_native_final_answer", session_id=session_id,
                 user_id=user_id, tool_round=tool_round, chars=len(final_text),
                 thinking_deltas=thinking_deltas)
            yield _sse({"type": "token", "content": final_text})
            break

        calls = acc.finalize()
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
            yield _sse({"type": "token", "content": blocked_text})
            break

        context_result = None
        if translated.context_request is not None:
            yield _sse({"type": "progress", "content": "🔍 Looking at the code…"})
            tag_kind, body = translated.context_request
            context_result = _dispatch_context_request(
                tag_kind, body, execute_tool_round_fn=execute_tool_round_fn,
                symbol_maps_by_name=symbol_maps_by_name,
                file_content_lookup=file_content_lookup,
                user_id=user_id, session_id=session_id, tool_round=tool_round,
            )
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
