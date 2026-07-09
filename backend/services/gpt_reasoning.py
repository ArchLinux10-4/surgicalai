"""
GPT-5.x reasoning-model hardening + Responses API adapter for SurgicalAI.

WHY THIS MODULE EXISTS (verified against official OpenAI docs, 2026-07-03):
  - Reasoning tokens are billed as output tokens and count against
    max_completion_tokens. OpenAI: "reserve at least 25,000 tokens for
    reasoning and outputs" and truncation "might occur before any visible
    output tokens are produced." The pipeline previously used 16,384.
  - finish_reason == "length" was never checked anywhere, so truncated or
    empty output was silently parsed downstream.
  - OpenAI: "Reasoning models work better with the Responses API"
    (3% SWE-bench improvement, same prompt).

DESIGN RULES:
  - Dependency injection only: get_setting / dlog are passed in as arguments.
    This module imports nothing from pipeline.py — zero circular-import risk.
  - Claude models NEVER reach this module: pipeline only calls it inside the
    NO_TEMPERATURE_MODELS branch of _chat_create (GPT-5.x / o-series only).
  - Every function degrades gracefully: any internal error is logged and the
    caller falls back to the exact pre-existing behavior.

FEATURE FLAGS (settings table):
  gpt5_hardening    default "true"  — Phase 1: budget / effort / truncation retry
  gpt_responses_api default "false" — Phase 3: route non-stream calls to Responses API
"""

# ── Constants (values sourced from official OpenAI documentation) ────────────
HARDENED_MAX_COMPLETION_TOKENS = 32768  # docs: reserve >= 25,000 for reasoning+output
RETRY_MAX_COMPLETION_TOKENS = 65536     # single retry budget after truncation
DEFAULT_REASONING_EFFORT = "low"        # docs (reasoning best practices): low effort
                                        # for execution-oriented coding tasks; also
                                        # maximizes visible-output share of the budget
_VALID_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


# ── Flag helpers ──────────────────────────────────────────────────────────────

def hardening_enabled(get_setting) -> bool:
    """Phase 1 flag. Default ON — GPT-5.x was non-functional without it."""
    try:
        return str(get_setting("gpt5_hardening", "true")).strip().lower() != "false"
    except Exception:
        return True


def responses_api_enabled(get_setting) -> bool:
    """Phase 3 flag. Default OFF."""
    try:
        return str(get_setting("gpt_responses_api", "false")).strip().lower() == "true"
    except Exception:
        return False


# ── Phase 1: kwargs hardening ────────────────────────────────────────────────

def apply_hardening_kwargs(base_model: str, kwargs: dict, effort_models,
                           get_setting, dlog) -> bool:
    """Mutate kwargs in place with hardened defaults for a reasoning model.
    Returns True if hardening was applied, False if the flag is off (caller
    must then apply legacy defaults). Caller-supplied kwargs always win."""
    if not hardening_enabled(get_setting):
        dlog("gpt_hardening_disabled", base_model=base_model)
        return False

    # Budget: OpenAI docs say reserve >= 25k for reasoning + output.
    if "max_completion_tokens" not in kwargs:
        kwargs["max_completion_tokens"] = HARDENED_MAX_COMPLETION_TOKENS
        dlog("gpt_hardening_budget", base_model=base_model,
             max_completion_tokens=HARDENED_MAX_COMPLETION_TOKENS)

    # Effort: explicit per-call value > settings override > hardened default.
    # Only for models that accept the parameter (REASONING_EFFORT_MODELS).
    if base_model in effort_models and "reasoning_effort" not in kwargs:
        _setting = ""
        try:
            _setting = str(get_setting("reasoning_effort", "") or "").strip().lower()
        except Exception:
            _setting = ""
        if _setting in _VALID_EFFORTS:
            kwargs["reasoning_effort"] = _setting
            dlog("gpt_hardening_effort", base_model=base_model,
                 reasoning_effort=_setting, source="settings")
        else:
            kwargs["reasoning_effort"] = DEFAULT_REASONING_EFFORT
            dlog("gpt_hardening_effort", base_model=base_model,
                 reasoning_effort=DEFAULT_REASONING_EFFORT, source="default")
    return True


# ── Phase 1: truncation detection + single retry ─────────────────────────────

