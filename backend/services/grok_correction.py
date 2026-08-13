"""
Grok-4.5 fallback for the Claude-only correction/retry loops inside the
natural-edit pipeline (pipeline.run_natural_pipeline_stream).

WHY THIS MODULE EXISTS
-----------------------
Several correction/retry helpers in pipeline.py call the Anthropic Claude API
directly (``_safe_claude_call`` / ``_retry_truncated_edit`` /
``_retry_truncated_newfile`` / ``_execute_single_edit``) and are tagged
"R25: corrections always Claude". Inside ``run_natural_pipeline_stream``, when
the architect model is NOT Claude and the user has NOT configured an Anthropic
key, ``aclient`` is explicitly set to ``None``
(``services/pipeline.py:15396-15410``, dlog ``natural_gpt_no_anthropic_key``).
Every one of those Claude-only call sites then silently no-ops.

``services/gpt_correction.py`` already closed that gap for GPT. This module is
the exact same gap closed for Grok — as a SEPARATE, NEW file. It deliberately
does NOT import from, subclass, or otherwise touch ``gpt_correction.py``: that
file is already-working, already-QA'd code and must stay byte-for-byte
untouched, so the small amount of shared shape (the Anthropic-``Message``
adapter and the tag-extraction logic) is duplicated here instead of factored
out. Duplication is the intentional, instructed trade-off.

Active ONLY when the architect model is a Grok model AND ``aclient is None``
(no Anthropic key at all) — verified per-call-site in pipeline.py. When an
Anthropic key exists, the pre-existing Claude correction path runs exactly as
before; when the model is GPT, the pre-existing ``gpt_correction.py`` path runs
exactly as before. This module is only ever reached on a third, previously
dead branch.

Model: caller-supplied Grok id (``grok-4.5`` or ``grok-4.6`` — confirmed at
docs.x.ai/developers/models). Default fallback is
``services.grok_provider.GROK_DEFAULT_MODEL`` (``grok-4.5``). Also listed in
``routers/settings.py`` and matched by ``services/grok_provider.py:_is_grok_model``.

DESIGN RULES (mirrors services/gpt_correction.py / services/gpt_reasoning.py):
  - Dependency injection only: ``chat_create``, ``dlog``, ``get_setting`` are
    passed in by the caller (pipeline.py). This module imports nothing from
    pipeline.py — zero circular-import risk.
  - Reuses pipeline.py's already-hardened ``_chat_create()`` (passed in as
    ``chat_create``) for the actual API call, so retry/timeout behaviour is not
    reimplemented twice.
  - xAI-specific request shaping (empty-content-with-tool_calls, disallowed
    penalty/stop params) is applied via ``services/grok_provider.py`` before
    every call, and 429s are classified rate-limit-vs-billing-cap on the error
    path, so the documented xAI incompatibilities are handled here rather than
    leaking into the shared pipeline code.
  - Every function degrades gracefully: any internal error is logged and the
    caller receives ``None`` (or an empty ``_GrokMessage``), matching the
    contract of the Claude-only function it stands in for. Nothing raises.
  - Response shape: ``_GrokMessage`` / ``_GrokTextBlock`` mimic just enough of
    an Anthropic ``Message`` (``.content`` list of blocks with ``.text``,
    ``.stop_reason``, ``.usage.output_tokens``) that call sites written against
    the Claude response shape work completely unmodified.

FEATURE FLAG (settings table):
  grok_correction_fallback   default "true" — kill switch. When "false", every
  function here returns None / an empty message immediately (identical to the
  pre-existing silent-skip behaviour) without calling any API.

LOGGING
-------
Every function and every branch logs via ``_dlog``. ``_dlog`` prefers the
caller-injected ``dlog`` (pipeline.py's own ``_dlog``) and otherwise falls back
to ``database._dlog`` (the ``from database import _dlog`` convention used by
``backend/routers/settings.py``), so no code path here can ever be unlogged.
"""

