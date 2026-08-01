"""
xAI Grok provider adapter for SurgicalAI.

WHY THIS MODULE EXISTS
-----------------------
xAI's Chat Completions endpoint (``https://api.x.ai/v1/chat/completions``) is
OpenAI-SDK-compatible, so Grok can reuse the same ``openai.OpenAI`` client the
pipeline already uses — exactly the same base_url-swap trick that
``services/pipeline.py:_get_client_for_model`` already uses for Gemini
(``https://generativelanguage.googleapis.com/v1beta/openai/``). That existing,
working pattern is the precedent this module follows; nothing in the Claude or
GPT/OpenAI code paths is changed.

"Compatible" is NOT "identical", though. xAI's API has several documented and
field-reported behaviours that differ from OpenAI's and that silently (or
loudly) break a naive drop-in reuse. Every helper in this module exists to
mitigate ONE specific, cited difference so the mitigation is testable in
isolation rather than buried inside the 23K-line pipeline:

  1. Assistant messages carrying ``tool_calls`` with null/omitted ``content``
     are rejected by xAI with a hard 400 ("Invalid request content: Each
     message must have at least one content element"). OpenAI tolerates this.
     Ref: https://github.com/langchain-ai/langchain/issues/34140
     -> ``sanitize_outgoing_messages``
  2. ``presence_penalty`` / ``frequency_penalty`` / ``stop`` are *rejected with
     an error* on xAI reasoning models (OpenAI merely ignores them). Grok 4.5
     is reasoning-only, so these must never be sent.
     Ref: https://docs.x.ai/developers/model-capabilities/text/reasoning
     -> ``strip_unsupported_params``
  3. The o-series-only ``reasoning_effort`` injection in pipeline.py is gated on
     exact OpenAI o-series model-id strings, so it cannot misfire for a
     ``grok-*`` id. That is asserted here (rather than assumed) so a future
     edit to that gate cannot silently start injecting o-series kwargs into
     Grok requests.
     -> ``o_series_injection_can_misfire``
  4. Tool-call argument strings can arrive HTML-entity encoded (``&amp;&amp;``,
     ``&quot;``) from some Grok variants, breaking anything that shells out or
     parses JSON.
     Ref: https://github.com/openclaw/openclaw/issues/35173
     -> ``decode_tool_call_arguments``
  5. ``finish_reason`` can be ``"stop"`` (xAI's raw ``"completed"``) even when
     tool calls are present, so an agent loop gated purely on finish_reason
     stops early.
     Ref: https://github.com/vercel/ai/issues/12218
     -> ``should_continue_agent_loop``
  6. Streamed SSE chunks include a full ``usage`` object on *every* chunk, not
     just the final one (OpenAI's default is final-chunk-only, opt-in).
     Ref: https://docs.x.ai/developers/model-capabilities/text/streaming
     -> ``accumulate_stream_usage``
  7. A custom tool literally named ``web_search`` (or ``x_search`` /
     ``code_execution`` / ``code_interpreter``) collides with xAI's own
     server-side built-ins and is silently refused.
     Ref: https://github.com/nicobailon/pi-web-access/issues/138
     -> ``check_tool_name_collisions``
  8. Prompt caching needs a stable per-conversation routing key
     (``prompt_cache_key`` / ``x-grok-conv-id``) or you pay full input price
     on a cache-cold server.
     Ref: https://docs.x.ai/developers/grok-4-5
     -> ``prompt_cache_key_for_session`` / ``prompt_cache_headers``
  9. A 429 can mean "rate limited" OR "out of credits / monthly spend cap
     reached" — same status code, opposite remediation (retry vs. never
     retry).
     Ref: https://github.com/continuedev/continue/issues/10373
     -> ``classify_429``
 10. Malformed / unparseable tool-call argument JSON in general — mitigated
     the same way the already-shipped GPT-path fix does it (commits 22ad61a /
     7c1ac0f): echo the model's own broken snippet back as explicit
     parse-error feedback instead of swallowing it.
     -> ``parse_tool_call_arguments``

MODEL
-----
``grok-4.5`` — confirmed shipping model id (docs.x.ai/developers/models/grok-4.5).
No other Grok model ids are registered here: only ids confirmed by research are
used, nothing invented.

LOGGING
-------
Every function in this module logs through ``_dlog`` on every branch, including
error branches. ``_dlog`` here prefers a caller-injected logger (pipeline.py's
own ``_dlog``, dependency-injected the same way ``services/gpt_correction.py``
injects it) and otherwise falls back to ``database._dlog`` — the same
``from database import ... _dlog`` convention used by
``backend/routers/settings.py:6``. That way no code path can ever be unlogged,
whether it is called from the pipeline or standalone.
"""

