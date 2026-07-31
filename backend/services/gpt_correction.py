"""
GPT-based fallback for the Claude-only correction/retry loops inside the
natural-edit pipeline (pipeline.run_natural_pipeline_stream).

WHY THIS MODULE EXISTS
-----------------------
Several correction/retry helpers in pipeline.py call the Anthropic Claude API
directly (``aclient.messages.stream`` / ``_safe_claude_call``) and are tagged
"R25: corrections always Claude". Inside run_natural_pipeline_stream, when the
architect model is GPT and the user has NOT configured an Anthropic key,
``aclient`` is explicitly set to ``None`` (pipeline.py ~15255-15266, comment:
"GPT mode: try to create aclient for corrections... if no Anthropic key,
corrections degrade gracefully"). Every one of those Claude-only call sites
then either raises inside its own try/except (returning None — "correction
skipped") or is never entered. Net effect for a GPT-only user with zero
Anthropic key: the truncation-retry, symbol-reference-correction, QA-retry,
and multi-window-correction loops all silently do nothing.

This module gives those specific call sites a GPT equivalent, active ONLY
when ``aclient is None`` (no Anthropic key at all — verified per-call-site in
pipeline.py, never when a Claude path is available). It is purely additive:
none of the existing Claude-only functions (_safe_claude_call,
_retry_truncated_edit, _retry_truncated_newfile, _execute_single_edit,
run_qa_agent, run_qa_for_changes) are modified by this file or by the call
sites that use it.

Model: "gpt-5.6-terra" — already a registered model id in pipeline.py's
NO_TEMPERATURE_MODELS / REASONING_EFFORT_MODELS sets and in
routers/settings.py + routers/images.py.

DESIGN RULES (mirrors services/gpt_reasoning.py exactly, for the same reason):
  - Dependency injection only: `chat_create`, `dlog`, `get_setting` are all
    passed in by the caller (pipeline.py). This module imports nothing from
    pipeline.py — zero circular-import risk.
  - Reuses pipeline.py's existing, already-hardened `_chat_create()` (passed
    in as `chat_create`) for the actual API call, so GPT-5.x reasoning-model
    truncation/starvation protection (services/gpt_reasoning.py:
    reasoning_effort injection, max_completion_tokens sizing,
    finish_reason=length truncation-retry) is reused as-is rather than
    reimplemented — one hardening implementation, not two divergent ones.
  - Every function degrades gracefully: any internal error is logged via
    `dlog` and the caller receives None (same contract as the Claude-only
    functions it stands in for), never a raised exception.
  - Response shape: `_GPTMessage` / `_GPTTextBlock` mimic just enough of an
    Anthropic `Message` (`.content` list of blocks with `.text`,
    `.stop_reason`, `.usage.output_tokens`) so call sites written against the
    Claude response shape (`for block in resp.content if hasattr(block,
    "text")`) work completely unmodified when a GPT-path result is used in
    place of a Claude-path result.

FEATURE FLAG (settings table):
  gpt_correction_fallback   default "true" — kill switch. When "false", every
  function in this module returns None immediately (identical to the current
  no-Anthropic-key degrade behavior) without calling any API.
"""

import asyncio
import time


# ── Flag helper ──────────────────────────────────────────────────────────────

def correction_fallback_enabled(get_setting) -> bool:
    """Kill switch for the whole module. Default ON.

    Never raises — any lookup failure is treated as "keep the fallback on"
    since the only alternative (this module unused) is the pre-existing
    silent-skip behavior, which is strictly worse for a GPT-only user.
    """
    try:
        return str(get_setting("gpt_correction_fallback", "true")).strip().lower() != "false"
    except Exception:
        return True


# ── Minimal Anthropic-Message-shaped adapter ─────────────────────────────────

class _GPTTextBlock:
    """Mimics an Anthropic TextBlock: `.type == "text"`, `.text == <str>`."""
    __slots__ = ("type", "text")

    def __init__(self, text: str):
        self.type = "text"
        self.text = text or ""


class _GPTUsage:
    __slots__ = ("output_tokens",)

    def __init__(self, output_tokens):
        self.output_tokens = output_tokens


class _GPTMessage:
    """Mimics just enough of an Anthropic `Message` for existing call sites
    (`resp.content`, `getattr(resp, "stop_reason", None)`,
    `getattr(getattr(resp, "usage", None), "output_tokens", None)`)."""
    __slots__ = ("content", "stop_reason", "usage")

    def __init__(self, text: str, stop_reason: str = "end_turn", output_tokens=None):
        self.content = [_GPTTextBlock(text)]
        self.stop_reason = stop_reason
        self.usage = _GPTUsage(output_tokens)


def _oai_messages(system: str, messages: list) -> list:
    """Convert Anthropic-shaped (system= kwarg + user/assistant turns) into
    OpenAI chat-completions-shaped messages (system as the first message)."""
    out = [{"role": "system", "content": system}]
    for m in messages or []:
        out.append({"role": m.get("role", "user"), "content": m.get("content", "")})
    return out