import asyncio
import json as _json
import time

try:  # pragma: no cover - import guard only
    from database import _dlog as _db_dlog
except Exception:  # pragma: no cover
    _db_dlog = None

from services.grok_provider import (
    GROK_DEFAULT_MODEL,
    classify_429,
    grok_429_user_message,
    sanitize_outgoing_messages,
    strip_unsupported_params,
)


def _dlog(event: str, dlog=None, **kwargs):
    """Structured debug log used by every code path in this module.

    Prefers the caller-injected logger (pipeline.py's ``_dlog``) so Grok
    correction events land in the same run stream as everything else; falls
    back to ``database._dlog`` when called standalone. Never raises.
    """
    try:
        if dlog is not None:
            dlog(event, **kwargs)
            return
        if _db_dlog is not None:
            _db_dlog(event, **kwargs)
    except Exception:
        pass


# ── Flag helper ──────────────────────────────────────────────────────────────

def grok_correction_fallback_enabled(get_setting, dlog=None) -> bool:
    """Kill switch for the whole module. Default ON.

    Never raises — any lookup failure is treated as "keep the fallback on",
    since the only alternative (this module unused) is the pre-existing
    silent-skip behaviour, which is strictly worse for a Grok-only user.
    """
    try:
        on = str(get_setting("grok_correction_fallback", "true")).strip().lower() != "false"
        _dlog("grok_correction_flag_checked", dlog=dlog, enabled=on)
        return on
    except Exception as e:
        _dlog("grok_correction_flag_error", dlog=dlog,
              error_type=type(e).__name__, error=str(e)[:200], defaulted_to=True)
        return True


# ── Minimal Anthropic-Message-shaped adapter ─────────────────────────────────

class _GrokTextBlock:
    """Mimics an Anthropic TextBlock: `.type == "text"`, `.text == <str>`."""
    __slots__ = ("type", "text")

    def __init__(self, text: str):
        self.type = "text"
        self.text = text or ""


class _GrokUsage:
    __slots__ = ("output_tokens",)

    def __init__(self, output_tokens):
        self.output_tokens = output_tokens


class _GrokMessage:
    """Mimics just enough of an Anthropic `Message` for existing call sites
    (`resp.content`, `getattr(resp, "stop_reason", None)`,
    `getattr(getattr(resp, "usage", None), "output_tokens", None)`)."""
    __slots__ = ("content", "stop_reason", "usage")

    def __init__(self, text: str, stop_reason: str = "end_turn", output_tokens=None):
        self.content = [_GrokTextBlock(text)]
        self.stop_reason = stop_reason
        self.usage = _GrokUsage(output_tokens)


def _xai_messages(system: str, messages: list, dlog=None,
                  session_id: str = "", user_id: str = "") -> list:
    """Convert Anthropic-shaped (system= kwarg + user/assistant turns) into
    xAI/OpenAI chat-completions-shaped messages (system as the first message),
    then apply xAI's stricter message-content rule via grok_provider."""
    out = [{"role": "system", "content": system or ""}]
    for m in messages or []:
        conv = {"role": m.get("role", "user"), "content": m.get("content", "")}
        # Carry tool-call plumbing through untouched so grok_provider's
        # xAI-strictness fixer (content must never be null alongside
        # tool_calls) can see and repair it. Dropping these keys here would
        # silently break any tool-calling correction turn.
        for k in ("tool_calls", "tool_call_id", "name"):
            if isinstance(m, dict) and m.get(k) is not None:
                conv[k] = m[k]
        out.append(conv)
    shaped = sanitize_outgoing_messages(out, dlog=dlog,
                                        session_id=session_id, user_id=user_id)
    _dlog("grok_correction_messages_built", dlog=dlog,
          session_id=session_id, user_id=user_id, message_count=len(shaped))
    return shaped