import html as _html
import json as _json

try:  # pragma: no cover - import guard only
    from database import _dlog as _db_dlog, get_user_api_key
except Exception:  # pragma: no cover
    _db_dlog = None
    get_user_api_key = None

try:  # pragma: no cover - import guard only
    from crypto_utils import decrypt_api_key
except Exception:  # pragma: no cover
    decrypt_api_key = None


# ── Constants ────────────────────────────────────────────────────────────────

#: xAI OpenAI-compatible base URL (https://docs.x.ai/developers/quickstart).
GROK_BASE_URL = "https://api.x.ai/v1"

#: Confirmed shipping model id — do not add unverified ids here.
GROK_DEFAULT_MODEL = "grok-4.5"

#: Params xAI reasoning models REJECT (error, not ignore). Both snake_case
#: (OpenAI SDK / Chat Completions) and camelCase (docs wording) spellings.
GROK_DISALLOWED_PARAMS = (
    "presence_penalty", "presencePenalty",
    "frequency_penalty", "frequencyPenalty",
    "stop",
)

#: xAI server-side built-in tool names. A custom tool with one of these names
#: is silently refused by the model.
GROK_RESERVED_TOOL_NAMES = ("web_search", "x_search", "code_execution", "code_interpreter")


def _dlog(event: str, dlog=None, **kwargs):
    """Structured debug log used by every code path in this module.

    Prefers the caller-injected ``dlog`` (pipeline.py's ``_dlog``, injected
    exactly as ``services/gpt_correction.py`` does) so Grok events land in the
    same debug stream as the rest of a run; falls back to ``database._dlog``
    (the ``from database import _dlog`` convention used by
    ``backend/routers/settings.py``) when called standalone. Never raises — a
    logging failure must never break a real request.
    """
    try:
        if dlog is not None:
            dlog(event, **kwargs)
            return
        if _db_dlog is not None:
            _db_dlog(event, **kwargs)
    except Exception:
        pass


# ── Model / key resolution ───────────────────────────────────────────────────

def _is_grok_model(model: str) -> bool:
    """Check if a model ID is a Grok/xAI model."""
    return bool(model and model.startswith("grok-"))


def _get_grok_key(user_id: str = "", dlog=None) -> str:
    """Resolve xAI Grok API key for user. Per-user only — no global fallback.

    Mirrors ``services/pipeline.py:_get_gemini_key`` exactly: reads the
    encrypted value out of the existing ``user_api_keys`` table under a new
    ``key_type`` string (``"grok"``) — no schema migration needed, since
    ``key_type`` is already a free-form discriminator column — and decrypts it
    with the same shared ``crypto_utils.decrypt_api_key`` Fernet helper.
    """
    if user_id and get_user_api_key is not None:
        encrypted = get_user_api_key(user_id, "grok")
        if encrypted:
            try:
                key = decrypt_api_key(encrypted)
                _dlog("grok_key_resolved", dlog=dlog, user_id=user_id, key_type="grok")
                return key
            except Exception:
                _dlog("api_key_decrypt_failed", dlog=dlog, user_id=user_id, key_type="grok")
    _dlog("grok_key_missing", dlog=dlog, user_id=user_id, key_type="grok")
    raise ValueError("xAI Grok API key not configured. Go to Settings → API Keys to add it.")


def get_grok_client(user_id: str = "", dlog=None):
    """Return an OpenAI-SDK client pointed at xAI's OpenAI-compatible endpoint.

    This is the function ``pipeline._get_client_for_model`` calls from its new
    ``elif _is_grok_model(model)`` branch. Importing ``OpenAI`` locally keeps
    this module import-safe (and mockable in tests) without touching
    pipeline.py's module-level ``from openai import OpenAI``.
    """
    key = _get_grok_key(user_id, dlog=dlog)
    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover - openai is a hard dependency
        _dlog("grok_client_openai_import_failed", dlog=dlog,
              user_id=user_id, error_type=type(e).__name__, error=str(e)[:300])
        raise
    _dlog("grok_client_created", dlog=dlog, user_id=user_id, base_url=GROK_BASE_URL)
    return OpenAI(api_key=key, base_url=GROK_BASE_URL)