def _extract_oai_text(response) -> str:
    """Safely extract text from an OpenAI ChatCompletion response.
    Returns '' on any unexpected shape (never raises)."""
    try:
        choice = response.choices[0]
        return choice.message.content or ""
    except Exception:
        return ""


def _extract_oai_finish_reason(response) -> str:
    try:
        return response.choices[0].finish_reason or "stop"
    except Exception:
        return "stop"


def _extract_oai_output_tokens(response):
    try:
        return getattr(response.usage, "completion_tokens", None)
    except Exception:
        return None


# ── Core call primitive ──────────────────────────────────────────────────────

async def safe_gpt_correction_call(
    client,
    *,
    model: str,
    system: str,
    messages: list,
    chat_create,
    dlog,
    get_setting=None,
    session_id: str = "",
    user_id: str = "",
    timeout_s: float = 120.0,
    **_ignored_claude_kwargs,
):
    """GPT analogue of `_safe_claude_call`, for the 3 correction-loop call
    sites that pass `system=` + `messages=` and consume the result as an
    Anthropic `Message` (`resp.content` blocks with `.text`).

    `chat_create` MUST be pipeline.py's `_chat_create` function, passed in by
    the caller (dependency injection — see module docstring). This reuses
    that function's existing GPT-5.x reasoning-model hardening (reasoning
    effort, max_completion_tokens sizing, truncation retry) unmodified.

    `_ignored_claude_kwargs` absorbs Claude-only kwargs (e.g.
    `desired_text_tokens`, `thinking_budget`, `retry_on_starve`) so call
    sites can pass the exact same keyword set to either the Claude or the
    GPT path without branching on argument names — accepted but unused here
    since `chat_create`'s own hardening already sizes the output budget.

    Returns a `_GPTMessage` (never raises — any failure is logged and
    surfaces as a `_GPTMessage("")`, matching `_safe_claude_call`'s contract
    of always returning a message-shaped object, never None).
    """
    if get_setting is not None and not correction_fallback_enabled(get_setting):
        dlog("gpt_correction_call_disabled_by_flag",
             session_id=session_id, user_id=user_id, model=model)
        return _GPTMessage("", stop_reason="skipped")

    oai_messages = _oai_messages(system, messages)
    dlog("gpt_correction_call_entry",
         session_id=session_id, user_id=user_id, model=model,
         message_count=len(oai_messages))

    def _call():
        return chat_create(client, model=model, messages=oai_messages)

    _t0 = time.time()
    try:
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout_s)
    except asyncio.TimeoutError:
        dlog("gpt_correction_call_timeout",
             session_id=session_id, user_id=user_id, model=model,
             timeout_s=timeout_s, elapsed_s=round(time.time() - _t0, 1))
        return _GPTMessage("", stop_reason="timeout")
    except Exception as e:
        dlog("gpt_correction_call_error",
             session_id=session_id, user_id=user_id, model=model,
             error_type=type(e).__name__, error=str(e)[:300],
             elapsed_s=round(time.time() - _t0, 1))
        return _GPTMessage("", stop_reason="error")

    text = _extract_oai_text(response)
    finish_reason = _extract_oai_finish_reason(response)
    output_tokens = _extract_oai_output_tokens(response)
    dlog("gpt_correction_call_ok",
         session_id=session_id, user_id=user_id, model=model,
         elapsed_s=round(time.time() - _t0, 1),
         text_len=len(text), finish_reason=finish_reason,
         output_tokens=output_tokens)

    if not text:
        # No text at all (e.g. GPT hardening's own truncation-retry already
        # exhausted its budget upstream inside chat_create). Log distinctly
        # so this is provable from logs, mirroring
        # "thinking_starvation_final_fail" for the Claude path.
        dlog("gpt_correction_call_empty_text",
             session_id=session_id, user_id=user_id, model=model,
             finish_reason=finish_reason)

    return _GPTMessage(text, stop_reason=finish_reason, output_tokens=output_tokens)


# ── Focused single-edit / single-file retries ────────────────────────────────
# GPT analogues of pipeline.py's _retry_truncated_edit / _retry_truncated_newfile
# / _execute_single_edit. Those three functions are untouched; these mirror
# their prompt shape and tag-extraction logic exactly, swapping the Anthropic
# streaming call for a non-streaming GPT call via `chat_create` (non-streaming
# is required to get chat_create's/gpt_reasoning.py's truncation-retry, which
# only applies to non-stream calls).