def _extract_xai_text(response, dlog=None) -> str:
    """Safely extract text from an xAI ChatCompletion response.
    Returns '' on any unexpected shape (never raises)."""
    try:
        choice = response.choices[0]
        return choice.message.content or ""
    except Exception as e:
        _dlog("grok_correction_text_extract_failed", dlog=dlog,
              error_type=type(e).__name__, error=str(e)[:200])
        return ""


def _extract_xai_finish_reason(response, dlog=None) -> str:
    try:
        return response.choices[0].finish_reason or "stop"
    except Exception as e:
        _dlog("grok_correction_finish_reason_extract_failed", dlog=dlog,
              error_type=type(e).__name__, error=str(e)[:200])
        return "stop"


def _extract_xai_output_tokens(response, dlog=None):
    try:
        return getattr(response.usage, "completion_tokens", None)
    except Exception as e:
        _dlog("grok_correction_usage_extract_failed", dlog=dlog,
              error_type=type(e).__name__, error=str(e)[:200])
        return None


def _log_call_exception(e, dlog=None, session_id: str = "", user_id: str = "",
                        model: str = "", phase: str = ""):
    """Log an API exception, classifying 429s as retryable rate-limit vs. a
    hard billing/spend-cap failure (they share the same status code but have
    opposite remediation). Never raises."""
    text = str(e)
    is_429 = "429" in text or "rate limit" in text.lower() or "rate_limit" in text.lower()
    if is_429:
        kind = classify_429(text, dlog=dlog, session_id=session_id, user_id=user_id)
        _dlog("grok_correction_call_429", dlog=dlog,
              session_id=session_id, user_id=user_id, model=model, phase=phase,
              kind=kind, retryable=(kind != "billing"),
              user_message=grok_429_user_message(kind, dlog=dlog))
        return
    _dlog("grok_correction_call_exception", dlog=dlog,
          session_id=session_id, user_id=user_id, model=model, phase=phase,
          error_type=type(e).__name__, error=text[:300])


def _call_kwargs(dlog=None, session_id: str = "", user_id: str = "") -> dict:
    """Extra kwargs for a Grok chat-completions call, with xAI-disallowed
    params stripped. Grok 4.5 is reasoning-only and rejects
    presence_penalty/frequency_penalty/stop outright, so nothing that could
    trip that rule is ever sent from here."""
    kwargs = strip_unsupported_params({}, dlog=dlog,
                                      session_id=session_id, user_id=user_id)
    _dlog("grok_correction_call_kwargs_built", dlog=dlog,
          session_id=session_id, user_id=user_id, keys=sorted(kwargs.keys()))
    return kwargs


# ── Core call primitive ──────────────────────────────────────────────────────

