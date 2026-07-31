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