async def retry_truncated_edit_gpt(
    client,
    model: str,
    filename: str,
    symbol_name: str,
    file_content: str,
    smap,
    user_request: str,
    chat_create,
    dlog,
    get_setting=None,
    session_id: str = "",
    user_id: str = "",
) -> str | None:
    """GPT analogue of pipeline.py's `_retry_truncated_edit`. Same contract:
    returns the raw `<surgical_edit>` JSON body, or None on failure."""
    if get_setting is not None and not correction_fallback_enabled(get_setting):
        dlog("gpt_retry_truncated_edit_disabled_by_flag",
             session_id=session_id, user_id=user_id, filename=filename, symbol=symbol_name)
        return None

    sym_index = ""
    if smap:
        lines = []
        for sym in smap.symbols:
            lines.append(f"  {sym.symbol_type.value}: {sym.name} (L{sym.start_line}-L{sym.end_line})")
        sym_index = "SYMBOL INDEX:\n" + "\n".join(lines) + "\n\n"

    focused_system = (
        "You are SurgicalAI. Write EXACTLY ONE <surgical_edit> block for the "
        f"symbol '{symbol_name}' in '{filename}'.\n\n"
        "For JSX/TSX/HTML: before writing the edit, verify your tag balance — "
        "count opening vs closing tags at each nesting level and confirm they match. "
        "Then emit the edit block.\n\n"
        "Format:\n"
        "<surgical_edit>\n"
        '{"filename": "...", "symbol": "...", "description": "...", "new_code": "..."}\n'
        "</surgical_edit>\n\n"
        "For large symbols, use a targeted edit with old_code + new_code instead of "
        "rewriting the entire symbol."
    )
    focused_user = (
        f"User request: {user_request}\n\n"
        f"\u2501\u2501\u2501 FILE: {filename} \u2501\u2501\u2501\n"
        f"{sym_index}"
        f"{file_content}"
    )

    dlog("gpt_retry_truncated_edit_entry",
         session_id=session_id, user_id=user_id,
         filename=filename, symbol=symbol_name, model=model)

    def _call():
        return chat_create(
            client, model=model,
            messages=[
                {"role": "system", "content": focused_system},
                {"role": "user", "content": focused_user},
            ],
        )

    _t0 = time.time()
    try:
        async with asyncio.timeout(120):
            response = await asyncio.to_thread(_call)
    except TimeoutError:
        dlog("gpt_retry_truncated_edit_timeout",
             session_id=session_id, user_id=user_id,
             filename=filename, symbol=symbol_name,
             duration_s=round(time.time() - _t0, 1))
        return None
    except Exception as e:
        dlog("gpt_retry_truncated_edit_error",
             session_id=session_id, user_id=user_id,
             filename=filename, symbol=symbol_name,
             error=str(e)[:300])
        return None

    text = _extract_oai_text(response).strip()
    dlog("gpt_retry_truncated_edit_response",
         session_id=session_id, user_id=user_id,
         filename=filename, symbol=symbol_name,
         duration_s=round(time.time() - _t0, 1), text_len=len(text),
         finish_reason=_extract_oai_finish_reason(response))

    EDIT_OPEN, EDIT_CLOSE = "<surgical_edit>", "</surgical_edit>"
    start, end = text.find(EDIT_OPEN), text.find(EDIT_CLOSE)
    if start != -1 and end != -1:
        raw = text[start + len(EDIT_OPEN):end].strip()
        import json as _json
        try:
            _json.loads(raw)  # validate parseable, mirrors Claude-path behavior
        except Exception as e:
            dlog("gpt_retry_truncated_edit_unparseable",
                 session_id=session_id, user_id=user_id,
                 filename=filename, symbol=symbol_name, error=str(e)[:200])
            return None
        dlog("gpt_retry_truncated_edit_success",
             session_id=session_id, user_id=user_id,
             filename=filename, symbol=symbol_name, raw_len=len(raw))
        return raw

    dlog("gpt_retry_truncated_edit_no_block",
         session_id=session_id, user_id=user_id,
         filename=filename, symbol=symbol_name, response_preview=text[:200])
    return None