async def safe_grok_correction_call(
    client,
    *,
    model: str = GROK_DEFAULT_MODEL,
    system: str,
    messages: list,
    chat_create,
    dlog=None,
    get_setting=None,
    session_id: str = "",
    user_id: str = "",
    timeout_s: float = 120.0,
    **_ignored_claude_kwargs,
):
    """Grok analogue of `_safe_claude_call`, for the correction-loop call sites
    that pass `system=` + `messages=` and consume the result as an Anthropic
    `Message` (`resp.content` blocks with `.text`).

    `chat_create` MUST be pipeline.py's `_chat_create`, passed in by the caller
    (dependency injection — see module docstring).

    `_ignored_claude_kwargs` absorbs Claude-only kwargs (`desired_text_tokens`,
    `thinking_budget`, `retry_on_starve`) so call sites can pass the same
    keyword set to the Claude, GPT or Grok path without branching on argument
    names.

    Returns a `_GrokMessage` (never raises — any failure surfaces as an empty
    `_GrokMessage`, matching `_safe_claude_call`'s always-message contract).
    """
    if get_setting is not None and not grok_correction_fallback_enabled(get_setting, dlog=dlog):
        _dlog("grok_correction_call_disabled_by_flag", dlog=dlog,
              session_id=session_id, user_id=user_id, model=model)
        return _GrokMessage("", stop_reason="skipped")

    xai_messages = _xai_messages(system, messages, dlog=dlog,
                                 session_id=session_id, user_id=user_id)
    extra = _call_kwargs(dlog=dlog, session_id=session_id, user_id=user_id)
    _dlog("grok_correction_call_entry", dlog=dlog,
          session_id=session_id, user_id=user_id, model=model,
          message_count=len(xai_messages))

    def _call():
        return chat_create(client, model=model, messages=xai_messages, **extra)

    _t0 = time.time()
    try:
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout_s)
    except asyncio.TimeoutError:
        _dlog("grok_correction_call_timeout", dlog=dlog,
              session_id=session_id, user_id=user_id, model=model,
              timeout_s=timeout_s, elapsed_s=round(time.time() - _t0, 1))
        return _GrokMessage("", stop_reason="timeout")
    except Exception as e:
        _log_call_exception(e, dlog=dlog, session_id=session_id, user_id=user_id,
                            model=model, phase="safe_grok_correction_call")
        return _GrokMessage("", stop_reason="error")

    text = _extract_xai_text(response, dlog=dlog)
    finish_reason = _extract_xai_finish_reason(response, dlog=dlog)
    output_tokens = _extract_xai_output_tokens(response, dlog=dlog)
    _dlog("grok_correction_call_ok", dlog=dlog,
          session_id=session_id, user_id=user_id, model=model,
          elapsed_s=round(time.time() - _t0, 1),
          text_len=len(text), finish_reason=finish_reason,
          output_tokens=output_tokens)

    if not text:
        _dlog("grok_correction_call_empty_text", dlog=dlog,
              session_id=session_id, user_id=user_id, model=model,
              finish_reason=finish_reason)

    return _GrokMessage(text, stop_reason=finish_reason, output_tokens=output_tokens)


# ── Focused single-edit / single-file retries ────────────────────────────────
# Grok analogues of pipeline.py's _retry_truncated_edit /
# _retry_truncated_newfile / _execute_single_edit. Those three functions are
# untouched; these mirror their prompt shape and tag-extraction logic, swapping
# the Anthropic streaming call for a non-streaming Grok call via `chat_create`.

async def retry_truncated_edit_grok(
    client,
    model: str,
    filename: str,
    symbol_name: str,
    file_content: str,
    smap,
    user_request: str,
    chat_create,
    dlog=None,
    get_setting=None,
    session_id: str = "",
    user_id: str = "",
):
    """Grok analogue of pipeline.py's `_retry_truncated_edit`. Same contract:
    returns the raw `<surgical_edit>` JSON body, or None on failure."""
    if get_setting is not None and not grok_correction_fallback_enabled(get_setting, dlog=dlog):
        _dlog("grok_retry_truncated_edit_disabled_by_flag", dlog=dlog,
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name)
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

    _dlog("grok_retry_truncated_edit_entry", dlog=dlog,
          session_id=session_id, user_id=user_id,
          filename=filename, symbol=symbol_name, model=model)

    msgs = _xai_messages(focused_system, [{"role": "user", "content": focused_user}],
                         dlog=dlog, session_id=session_id, user_id=user_id)
    extra = _call_kwargs(dlog=dlog, session_id=session_id, user_id=user_id)

    def _call():
        return chat_create(client, model=model, messages=msgs, **extra)

    _t0 = time.time()
    try:
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=120)
    except (asyncio.TimeoutError, TimeoutError):
        _dlog("grok_retry_truncated_edit_timeout", dlog=dlog,
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name,
              duration_s=round(time.time() - _t0, 1))
        return None
    except Exception as e:
        _log_call_exception(e, dlog=dlog, session_id=session_id, user_id=user_id,
                            model=model, phase="retry_truncated_edit_grok")
        _dlog("grok_retry_truncated_edit_error", dlog=dlog,
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name, error=str(e)[:300])
        return None

    text = _extract_xai_text(response, dlog=dlog).strip()
    _dlog("grok_retry_truncated_edit_response", dlog=dlog,
          session_id=session_id, user_id=user_id,
          filename=filename, symbol=symbol_name,
          duration_s=round(time.time() - _t0, 1), text_len=len(text),
          finish_reason=_extract_xai_finish_reason(response, dlog=dlog))

    EDIT_OPEN, EDIT_CLOSE = "<surgical_edit>", "</surgical_edit>"
    start, end = text.find(EDIT_OPEN), text.find(EDIT_CLOSE)
    if start != -1 and end != -1:
        raw = text[start + len(EDIT_OPEN):end].strip()
        try:
            _json.loads(raw)  # validate parseable, mirrors Claude-path behavior
        except Exception as e:
            _dlog("grok_retry_truncated_edit_unparseable", dlog=dlog,
                  session_id=session_id, user_id=user_id,
                  filename=filename, symbol=symbol_name, error=str(e)[:200])
            return None
        _dlog("grok_retry_truncated_edit_success", dlog=dlog,
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name, raw_len=len(raw))
        return raw

    _dlog("grok_retry_truncated_edit_no_block", dlog=dlog,
          session_id=session_id, user_id=user_id,
          filename=filename, symbol=symbol_name, response_preview=text[:200])
    return None