# ── Gotcha 1: assistant tool_calls messages must carry a content element ─────

def sanitize_outgoing_messages(messages, dlog=None, session_id: str = "", user_id: str = ""):
    """Force ``content: ""`` on any assistant message that has ``tool_calls``.

    xAI 400s on ``content: null`` / omitted content alongside ``tool_calls``
    (langchain#34140). Returns a NEW list; the caller's list and every message
    dict that needs no change are left as-is (no in-place mutation of caller
    state). Never raises.
    """
    if not messages:
        _dlog("grok_sanitize_messages_noop", dlog=dlog,
              session_id=session_id, user_id=user_id, reason="empty")
        return messages if messages is not None else []

    out = []
    fixed = 0
    for m in messages:
        try:
            if (isinstance(m, dict)
                    and m.get("role") == "assistant"
                    and m.get("tool_calls")
                    and not m.get("content")):
                patched = dict(m)
                patched["content"] = ""
                out.append(patched)
                fixed += 1
                continue
        except Exception as e:
            _dlog("grok_sanitize_messages_item_error", dlog=dlog,
                  session_id=session_id, user_id=user_id,
                  error_type=type(e).__name__, error=str(e)[:200])
        out.append(m)

    _dlog("grok_sanitize_messages_done", dlog=dlog,
          session_id=session_id, user_id=user_id,
          message_count=len(out), patched_count=fixed)
    return out


# ── Gotcha 2: reasoning models reject penalty/stop params ────────────────────

def strip_unsupported_params(kwargs, dlog=None, session_id: str = "", user_id: str = ""):
    """Remove params xAI reasoning models reject outright.

    ``presence_penalty`` / ``frequency_penalty`` / ``stop`` cause an API error
    on Grok reasoning models (docs.x.ai reasoning page) where OpenAI would
    simply ignore them. Returns a NEW dict — the caller's kwargs dict is not
    mutated. Never raises.
    """
    if not kwargs:
        _dlog("grok_strip_params_noop", dlog=dlog,
              session_id=session_id, user_id=user_id, reason="empty")
        return {} if kwargs is None else dict(kwargs)

    cleaned = {}
    removed = []
    for k, v in kwargs.items():
        if k in GROK_DISALLOWED_PARAMS:
            removed.append(k)
            continue
        cleaned[k] = v

    if removed:
        _dlog("grok_strip_params_removed", dlog=dlog,
              session_id=session_id, user_id=user_id, removed=removed)
    else:
        _dlog("grok_strip_params_clean", dlog=dlog,
              session_id=session_id, user_id=user_id, param_count=len(cleaned))
    return cleaned


# ── Gotcha 3: o-series-only reasoning_effort injection must never fire ───────

def o_series_injection_can_misfire(model: str, reasoning_effort_models, dlog=None) -> bool:
    """True if a Grok model id would be caught by pipeline.py's o-series gate.

    pipeline.py injects ``reasoning_effort`` only when
    ``base_model in REASONING_EFFORT_MODELS`` — an exact-string membership test
    against OpenAI o-series ids. This function proves (rather than assumes)
    that a ``grok-*`` id is not in that set, so the injection cannot misfire.
    Used by the test suite as a regression guard: if someone ever adds a
    Grok-shaped id to that set, the test fails loudly instead of Grok requests
    silently gaining an OpenAI-only kwarg.
    """
    try:
        hit = bool(model) and model in set(reasoning_effort_models or ())
    except Exception as e:
        _dlog("grok_o_series_check_error", dlog=dlog, model=model,
              error_type=type(e).__name__, error=str(e)[:200])
        return False
    if hit:
        _dlog("grok_o_series_gate_would_misfire", dlog=dlog, model=model)
    else:
        _dlog("grok_o_series_gate_safe", dlog=dlog, model=model)
    return hit


# ── Gotcha 4: HTML-entity-encoded tool-call arguments ───────────────────────