async def retry_truncated_newfile_gpt(
    client,
    model: str,
    filename: str,
    user_request: str,
    chat_create,
    dlog,
    get_setting=None,
    session_id: str = "",
    user_id: str = "",
) -> str | None:
    """GPT analogue of pipeline.py's `_retry_truncated_newfile`. Same
    contract: returns the raw `<new_file>` JSON body, or None on failure."""
    if get_setting is not None and not correction_fallback_enabled(get_setting):
        dlog("gpt_retry_truncated_newfile_disabled_by_flag",
             session_id=session_id, user_id=user_id, filename=filename)
        return None

    focused_system = (
        "You are SurgicalAI. Write EXACTLY ONE <new_file> block for the "
        f"file '{filename}'.\n\n"
        "Format:\n"
        "<new_file>\n"
        '{"filename": "...", "language": "...", "summary": "...", "content": "..."}\n'
        "</new_file>\n\n"
        "Write production-ready code — no stubs, no TODOs, no placeholders. "
        "Include all necessary imports. The file should be immediately usable."
    )
    focused_user = f"User request: {user_request}\n\nWrite the complete file '{filename}' now, in full."

    dlog("gpt_retry_truncated_newfile_entry",
         session_id=session_id, user_id=user_id, filename=filename, model=model)

    def _call():
        return chat_create(
            client, model=model,
            messages=[
                {"role": "system", "content": focused_system},
                {"role": "user", "content": focused_user},
            ],
        )

    _t0 = time.time()
    try:
        async with asyncio.timeout(120):
            response = await asyncio.to_thread(_call)
    except TimeoutError:
        dlog("gpt_retry_truncated_newfile_timeout",
             session_id=session_id, user_id=user_id, filename=filename,
             duration_s=round(time.time() - _t0, 1))
        return None
    except Exception as e:
        dlog("gpt_retry_truncated_newfile_error",
             session_id=session_id, user_id=user_id, filename=filename,
             error=str(e)[:300])
        return None

    text = _extract_oai_text(response).strip()
    dlog("gpt_retry_truncated_newfile_response",
         session_id=session_id, user_id=user_id, filename=filename,
         duration_s=round(time.time() - _t0, 1), text_len=len(text),
         finish_reason=_extract_oai_finish_reason(response))

    FILE_OPEN, FILE_CLOSE = "<new_file>", "</new_file>"
    start, end = text.find(FILE_OPEN), text.find(FILE_CLOSE)
    if start != -1 and end != -1:
        raw = text[start + len(FILE_OPEN):end].strip()
        import json as _json
        try:
            _json.loads(raw)
        except Exception as e:
            dlog("gpt_retry_truncated_newfile_unparseable",
                 session_id=session_id, user_id=user_id, filename=filename,
                 error=str(e)[:200])
            return None
        dlog("gpt_retry_truncated_newfile_success",
             session_id=session_id, user_id=user_id, filename=filename, raw_len=len(raw))
        return raw

    dlog("gpt_retry_truncated_newfile_no_block",
         session_id=session_id, user_id=user_id, filename=filename,
         response_preview=text[:200])
    return None


def gpt_multi_turn_surgeon_enabled(get_setting) -> bool:
    """Kill switch for the GPT multi-turn Surgeon verification loop. Default ON.

    Separate flag from `gpt_correction_fallback` (module-wide) so this
    specific, newer code path can be disabled independently without
    affecting the other GPT correction fallbacks in this module. When
    disabled (or on any lookup error being treated conservatively — here we
    default to enabled, matching this module's existing convention), the
    caller (pipeline.py run_surgeon) falls through to its pre-existing,
    already-working single-turn GPT tool_use branch — never raises, never
    blocks execution.
    """
    try:
        return str(get_setting("gpt_multi_turn_surgeon", "true")).strip().lower() != "false"
    except Exception:
        return True


