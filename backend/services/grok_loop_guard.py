"""Circuit breaker for a Grok agent loop repeating an identical no-op tool
call turn after turn.

WHY THIS MODULE EXISTS
-----------------------
The only thing currently bounding a Grok agent loop stuck calling the exact
same tool with the exact same arguments over and over is the generic
``AGENT_MAX_TURNS`` ceiling (default 24) in ``pipeline.py`` — there is no
repeat-specific detection, so a stuck loop burns up to 24 turns of wall-clock
time and API cost before the ceiling finally kills it.

PRIOR ART / DESIGN RATIONALE
-----------------------------
This is a well-known agent-framework failure mode. LangChain issue #26019
("The agent repeatedly calls the same tool without processing the output or
providing a response to the user.") documents the exact symptom. Community
write-ups converge on the same fix shape: hash the (tool, arguments) tuple
per step, keep a small bounded window of the last N signatures, and halt
with an explicit, loggable/diagnosable error the moment the window is fully
identical — NOT a swallowed exception, and not just "ran out of budget"
(the existing ``AGENT_MAX_TURNS` ceiling is a budget cap, not a diagnosis).
See e.g. "Stop AI Agents Looping on the Same Failed Tool Call"
(particula.tech/blog/stop-ai-agents-looping-same-tool-call-no-progress,
citing LangChain issue #26019) and the LangChain forum thread "Loop
prevention in Deep Agents with repeated tool calls", both of which use a
sliding window of the last ``max_repeats`` call signatures and trip once
they're all equal. This module follows the same shape, adapted to Grok's
existing ``report_blocked`` / ``<blocked>...</blocked>`` termination path
(see below) instead of raising a Python exception, since that's the
already-tested, already-wired-up clean-exit mechanism in ``pipeline.py``.

SIGNATURE CHOICE (documented per the task's explicit ask)
-----------------------------------------------------------
A turn's tool calls are reduced to a sorted tuple of
``(name, arguments)`` pairs, where ``arguments`` is the RAW JSON string
exactly as accumulated by ``StreamedToolCallAccumulator.finalize()`` — it is
NOT re-parsed/re-serialized. Sorting by ``(name, arguments)`` makes the
signature independent of the arrival order of multiple tool calls within a
single turn (so e.g. two calls in a turn arriving in a different order
across turns don't cause a false negative), while comparing the raw string
byte-for-byte is what actually proves the model repeated the identical
no-op: consider the model calling ``request_file(["x.py"])`` three turns
running — every real xAI response for that exact call re-serializes
identical JSON for identical arguments (verified by reading
``StreamedToolCallAccumulator.add_delta``, which simply concatenates the
provider's own streamed ``arguments`` string fragments — the provider, not
this code, controls the exact bytes). Re-parsing + re-dumping with
``sort_keys=True`` was considered as a hardening step against pure
key-ordering differences, but was rejected here: doing so risks MASKING a
real, meaningfully-different repeat behind normalization (e.g. silently
treating two different-but-JSON-equivalent payloads as "the same" is not
what we want to guard against — we want to guard against a literal no-op,
and the raw string is the most conservative, least surprising signal for
that). If key-ordering noise turns out to cause false negatives in practice
(the breaker failing to trip when it should), that can be revisited without
changing this module's public contract.

Only reachable from Grok-only code paths (this module doesn't import or
touch anything from the Claude/GPT text-tag scanner).
"""

import os

# `database` is a top-level module (backend/database.py), NOT under
# `services` — mirrors the exact import guard grok_provider.py itself uses
# for its own `_db_dlog` fallback.
try:  # pragma: no cover - import guard only
    from database import _dlog as _db_dlog
except Exception:  # pragma: no cover
    _db_dlog = None


#: Configurable the same way AGENT_MAX_TURNS is, so it can be tuned without a
#: code change if 3 turns out to be too aggressive/lenient in practice.
GROK_LOOP_REPEAT_LIMIT = int(os.getenv("GROK_LOOP_REPEAT_LIMIT", "3"))