def decode_tool_call_arguments(arguments, dlog=None, session_id: str = "", user_id: str = ""):
    """HTML-unescape a raw tool-call ``arguments`` string.

    Some Grok variants return ``&amp;&amp;`` / ``&quot;`` inside tool-call
    argument strings, which breaks anything that shells out or JSON-parses them
    (openclaw#35173). Only unescapes when an entity is actually present, so a
    clean payload is returned byte-identical. Never raises.
    """
    if not isinstance(arguments, str) or not arguments:
        _dlog("grok_tool_args_decode_noop", dlog=dlog,
              session_id=session_id, user_id=user_id,
              type=type(arguments).__name__)
        return arguments

    if "&" not in arguments:
        _dlog("grok_tool_args_decode_clean", dlog=dlog,
              session_id=session_id, user_id=user_id, length=len(arguments))
        return arguments

    try:
        decoded = _html.unescape(arguments)
    except Exception as e:
        _dlog("grok_tool_args_decode_error", dlog=dlog,
              session_id=session_id, user_id=user_id,
              error_type=type(e).__name__, error=str(e)[:200])
        return arguments

    if decoded != arguments:
        _dlog("grok_tool_args_decoded", dlog=dlog,
              session_id=session_id, user_id=user_id,
              before_len=len(arguments), after_len=len(decoded))
    else:
        _dlog("grok_tool_args_decode_clean", dlog=dlog,
              session_id=session_id, user_id=user_id, length=len(arguments))
    return decoded


# ── Gotcha 5: finish_reason is not authoritative for "keep looping" ─────────

def should_continue_agent_loop(finish_reason, tool_calls, dlog=None,
                               session_id: str = "", user_id: str = "") -> bool:
    """Decide whether an agent loop should run another tool round.

    Grok can report ``finish_reason == "stop"`` (raw xAI ``"completed"``) while
    still returning tool calls (vercel/ai#12218). Presence of a non-empty
    ``tool_calls`` collection is therefore treated as authoritative, and the
    finish_reason string is only a secondary signal. Provider-agnostic and
    safe for any OpenAI-shaped response. Never raises.
    """
    try:
        has_calls = bool(tool_calls)
    except Exception as e:
        _dlog("grok_loop_decision_error", dlog=dlog,
              session_id=session_id, user_id=user_id,
              error_type=type(e).__name__, error=str(e)[:200])
        return False

    if has_calls:
        _dlog("grok_loop_continue_tool_calls_present", dlog=dlog,
              session_id=session_id, user_id=user_id,
              finish_reason=finish_reason, tool_call_count=len(tool_calls))
        return True

    if finish_reason in ("tool_calls", "tool-calls", "function_call"):
        # Reported tool-call finish with no parsed calls — keep looping so the
        # caller can re-ask rather than ending the turn on an empty round.
        _dlog("grok_loop_continue_finish_reason_only", dlog=dlog,
              session_id=session_id, user_id=user_id, finish_reason=finish_reason)
        return True

    _dlog("grok_loop_stop", dlog=dlog, session_id=session_id, user_id=user_id,
          finish_reason=finish_reason)
    return False


# ── Gotcha 6: usage arrives on every streamed chunk ────────────────────────

def accumulate_stream_usage(current, chunk_usage, dlog=None,
                            session_id: str = "", user_id: str = ""):
    """Fold a streamed chunk's ``usage`` into the running total by OVERWRITE.

    xAI emits a full (cumulative) ``usage`` object on every SSE chunk, unlike
    OpenAI's final-chunk-only default. Summing them would multiply the token
    counts, so the latest non-empty usage wins. Returns the value to keep.
    Never raises.
    """
    if chunk_usage is None:
        _dlog("grok_stream_usage_chunk_empty", dlog=dlog,
              session_id=session_id, user_id=user_id, kept_previous=current is not None)
        return current
    try:
        _dlog("grok_stream_usage_overwritten", dlog=dlog,
              session_id=session_id, user_id=user_id,
              had_previous=current is not None,
              prompt_tokens=getattr(chunk_usage, "prompt_tokens", None)
              if not isinstance(chunk_usage, dict) else chunk_usage.get("prompt_tokens"),
              completion_tokens=getattr(chunk_usage, "completion_tokens", None)
              if not isinstance(chunk_usage, dict) else chunk_usage.get("completion_tokens"))
    except Exception as e:
        _dlog("grok_stream_usage_log_error", dlog=dlog,
              session_id=session_id, user_id=user_id,
              error_type=type(e).__name__, error=str(e)[:200])
    return chunk_usage


# ── Gotcha 7: reserved built-in tool-name collisions ───────────────────────