def run_gpt_multi_turn_surgeon(
    client,
    model: str,
    user_msg: str,
    symbol_code: str,
    forbid_noop: bool,
    chat_create,
    dlog,
    surgeon_tool_use_system: str,
    surgeon_tools_openai: list,
    session_id: str = "",
    user_id: str = "",
    max_turns: int = 5,
) -> dict:
    """GPT analogue of pipeline.py's Claude multi-turn Surgeon verification
    loop (the `_is_claude_model(surg_model) and _use_multi_turn` branch
    inside `run_surgeon`, Anthropic Messages API `tool_use`/`tool_result`
    blocks). Mirrors its turn-by-turn `edit_code` / `replace_symbol` /
    `no_change_needed` handling, in-memory working-copy tracking,
    verification-context feedback, and failed-edit hint logic line-for-line
    — swapping the Anthropic Messages API for OpenAI chat.completions
    function-calling (`tools=` / `message.tool_calls` / role="tool" reply
    messages).

    The OpenAI multi-turn conversation shape used here (assistant message
    with `tool_calls: [{"id", "type": "function", "function": {"name",
    "arguments"}}]`, follow-up `{"role": "tool", "tool_call_id", "content"}`
    messages) is NOT novel — it is the exact shape already used and proven
    correct by pipeline.py's live Agent Mode GPT correction loop
    (`agent_mode_gpt_correction_*` dlog events, ~pipeline.py:6700-6880).
    This function reuses that same proven shape for a different call site;
    it does not invent new wire-format assumptions.

    Sync (matches `run_surgeon`, which is itself a sync function — unlike
    the async helpers elsewhere in this module).

    `chat_create` MUST be pipeline.py's `_chat_create` (dependency
    injection — see module docstring), so GPT-5.x reasoning-model hardening
    (reasoning_effort, max_completion_tokens sizing, truncation retry) is
    reused as-is.

    Returns a dict, never raises:
      {
        "early_return": None | (code, confidence, notes, [], []),
        "operations": [{"find": ..., "replace": ...}, ...],
        "confidence": int | None,   # None => caller keeps its existing value
        "notes": [str, ...],
      }
    Caller contract (mirrors the Claude multi-turn branch exactly): if
    `early_return` is not None, return it immediately from `run_surgeon`.
    Otherwise assign `operations` / `confidence` (if not None) / extend
    `surgeon_notes`, then continue into the shared post-processing
    (`_rescue_trailing_context`, `apply_operations`, etc.) exactly as the
    Claude multi-turn branch does — that downstream code is model-agnostic
    and untouched.
    """
    import json as _json

    _mt_messages = [
        {"role": "system", "content": surgeon_tool_use_system},
        {"role": "user", "content": user_msg},
    ]
    _mt_working_code = symbol_code
    operations: list = []
    surgeon_notes: list = []
    _mt_total_edits = 0
    _mt_failed_edits = 0
    _mt_turn = 0

    dlog("surgeon_multi_turn_gpt_start",
         session_id=session_id, user_id=user_id, model=model,
         symbol_len=len(symbol_code or ""), max_turns=max_turns,
         forbid_noop=forbid_noop)
    print(f"[SURGEON][MULTI_TURN][GPT] Start — model={model}, max_turns={max_turns}")

    for _mt_turn in range(max_turns):
        _mt_api_start = time.time()
        try:
            _mt_resp = chat_create(
                client, model=model,
                messages=_mt_messages,
                tools=surgeon_tools_openai,
                tool_choice="auto",  # allow natural stop (finish_reason="stop"),
                                     # mirrors Claude's end_turn vs tool_use distinction
            )
        except Exception as _mt_api_err:
            dlog("surgeon_multi_turn_gpt_api_error",
                 turn=_mt_turn + 1, error=str(_mt_api_err)[:500],
                 model=model, user_id=user_id, ops_so_far=len(operations))
            print(f"[SURGEON][MULTI_TURN][GPT] API error on turn {_mt_turn + 1}: {_mt_api_err}")
            if operations:
                surgeon_notes.append(
                    f"Multi-turn (GPT): API error on turn {_mt_turn + 1}, "
                    f"returning {len(operations)} ops collected so far")
                break
            return {
                "early_return": (symbol_code, 0,
                                  [f"Surgeon: multi-turn API error — {str(_mt_api_err)[:100]}"], [], []),
                "operations": [], "confidence": None, "notes": [],
            }

        _mt_api_elapsed = time.time() - _mt_api_start

        # Phase-1-style truncation guard, mirrors the single-turn GPT
        # tool_use path's `_sai_truncated` check: a max_completion_tokens
        # cut can leave partial/absent tool-call JSON. Refuse to build on
        # a truncated turn rather than risk parsing a half-written edit.
        if getattr(_mt_resp, "_sai_truncated", False):
            dlog("surgeon_multi_turn_gpt_truncated",
                 turn=_mt_turn + 1, model=model, user_id=user_id,
                 ops_so_far=len(operations))
            print(f"[SURGEON][MULTI_TURN][GPT] Turn {_mt_turn + 1} truncated after retry — stopping")
            if operations:
                surgeon_notes.append(
                    f"Multi-turn (GPT): output truncated on turn {_mt_turn + 1}, "
                    f"returning {len(operations)} ops collected so far")
            else:
                # No ops collected before truncation hit — refuse rather than
                # silently letting a zero-op result read as "nothing to change".
                # Mirrors the single-turn GPT tool_use path's truncation refusal.
                return {
                    "early_return": (symbol_code, 0,
                                      ["Surgeon: output truncated — retry"], [], []),
                    "operations": [], "confidence": None, "notes": [],
                }
            break

        _mt_choice = _mt_resp.choices[0]
        _mt_message = _mt_choice.message
        _mt_finish = getattr(_mt_choice, "finish_reason", None) or "stop"
        _mt_tool_calls = _mt_message.tool_calls or []

        print(f"[SURGEON][MULTI_TURN][GPT] Turn {_mt_turn + 1}/{max_turns}: "
              f"finish_reason={_mt_finish}, tool_calls={len(_mt_tool_calls)}, "
              f"latency={_mt_api_elapsed:.1f}s, ops_so_far={len(operations)}")

        _mt_tool_results = []
        _mt_noop_early_return = None

        for _tc in _mt_tool_calls:
            try:
                _tc_args = _json.loads(_tc.function.arguments) if _tc.function.arguments else {}
            except Exception as _jpe:
                dlog("surgeon_multi_turn_gpt_arg_parse_error",
                     turn=_mt_turn + 1, tool=_tc.function.name,
                     error=str(_jpe)[:200], user_id=user_id)
                print(f"[SURGEON][MULTI_TURN][GPT]   Failed to parse tool args for "
                      f"{_tc.function.name}: {_jpe}")
                _mt_tool_results.append({
                    "role": "tool", "tool_call_id": _tc.id,
                    "content": _json.dumps({"success": False, "error": "Malformed tool arguments JSON"}),
                })
                continue

            _tc_name = _tc.function.name

            if _tc_name == "edit_code":
                _old = _tc_args.get("old_code", "")
                _new = _tc_args.get("new_code", "")
                _mt_total_edits += 1

                if _old and _old in _mt_working_code:
                    _mt_working_code = _mt_working_code.replace(_old, _new, 1)
                    operations.append({"find": _old, "replace": _new})

                    _edit_pos = _mt_working_code.find(_new) if _new else 0
                    _ctx_lines = _mt_working_code.splitlines()
                    _edit_line = _mt_working_code[:max(0, _edit_pos)].count("\n") + 1
                    _ctx_start = max(0, _edit_line - 4)
                    _ctx_end = min(len(_ctx_lines), _edit_line + _new.count("\n") + 4)
                    _ctx_snippet = "\n".join(
                        f"{_ctx_start + j + 1}: {l}"
                        for j, l in enumerate(_ctx_lines[_ctx_start:_ctx_end])
                    )

                    _mt_tool_results.append({
                        "role": "tool", "tool_call_id": _tc.id,
                        "content": _json.dumps({
                            "success": True,
                            "message": f"Edit applied successfully at line {_edit_line}",
                            "lines_changed": _new.count("\n") + 1,
                            "context": _ctx_snippet,
                        }),
                    })
                    dlog("surgeon_multi_turn_gpt_edit_ok",
                         turn=_mt_turn + 1, edit_num=_mt_total_edits,
                         edit_line=_edit_line, old_len=len(_old), new_len=len(_new),
                         working_code_len=len(_mt_working_code), user_id=user_id)
                    print(f"[SURGEON][MULTI_TURN][GPT]   \u2705 edit_code applied at L{_edit_line} "
                          f"(old={len(_old)}\u2192new={len(_new)} chars)")
                else:
                    _mt_failed_edits += 1
                    _hint_lines = []
                    _old_first_line = _old.strip().split("\n")[0] if _old.strip() else ""
                    if _old_first_line:
                        for _hi, _hl in enumerate(_mt_working_code.splitlines(), 1):
                            if _old_first_line.strip() in _hl.strip():
                                _hint_lines.append(f"L{_hi}: {_hl.rstrip()}")
                                if len(_hint_lines) >= 3:
                                    break

                    _mt_tool_results.append({
                        "role": "tool", "tool_call_id": _tc.id,
                        "content": _json.dumps({
                            "success": False,
                            "error": "old_code not found in current file content. "
                                     "The text must match exactly, including indentation and whitespace.",
                            "old_code_preview": _old[:200],
                            "hint": f"Similar lines found: {'; '.join(_hint_lines)}" if _hint_lines else
                                    "No similar lines found. Try requesting the current file content.",
                        }),
                    })
                    dlog("surgeon_multi_turn_gpt_edit_fail",
                         turn=_mt_turn + 1, edit_num=_mt_total_edits,
                         old_code_len=len(_old), old_code_preview=_old[:300],
                         hint_lines=_hint_lines, working_code_len=len(_mt_working_code),
                         user_id=user_id)
                    print(f"[SURGEON][MULTI_TURN][GPT]   \u274c edit_code FAILED — old_code not found "
                          f"(old={len(_old)} chars, hints={len(_hint_lines)})")

            elif _tc_name == "replace_symbol":
                _new = _tc_args.get("new_code", "")
                operations.append({"find": _mt_working_code, "replace": _new})
                _mt_working_code = _new
                _mt_total_edits += 1
                _mt_tool_results.append({
                    "role": "tool", "tool_call_id": _tc.id,
                    "content": _json.dumps({
                        "success": True,
                        "message": f"Symbol fully replaced ({len(_new.splitlines())} lines)",
                        "new_length": len(_new.splitlines()),
                    }),
                })
                dlog("surgeon_multi_turn_gpt_replace_symbol",
                     turn=_mt_turn + 1, new_len=len(_new), user_id=user_id)
                print(f"[SURGEON][MULTI_TURN][GPT]   \u2705 replace_symbol ({len(_new)} chars)")

            elif _tc_name == "no_change_needed":
                _reason = _tc_args.get("reason", "")
                print(f"[SURGEON][MULTI_TURN][GPT]   no_change_needed: {_reason[:200]}")
                if forbid_noop:
                    _mt_tool_results.append({
                        "role": "tool", "tool_call_id": _tc.id,
                        "content": _json.dumps({
                            "success": False,
                            "error": "QA has rejected the current code. Changes ARE required. "
                                     "Re-read the CHANGE PLAN and make the specified modifications.",
                        }),
                    })
                    dlog("surgeon_multi_turn_gpt_noop_rejected",
                         turn=_mt_turn + 1, reason=_reason[:300], user_id=user_id)
                else:
                    dlog("surgeon_multi_turn_gpt_noop_accepted",
                         turn=_mt_turn + 1, reason=_reason[:300], user_id=user_id)
                    _mt_noop_early_return = (
                        symbol_code, 10, [f"Surgeon: already implemented — {_reason[:100]}"], [], [])
                    break

        if _mt_noop_early_return is not None:
            return {"early_return": _mt_noop_early_return, "operations": [], "confidence": None, "notes": []}

        # ── Turn exit conditions (mirrors Claude multi-turn exactly) ──────────
        # 1. Natural stop (no tool calls this turn) → GPT is done.
        if _mt_finish == "stop":
            dlog("surgeon_multi_turn_gpt_end_turn",
                 turn=_mt_turn + 1, total_ops=len(operations),
                 total_edits=_mt_total_edits, failed_edits=_mt_failed_edits,
                 user_id=user_id)
            print(f"[SURGEON][MULTI_TURN][GPT] Natural stop on turn {_mt_turn + 1} — done")
            break

        # 2. No tool results to send back → nothing to continue with.
        if not _mt_tool_results:
            dlog("surgeon_multi_turn_gpt_no_tools",
                 turn=_mt_turn + 1, finish_reason=_mt_finish, user_id=user_id)
            print(f"[SURGEON][MULTI_TURN][GPT] No tool results on turn {_mt_turn + 1} — done")
            break

        # 3. Budget exhausted.
        if _mt_turn >= max_turns - 1:
            dlog("surgeon_multi_turn_gpt_budget_exhausted",
                 turns_used=_mt_turn + 1, total_ops=len(operations),
                 failed_edits=_mt_failed_edits, user_id=user_id)
            print(f"[SURGEON][MULTI_TURN][GPT] Budget exhausted after {_mt_turn + 1} turns")
            break

        # ── Continue conversation — proven OpenAI multi-turn tool-call shape,
        # identical to the live Agent Mode GPT correction loop in pipeline.py
        # (agent_mode_gpt_correction_* call sites, ~pipeline.py:6700-6880). ──
        _mt_assistant_msg = {"role": "assistant", "content": _mt_message.content or None}
        _mt_assistant_msg["tool_calls"] = [
            {"id": _tc.id, "type": "function",
             "function": {"name": _tc.function.name, "arguments": _tc.function.arguments}}
            for _tc in _mt_tool_calls
        ]
        _mt_messages.append(_mt_assistant_msg)
        _mt_messages.extend(_mt_tool_results)

        print(f"[SURGEON][MULTI_TURN][GPT] Continuing \u2192 turn {_mt_turn + 2} "
              f"(msgs={len(_mt_messages)}, ops={len(operations)}, failed={_mt_failed_edits})")

    _mt_final_turns = _mt_turn + 1
    dlog("surgeon_multi_turn_gpt_complete",
         turns_used=_mt_final_turns, max_turns=max_turns,
         total_ops=len(operations), total_edits=_mt_total_edits,
         failed_edits=_mt_failed_edits, working_code_len=len(_mt_working_code),
         original_code_len=len(symbol_code or ""), model=model, user_id=user_id)
    print(f"[SURGEON][MULTI_TURN][GPT] Complete: {len(operations)} ops in "
          f"{_mt_final_turns} turn(s), {_mt_failed_edits} failed edits")

    _confidence = None
    if _mt_failed_edits > 0 and not operations:
        surgeon_notes.append(f"Multi-turn (GPT): {_mt_failed_edits} edit(s) failed, 0 succeeded")
        _confidence = 0
    elif _mt_failed_edits > 0:
        surgeon_notes.append(
            f"Multi-turn (GPT): {_mt_failed_edits} edit(s) failed, {len(operations)} succeeded")

    return {
        "early_return": None,
        "operations": operations,
        "confidence": _confidence,
        "notes": surgeon_notes,
    }