def is_truncated(resp, dlog, model: str = "") -> bool:
    """True if the completion was cut off by the token budget, or the model
    produced no visible content at all (budget consumed by reasoning)."""
    try:
        choice = resp.choices[0]
    except Exception:
        return False
    finish = getattr(choice, "finish_reason", None)
    if finish == "length":
        dlog("gpt_truncation_detected", model=model, finish_reason="length")
        return True
    msg = getattr(choice, "message", None)
    content = getattr(msg, "content", None) if msg is not None else None
    tool_calls = getattr(msg, "tool_calls", None) if msg is not None else None
    if not (content or "").strip() and not tool_calls:
        dlog("gpt_truncation_detected", model=model,
             finish_reason=str(finish), reason="empty_content_no_tool_calls")
        return True
    return False


def mark_truncated(resp, dlog, model: str = ""):
    """Attach _sai_truncated=True to the response so downstream parsers can
    refuse partial output. object.__setattr__ bypasses pydantic restrictions;
    if even that fails, we log and return the response unmarked (degrades to
    pre-existing behavior — never raises)."""
    try:
        object.__setattr__(resp, "_sai_truncated", True)
    except Exception as err:
        dlog("gpt_mark_truncated_failed", model=model, error=str(err)[:200])
    return resp


def create_with_truncation_retry(create_fn, kwargs: dict, dlog, model: str = ""):
    """Run create_fn(kwargs); if the result is truncated, retry ONCE with a
    doubled budget. If still truncated, mark the response so parsers refuse it.
    Never called for stream=True (streams cannot be inspected here)."""
    resp = create_fn(kwargs)
    if not is_truncated(resp, dlog, model=model):
        return resp

    retry_kwargs = dict(kwargs)
    retry_kwargs["max_completion_tokens"] = max(
        RETRY_MAX_COMPLETION_TOKENS,
        int(retry_kwargs.get("max_completion_tokens") or 0),
    )
    dlog("gpt_truncation_retry", model=model,
         retry_max_completion_tokens=retry_kwargs["max_completion_tokens"])
    try:
        retry_resp = create_fn(retry_kwargs)
    except Exception as err:
        dlog("gpt_truncation_retry_error", model=model, error=str(err)[:300])
        return mark_truncated(resp, dlog, model=model)

    if is_truncated(retry_resp, dlog, model=model):
        dlog("gpt_truncation_retry_still_truncated", model=model)
        return mark_truncated(retry_resp, dlog, model=model)
    dlog("gpt_truncation_retry_success", model=model)
    return retry_resp


# ── Phase 3: Responses API adapter ───────────────────────────────────────────
# Shims mimic the exact chat-completions attributes the pipeline reads:
#   resp.choices[0].message.content
#   resp.choices[0].message.tool_calls[i].id/.function.name/.function.arguments
#   resp.choices[0].finish_reason

class _ShimFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _ShimToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.type = "function"
        self.function = _ShimFunction(name, arguments)


class _ShimMessage:
    def __init__(self, content, tool_calls):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls or None


class _ShimChoice:
    def __init__(self, message, finish_reason):
        self.index = 0
        self.message = message
        self.finish_reason = finish_reason


class _ShimResponse:
    def __init__(self, choice, response_id):
        self.id = response_id
        self.choices = [choice]
        self.usage = None
        self._sai_truncated = False
        self._sai_via_responses_api = True


def _convert_tools_to_responses(tools, dlog):
    """Chat Completions tools are externally tagged ({"type":"function",
    "function":{...}}); Responses tools are internally tagged (flattened).
    Per docs, Responses attempts strict mode when strict is omitted — we set
    strict=False explicitly to preserve Chat Completions (non-strict) behavior."""
    converted = []
    for t in tools or []:
        try:
            fn = t.get("function", {}) if isinstance(t, dict) else {}
            converted.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
                "strict": False,
            })
        except Exception as err:
            dlog("gpt_responses_tool_convert_error", error=str(err)[:200])
            return None
    return converted