async def retry_truncated_newfile_grok(
    client,
    model: str,
    filename: str,
    user_request: str,
    chat_create,
    dlog=None,
    get_setting=None,
    session_id: str = "",
    user_id: str = "",
):
    """Grok analogue of pipeline.py's `_retry_truncated_newfile`. Same
    contract: returns the raw `<new_file>` JSON body, or None on failure."""
    if get_setting is not None and not grok_correction_fallback_enabled(get_setting, dlog=dlog):
        _dlog("grok_retry_truncated_newfile_disabled_by_flag", dlog=dlog,
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

    _dlog("grok_retry_truncated_newfile_entry", dlog=dlog,
          session_id=session_id, user_id=user_id, filename=filename, model=model)

    msgs = _xai_messages(focused_system, [{"role": "user", "content": focused_user}],
                         dlog=dlog, session_id=session_id, user_id=user_id)
    extra = _call_kwargs(dlog=dlog, session_id=session_id, user_id=user_id)

    def _call():
        return chat_create(client, model=model, messages=msgs, **extra)

    _t0 = time.time()
    try:
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=120)
    except (asyncio.TimeoutError, TimeoutError):
        _dlog("grok_retry_truncated_newfile_timeout", dlog=dlog,
              session_id=session_id, user_id=user_id, filename=filename,
              duration_s=round(time.time() - _t0, 1))
        return None
    except Exception as e:
        _log_call_exception(e, dlog=dlog, session_id=session_id, user_id=user_id,
                            model=model, phase="retry_truncated_newfile_grok")
        _dlog("grok_retry_truncated_newfile_error", dlog=dlog,
              session_id=session_id, user_id=user_id, filename=filename,
              error=str(e)[:300])
        return None

    text = _extract_xai_text(response, dlog=dlog).strip()
    _dlog("grok_retry_truncated_newfile_response", dlog=dlog,
          session_id=session_id, user_id=user_id, filename=filename,
          duration_s=round(time.time() - _t0, 1), text_len=len(text),
          finish_reason=_extract_xai_finish_reason(response, dlog=dlog))

    FILE_OPEN, FILE_CLOSE = "<new_file>", "</new_file>"
    start, end = text.find(FILE_OPEN), text.find(FILE_CLOSE)
    if start != -1 and end != -1:
        raw = text[start + len(FILE_OPEN):end].strip()
        try:
            _json.loads(raw)
        except Exception as e:
            _dlog("grok_retry_truncated_newfile_unparseable", dlog=dlog,
                  session_id=session_id, user_id=user_id, filename=filename,
                  error=str(e)[:200])
            return None
        _dlog("grok_retry_truncated_newfile_success", dlog=dlog,
              session_id=session_id, user_id=user_id, filename=filename,
              raw_len=len(raw))
        return raw

    _dlog("grok_retry_truncated_newfile_no_block", dlog=dlog,
          session_id=session_id, user_id=user_id, filename=filename,
          response_preview=text[:200])
    return None