def _dlog(event: str, dlog=None, **kwargs):
    """Structured debug log for this module. Prefers the caller-injected
    ``dlog`` (pipeline.py's ``_dlog``), falls back to ``database._dlog``.
    Never raises — a logging failure must never break a real request. Same
    defensive pattern as ``grok_provider.py``'s own ``_dlog`` helper."""
    try:
        if dlog is not None:
            dlog(event, **kwargs)
            return
        if _db_dlog is not None:
            _db_dlog(event, **kwargs)
    except Exception:
        pass


def signature_for_calls(calls: list) -> tuple:
    """Build a stable, order-independent-within-turn signature for a turn's
    finalized tool calls (as returned by
    ``StreamedToolCallAccumulator.finalize()``: a list of
    ``{"id", "name", "arguments"}`` dicts).

    Returns a tuple of ``(name, arguments)`` pairs sorted so that multiple
    calls within a single turn arriving in a different order don't cause a
    false negative/positive. ``arguments`` is used as the raw JSON string
    exactly as accumulated — see the module docstring for why this is not
    re-parsed/re-serialized. Never raises: any malformed entry is coerced to
    a string so a signature can always be produced.
    """
    try:
        pairs = []
        for c in calls or []:
            try:
                name = c.get("name") or ""
                arguments = c.get("arguments") or ""
            except Exception:
                name = ""
                arguments = str(c)
            pairs.append((str(name), str(arguments)))
        return tuple(sorted(pairs))
    except Exception:
        return tuple()


def check_repeated_calls(
    history: list, calls: list, max_repeats: int = GROK_LOOP_REPEAT_LIMIT,
    dlog=None, session_id: str = "", user_id: str = "",
) -> str:
    """Append this turn's signature to ``history`` (in place) and return a
    non-empty blocked-reason string once the last ``max_repeats`` entries in
    ``history`` are identical and non-empty. Returns ``None`` otherwise.

    ``history`` must be a cross-turn list owned by the caller (NOT reset
    every turn, unlike the per-turn ``_grok_blocked``/``_grok_tc_acc`` state
    in pipeline.py) so a genuine repeat across turns can be detected.
    Capped to the last ``max_repeats`` entries so it never grows unbounded
    over a long agent run. Never raises.
    """
    try:
        sig = signature_for_calls(calls)
        history.append(sig)
        # Cap history length — only the last max_repeats entries are ever
        # needed to detect a repeat, so trim eagerly to avoid unbounded growth.
        if len(history) > max_repeats:
            del history[: len(history) - max_repeats]

        _dlog("grok_loop_guard_check", dlog=dlog,
              session_id=session_id, user_id=user_id,
              history_len=len(history), max_repeats=max_repeats,
              signature_preview=str(sig)[:200])

        if not sig:
            # An empty signature (no calls) is never considered a repeat —
            # nothing to guard against here.
            return None

        if len(history) < max_repeats:
            return None

        last_n = history[-max_repeats:]
        if all(h == sig for h in last_n):
            tool_names = sorted({name for name, _args in sig}) if sig else []
            reason = (
                f"Stopped after {len(tool_names) or 1} identical tool call(s) repeated "
                f"{max_repeats} times in a row with no new progress — this usually means "
                "the requested context or action isn't resolving. Try rephrasing the "
                "request or providing the missing information directly."
            )
            _dlog("grok_loop_guard_tripped", dlog=dlog,
                  session_id=session_id, user_id=user_id,
                  repeat_count=max_repeats, tool_names=tool_names,
                  signature_preview=str(sig)[:200])
            return reason

        return None
    except Exception as e:
        _dlog("grok_loop_guard_check_error", dlog=dlog,
              session_id=session_id, user_id=user_id,
              error_type=type(e).__name__, error=str(e)[:300])
        return None