def _build_responses_kwargs(model: str, messages: list, kwargs: dict, dlog):
    """Translate chat-completions kwargs → responses kwargs (per migration guide).
    Returns None if any argument cannot be mapped safely (caller falls back)."""
    rk = {"model": model, "input": messages, "store": False}

    if "max_completion_tokens" in kwargs:
        rk["max_output_tokens"] = kwargs["max_completion_tokens"]
    if "reasoning_effort" in kwargs:
        rk["reasoning"] = {"effort": kwargs["reasoning_effort"]}

    rf = kwargs.get("response_format")
    if rf is not None:
        rf_type = rf.get("type") if isinstance(rf, dict) else None
        if rf_type == "json_object":
            rk["text"] = {"format": {"type": "json_object"}}
        else:
            # Unrecognized response_format — don't guess, fall back.
            dlog("gpt_responses_unmapped_response_format", format=str(rf)[:200])
            return None

    if "tools" in kwargs:
        converted = _convert_tools_to_responses(kwargs["tools"], dlog)
        if converted is None:
            return None
        rk["tools"] = converted
    if "tool_choice" in kwargs:
        tc = kwargs["tool_choice"]
        if tc in ("auto", "required", "none"):
            rk["tool_choice"] = tc
        else:
            dlog("gpt_responses_unmapped_tool_choice", tool_choice=str(tc)[:100])
            return None

    # Any kwargs we don't explicitly map → fall back rather than guess.
    _handled = {"max_completion_tokens", "reasoning_effort", "response_format",
                "tools", "tool_choice", "stream"}
    _unmapped = [k for k in kwargs if k not in _handled]
    if _unmapped:
        dlog("gpt_responses_unmapped_kwargs", unmapped=_unmapped)
        return None
    return rk


def _adapt_response(resp, dlog, model: str = ""):
    """Convert a Responses API result into a chat-completions-shaped shim."""
    text_parts = []
    tool_calls = []
    for item in getattr(resp, "output", None) or []:
        itype = getattr(item, "type", "")
        if itype == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", "") == "output_text":
                    text_parts.append(getattr(part, "text", "") or "")
        elif itype == "function_call":
            tool_calls.append(_ShimToolCall(
                getattr(item, "call_id", "") or getattr(item, "id", ""),
                getattr(item, "name", ""),
                getattr(item, "arguments", "") or "{}",
            ))
        # "reasoning" and other item types are intentionally ignored.

    status = getattr(resp, "status", "") or ""
    incomplete_reason = ""
    if status == "incomplete":
        details = getattr(resp, "incomplete_details", None)
        incomplete_reason = getattr(details, "reason", "") or "" if details else ""

    if status == "incomplete" and incomplete_reason == "max_output_tokens":
        finish_reason = "length"
    elif tool_calls:
        finish_reason = "tool_calls"
    else:
        finish_reason = "stop"

    content = "".join(text_parts)
    shim = _ShimResponse(
        _ShimChoice(_ShimMessage(content, tool_calls), finish_reason),
        getattr(resp, "id", ""),
    )
    dlog("gpt_responses_adapted", model=model, status=status,
         incomplete_reason=incomplete_reason, finish_reason=finish_reason,
         content_len=len(content), tool_call_count=len(tool_calls))
    return shim


def responses_create(client, model: str, messages: list, kwargs: dict,
                     retry_fn, dlog):
    """Phase 3 entry point. Calls client.responses.create and returns a
    chat-completions-shaped shim, with the same single truncation retry as
    Phase 1. Returns None on ANY problem — caller falls back to Chat
    Completions. Never raises."""
    try:
        rk = _build_responses_kwargs(model, messages, kwargs, dlog)
        if rk is None:
            return None

        def _call(k):
            return _adapt_response(retry_fn(lambda: client.responses.create(**k)),
                                   dlog, model=model)

        shim = _call(rk)
        if shim.choices[0].finish_reason == "length" or is_truncated(shim, dlog, model=model):
            retry_rk = dict(rk)
            retry_rk["max_output_tokens"] = max(
                RETRY_MAX_COMPLETION_TOKENS,
                int(retry_rk.get("max_output_tokens") or 0),
            )
            dlog("gpt_responses_truncation_retry", model=model,
                 retry_max_output_tokens=retry_rk["max_output_tokens"])
            shim2 = _call(retry_rk)
            if shim2.choices[0].finish_reason == "length" or is_truncated(shim2, dlog, model=model):
                dlog("gpt_responses_retry_still_truncated", model=model)
                return mark_truncated(shim2, dlog, model=model)
            return shim2
        return shim
    except Exception as err:
        dlog("gpt_responses_adapter_error", model=model,
             error_type=type(err).__name__, error=str(err)[:400])
        return None