def check_tool_name_collisions(tools, dlog=None, session_id: str = "", user_id: str = ""):
    """Return the list of custom tool names colliding with xAI built-ins.

    Grok silently refuses to emit a call for a custom tool named e.g.
    ``web_search`` (pi-web-access#138). Accepts the OpenAI Chat Completions
    nested tool shape (``{"type":"function","function":{"name":...}}``) and the
    flat shape. Returns ``[]`` when clean. Never raises.
    """
    collisions = []
    for t in tools or []:
        try:
            name = ""
            if isinstance(t, dict):
                name = (t.get("function") or {}).get("name") or t.get("name") or ""
            if name in GROK_RESERVED_TOOL_NAMES:
                collisions.append(name)
        except Exception as e:
            _dlog("grok_tool_name_check_item_error", dlog=dlog,
                  session_id=session_id, user_id=user_id,
                  error_type=type(e).__name__, error=str(e)[:200])

    if collisions:
        _dlog("grok_tool_name_collision", dlog=dlog,
              session_id=session_id, user_id=user_id, collisions=collisions,
              reserved=list(GROK_RESERVED_TOOL_NAMES))
    else:
        _dlog("grok_tool_names_clean", dlog=dlog,
              session_id=session_id, user_id=user_id,
              tool_count=len(tools or []))
    return collisions


# ── Gotcha 8: stable prompt-cache routing key ──────────────────────────────

def prompt_cache_key_for_session(session_id: str = "", user_id: str = "", dlog=None) -> str:
    """Build a stable per-conversation prompt-cache routing key.

    Without a stable key xAI "often" serves a cache-cold server and full input
    price is paid every turn (docs.x.ai/developers/grok-4-5). Deterministic for
    a given (session_id, user_id) so every turn of one conversation routes
    together. Never raises.
    """
    try:
        base = session_id or user_id or ""
        key = f"surgicalai-{base}" if base else "surgicalai-anon"
    except Exception as e:
        _dlog("grok_cache_key_error", dlog=dlog,
              error_type=type(e).__name__, error=str(e)[:200])
        return "surgicalai-anon"
    _dlog("grok_cache_key_built", dlog=dlog,
          session_id=session_id, user_id=user_id, cache_key=key)
    return key


def prompt_cache_headers(session_id: str = "", user_id: str = "", dlog=None) -> dict:
    """Extra HTTP headers pinning a conversation to one cache-warm server.

    xAI documents ``x-grok-conv-id`` as the Chat-Completions-side equivalent of
    the Responses API's ``prompt_cache_key``. Never raises.
    """
    key = prompt_cache_key_for_session(session_id, user_id, dlog=dlog)
    _dlog("grok_cache_headers_built", dlog=dlog,
          session_id=session_id, user_id=user_id, cache_key=key)
    return {"x-grok-conv-id": key}


# ── Gotcha 9: 429 means rate-limit OR billing cap ──────────────────────────

#: Returned by ``classify_429``.
GROK_429_RATE_LIMIT = "rate_limit"
GROK_429_BILLING = "billing"

_BILLING_MARKERS = (
    "monthly spending limit",
    "spending limit",
    "available credits",
    "used all available credits",
    "out of credits",
    "billing",
    "quota exceeded",
)


def classify_429(error_body, dlog=None, session_id: str = "", user_id: str = "") -> str:
    """Classify an xAI 429 as a retryable rate limit vs. a hard billing cap.

    Both arrive as HTTP 429 but only one is retryable: a spend-cap 429 will
    never succeed on retry and must surface a "add credits / raise the cap in
    console.x.ai" message instead (continue#10373). Defaults to
    ``rate_limit`` — the conservative choice, since treating a real rate limit
    as a billing failure would kill an otherwise-recoverable run. Never raises.
    """
    try:
        text = error_body if isinstance(error_body, str) else _json.dumps(error_body, default=str)
    except Exception as e:
        _dlog("grok_429_classify_error", dlog=dlog,
              session_id=session_id, user_id=user_id,
              error_type=type(e).__name__, error=str(e)[:200])
        return GROK_429_RATE_LIMIT

    low = (text or "").lower()
    for marker in _BILLING_MARKERS:
        if marker in low:
            _dlog("grok_429_billing_cap", dlog=dlog,
                  session_id=session_id, user_id=user_id,
                  marker=marker, retryable=False)
            return GROK_429_BILLING

    _dlog("grok_429_rate_limit", dlog=dlog,
          session_id=session_id, user_id=user_id, retryable=True)
    return GROK_429_RATE_LIMIT