async def execute_single_edit_gpt(
    client,
    model: str,
    filename: str,
    symbol_name: str,
    change_description: str,
    file_content: str,
    symbol_map,
    user_request: str,
    chat_create,
    dlog,
    get_setting=None,
    session_id: str = "",
    user_id: str = "",
    max_wait_s: float = 120.0,
    large_file_window: int = 400,
) -> str | None:
    """GPT analogue of pipeline.py's `_execute_single_edit` (Plan\u2192Execute
    orchestration inside run_natural_pipeline_stream). Same contract: returns
    the raw `<surgical_edit>` JSON body, or None on failure."""
    if get_setting is not None and not correction_fallback_enabled(get_setting):
        dlog("gpt_execute_single_edit_disabled_by_flag",
             session_id=session_id, user_id=user_id, filename=filename, symbol=symbol_name)
        return None

    sym_index = ""
    if symbol_map and hasattr(symbol_map, "symbols") and symbol_map.symbols:
        sym_lines = []
        for s in symbol_map.symbols:
            size = s.end_line - s.start_line + 1
            sym_lines.append(f"  [{s.symbol_type.value}] {s.full_path:<45} L{s.start_line}\u2013{s.end_line}  ({size}L)")
        sym_index = "SYMBOL INDEX:\n" + "\n".join(sym_lines) + "\n\n"

    focused_system = (
        "You are SurgicalAI. Produce exactly ONE <surgical_edit> block for the requested change.\n\n"
        "Rules:\n"
        "- For large files: use edit_start_line/edit_end_line (absolute line numbers shown in context)\n"
        "- For small symbols you can see entirely: include the COMPLETE edited symbol in new_code\n"
        "- For large symbols (>200 lines): PREFER old_code/new_code \u2014 copy only the few lines changing. "
        "Avoid edit_start_line/edit_end_line unless the change is a contiguous block you can fully output without truncation.\n"
        "- Match original indentation exactly\n"
        "- Copy ALL unchanged lines verbatim\n"
        "- The JSON must have: filename, symbol, description, new_code (and optionally old_code, "
        "or edit_start_line + edit_end_line)\n"
        "- Do NOT produce explanatory text outside the <surgical_edit> block\n"
    )

    file_lines = file_content.splitlines()
    file_line_count = len(file_lines)
    if file_line_count > large_file_window and symbol_map and hasattr(symbol_map, "symbols"):
        target_sym = None
        for s in symbol_map.symbols:
            if getattr(s, "full_path", "") == symbol_name or getattr(s, "name", "") == symbol_name:
                target_sym = s
                break
        if target_sym:
            padding = 50
            ws = max(0, target_sym.start_line - 1 - padding)
            we = min(file_line_count, target_sym.end_line + padding)
            window_parts = []
            if ws > 0:
                window_parts.append(f"... [{ws} lines above] ...\n")
            numbered = "\n".join(f"{i+1:5d}: {file_lines[i]}" for i in range(ws, we))
            window_parts.append(numbered)
            if we < file_line_count:
                window_parts.append(f"\n... [{file_line_count - we} lines below] ...")
            file_display = "\n".join(window_parts)
            dlog("gpt_execute_single_edit_windowed",
                 session_id=session_id, user_id=user_id,
                 filename=filename, symbol=symbol_name,
                 total_lines=file_line_count, window_start=ws + 1, window_end=we,
                 symbol_start=target_sym.start_line, symbol_end=target_sym.end_line)
        else:
            file_display = file_content
            dlog("gpt_execute_single_edit_no_window",
                 session_id=session_id, user_id=user_id,
                 filename=filename, symbol=symbol_name,
                 total_lines=file_line_count, reason="symbol_not_found_in_map")
        focused_user = (
            f"Edit the symbol `{symbol_name}` in `{filename}`.\n\n"
            f"Change: {change_description}\n\n"
            f"User's original request: {user_request}\n\n"
            f"{sym_index}"
            f"\u26a0\ufe0f LARGE FILE ({file_line_count} lines) \u2014 showing focused window "
            f"with absolute line numbers.\n"
            f"Use edit_start_line/edit_end_line for precise edits.\n\n"
            f"File content (focused):\n```\n{file_display}\n```\n\n"
            "Produce exactly ONE <surgical_edit> block now."
        )
    else:
        focused_user = (
            f"Edit the symbol `{symbol_name}` in `{filename}`.\n\n"
            f"Change: {change_description}\n\n"
            f"User's original request: {user_request}\n\n"
            f"{sym_index}"
            f"File content:\n```\n{file_content}\n```\n\n"
            "Produce exactly ONE <surgical_edit> block now."
        )

    dlog("gpt_execute_single_edit_entry",
         session_id=session_id, user_id=user_id,
         filename=filename, symbol=symbol_name, model=model, max_wait_s=max_wait_s)

    def _call():
        return chat_create(
            client, model=model,
            messages=[
                {"role": "system", "content": focused_system},
                {"role": "user", "content": focused_user},
            ],
        )

    single_edit_timeout = max(5.0, float(max_wait_s))
    _t0 = time.time()
    try:
        async with asyncio.timeout(single_edit_timeout):
            response = await asyncio.to_thread(_call)
    except TimeoutError:
        dlog("gpt_execute_single_edit_timeout",
             session_id=session_id, user_id=user_id,
             filename=filename, symbol=symbol_name, timeout_sec=single_edit_timeout)
        return None
    except Exception as e:
        dlog("gpt_execute_single_edit_error",
             session_id=session_id, user_id=user_id,
             filename=filename, symbol=symbol_name, error=str(e)[:300])
        return None

    text = _extract_oai_text(response)
    dlog("gpt_execute_single_edit_response",
         session_id=session_id, user_id=user_id,
         filename=filename, symbol=symbol_name,
         duration_s=round(time.time() - _t0, 1), text_len=len(text),
         finish_reason=_extract_oai_finish_reason(response))

    EDIT_OPEN, EDIT_CLOSE = "<surgical_edit>", "</surgical_edit>"
    start, end = text.find(EDIT_OPEN), text.find(EDIT_CLOSE)
    if start != -1 and end != -1:
        raw = text[start + len(EDIT_OPEN):end].strip()
        # No json.loads validation here — mirrors _execute_single_edit's own
        # comment: model output for JSX/CSS often has unescaped quotes that
        # break strict JSON; the downstream edit-parse chain has its own
        # fallbacks (including regex extraction) for exactly this case.
        dlog("gpt_execute_single_edit_success",
             session_id=session_id, user_id=user_id,
             filename=filename, symbol=symbol_name, raw_len=len(raw))
        return raw

    dlog("gpt_execute_single_edit_no_block",
         session_id=session_id, user_id=user_id,
         filename=filename, symbol=symbol_name, response_preview=text[:300])
    return None