async def execute_single_edit_grok(
    client,
    model: str,
    filename: str,
    symbol_name: str,
    change_description: str,
    file_content: str,
    symbol_map,
    user_request: str,
    chat_create,
    dlog=None,
    get_setting=None,
    session_id: str = "",
    user_id: str = "",
    max_wait_s: float = 120.0,
    large_file_window: int = 400,
):
    """Grok analogue of pipeline.py's `_execute_single_edit` (Plan→Execute
    orchestration inside run_natural_pipeline_stream). Same contract: returns
    the raw `<surgical_edit>` JSON body, or None on failure."""
    if get_setting is not None and not grok_correction_fallback_enabled(get_setting, dlog=dlog):
        _dlog("grok_execute_single_edit_disabled_by_flag", dlog=dlog,
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name)
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
            _dlog("grok_execute_single_edit_windowed", dlog=dlog,
                  session_id=session_id, user_id=user_id,
                  filename=filename, symbol=symbol_name,
                  total_lines=file_line_count, window_start=ws + 1, window_end=we,
                  symbol_start=target_sym.start_line, symbol_end=target_sym.end_line)
        else:
            file_display = file_content
            _dlog("grok_execute_single_edit_no_window", dlog=dlog,
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

    _dlog("grok_execute_single_edit_entry", dlog=dlog,
          session_id=session_id, user_id=user_id,
          filename=filename, symbol=symbol_name, model=model, max_wait_s=max_wait_s)

    msgs = _xai_messages(focused_system, [{"role": "user", "content": focused_user}],
                         dlog=dlog, session_id=session_id, user_id=user_id)
    extra = _call_kwargs(dlog=dlog, session_id=session_id, user_id=user_id)

    def _call():
        return chat_create(client, model=model, messages=msgs, **extra)

    single_edit_timeout = max(5.0, float(max_wait_s))
    _t0 = time.time()
    try:
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=single_edit_timeout)
    except (asyncio.TimeoutError, TimeoutError):
        _dlog("grok_execute_single_edit_timeout", dlog=dlog,
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name, timeout_sec=single_edit_timeout)
        return None
    except Exception as e:
        _log_call_exception(e, dlog=dlog, session_id=session_id, user_id=user_id,
                            model=model, phase="execute_single_edit_grok")
        _dlog("grok_execute_single_edit_error", dlog=dlog,
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name, error=str(e)[:300])
        return None

    text = _extract_xai_text(response, dlog=dlog)
    _dlog("grok_execute_single_edit_response", dlog=dlog,
          session_id=session_id, user_id=user_id,
          filename=filename, symbol=symbol_name,
          duration_s=round(time.time() - _t0, 1), text_len=len(text),
          finish_reason=_extract_xai_finish_reason(response, dlog=dlog))

    EDIT_OPEN, EDIT_CLOSE = "<surgical_edit>", "</surgical_edit>"
    start, end = text.find(EDIT_OPEN), text.find(EDIT_CLOSE)
    if start != -1 and end != -1:
        raw = text[start + len(EDIT_OPEN):end].strip()
        # No json.loads validation here — mirrors _execute_single_edit's own
        # comment: model output for JSX/CSS often has unescaped quotes that
        # break strict JSON; the downstream edit-parse chain has its own
        # fallbacks (including regex extraction) for exactly this case.
        _dlog("grok_execute_single_edit_success", dlog=dlog,
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name, raw_len=len(raw))
        return raw

    _dlog("grok_execute_single_edit_no_block", dlog=dlog,
          session_id=session_id, user_id=user_id,
          filename=filename, symbol=symbol_name, response_preview=text[:300])
    return None