def grok_429_user_message(kind: str, dlog=None) -> str:
    """User-facing copy for each 429 kind. Never raises."""
    if kind == GROK_429_BILLING:
        _dlog("grok_429_message_billing", dlog=dlog, kind=kind)
        return ("xAI rejected the request: this team has used all available credits or hit "
                "its monthly spending limit. Add credits or raise the spend cap at "
                "console.x.ai — retrying will not help until that is resolved.")
    _dlog("grok_429_message_rate_limit", dlog=dlog, kind=kind)
    return "xAI rate limit hit — backing off and retrying."


# ── Gotcha 10: malformed tool-call argument JSON ───────────────────────────

def parse_tool_call_arguments(raw, dlog=None, session_id: str = "", user_id: str = "",
                              tool_name: str = ""):
    """Parse tool-call arguments, HTML-decoding first, with explicit feedback.

    Returns ``(ok, parsed, feedback)``:
      * ``ok`` — True when ``parsed`` is a usable dict.
      * ``parsed`` — the decoded arguments dict, or ``{}`` on failure.
      * ``feedback`` — ``""`` on success; on failure, a message that echoes the
        model's OWN malformed snippet back to it, the same defensive pattern
        already shipped on the GPT path (commits 22ad61a / 7c1ac0f), so the
        model can see and fix what it actually emitted instead of the error
        being silently swallowed.
    Never raises.
    """
    decoded = decode_tool_call_arguments(raw, dlog=dlog,
                                         session_id=session_id, user_id=user_id)
    if decoded in (None, ""):
        _dlog("grok_tool_args_empty", dlog=dlog, session_id=session_id,
              user_id=user_id, tool_name=tool_name)
        return False, {}, (
            f"Your tool call for `{tool_name or 'the tool'}` had empty arguments. "
            "Re-emit the tool call with a complete JSON arguments object."
        )

    try:
        parsed = _json.loads(decoded) if isinstance(decoded, str) else decoded
    except Exception as e:
        snippet = (decoded if isinstance(decoded, str) else str(decoded))[:500]
        _dlog("grok_tool_args_json_error", dlog=dlog,
              session_id=session_id, user_id=user_id, tool_name=tool_name,
              error_type=type(e).__name__, error=str(e)[:200],
              raw_preview=snippet[:200])
        return False, {}, (
            f"Your tool call for `{tool_name or 'the tool'}` had malformed JSON arguments "
            f"({type(e).__name__}: {str(e)[:150]}). This is what you sent:\n"
            f"{snippet}\n"
            "Re-emit the tool call with valid JSON — do not repeat the same broken payload."
        )

    if not isinstance(parsed, dict):
        _dlog("grok_tool_args_not_object", dlog=dlog,
              session_id=session_id, user_id=user_id, tool_name=tool_name,
              parsed_type=type(parsed).__name__)
        return False, {}, (
            f"Your tool call for `{tool_name or 'the tool'}` decoded to a "
            f"{type(parsed).__name__}, not a JSON object. Re-emit it as a JSON object."
        )

    _dlog("grok_tool_args_parsed", dlog=dlog, session_id=session_id,
          user_id=user_id, tool_name=tool_name, key_count=len(parsed))
    return True, parsed, ""


# ── Composite request shaper ────────────────────────────────────────────────

def shape_grok_request(messages, kwargs, dlog=None, session_id: str = "", user_id: str = "",
                       tools=None):
    """Apply every outgoing-request mitigation in one call.

    Returns ``(messages, kwargs, warnings)``. ``warnings`` lists non-fatal
    problems the caller may want to surface (currently reserved-tool-name
    collisions). Never raises.
    """
    safe_messages = sanitize_outgoing_messages(messages, dlog=dlog,
                                               session_id=session_id, user_id=user_id)
    safe_kwargs = strip_unsupported_params(kwargs, dlog=dlog,
                                           session_id=session_id, user_id=user_id)
    warnings = []
    collisions = check_tool_name_collisions(tools, dlog=dlog,
                                            session_id=session_id, user_id=user_id)
    if collisions:
        warnings.append(
            "Tool name(s) collide with xAI built-ins and will be silently refused: "
            + ", ".join(collisions)
        )
        _dlog("grok_request_shaped_with_warnings", dlog=dlog,
              session_id=session_id, user_id=user_id, warning_count=len(warnings))
    else:
        _dlog("grok_request_shaped", dlog=dlog,
              session_id=session_id, user_id=user_id,
              message_count=len(safe_messages or []), kwarg_count=len(safe_kwargs or {}))
    return safe_messages, safe_kwargs, warnings
