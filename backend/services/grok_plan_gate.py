"""Grok (xAI) plan-first reliability gate for the SurgicalAI Edit/Agent loop.

WHY THIS MODULE EXISTS
----------------------
``grok-4.5`` in Edit/Agent mode is offered a full native toolset including
``write_edit_plan`` (see ``services/grok_agent_tools.py``), but with
``tool_choice="auto"`` (``pipeline.py`` seam) and a plan tool described as
merely "for 3+ edits", it reliably takes the cheapest interpretation of a
*compound* request: it emits a single shallow ``write_surgical_edit`` and stops,
leaving most of the request unaddressed.

SMOKING GUN (session 430d9711, ``surgical_debug_430d9711 (4).jsonl``): the
request "add a dedicated wizard button + widen the panel + ask job title / state
/ city / sources + keep results pretty" produced ONE edit (``.piq-panel`` width
580->660) and nothing else. Sonnet 5, same prompt, produced 9 coordinated
edits. Root cause is the optional, un-forced plan step — not a splicer/QA bug.

WHAT THIS MODULE DOES  (Option C: compound-detection + forced plan-first)
-------------------------------------------------------------------------
A small, dependency-free, per-request state machine, reachable ONLY from
``if _is_grok_model(...)`` branches in ``pipeline.py`` (Claude/GPT never touch
it). It:

1. **Detects compound requests** deterministically (``detect_compound_request``).
   A trivial request makes the whole gate a NO-OP: ``tool_choice`` stays
   ``"auto"`` and no tool call is ever intercepted — byte-for-byte current
   behavior, so simple edits stay fast ("trivial edits skip the gate entirely").

2. **Forces a plan before the first write** on a compound request. The model may
   gather context freely (request_* tools run under ``tool_choice="auto"``).
   The instant it tries to write (``write_surgical_edit`` / ``write_new_file``)
   with NO plan captured, those writes are HELD (dropped this turn, and each
   held write call's tool-result is rewritten to a "plan first" instruction so
   the xAI tool protocol still answers every ``tool_call_id``) and the next
   turn's ``tool_choice`` is forced to ``write_edit_plan``. Grok cannot skip
   planning. The pipeline MUST continue after a hold (see
   ``grok_plan_gate_continue_after_hold``) — a held turn looks like "no edits"
   to the text-only exit counter, and exiting there silently drops the forced
   plan turn (session cb380321 / Retry with QA).

WHY THIS IS ENOUGH (verified against real pipeline.py)
------------------------------------------------------
Once Grok emits a ``write_edit_plan``, the pipeline sets ``edit_plan_data`` and
the agent loop ends; the EXISTING, proven **Plan->Execute** machinery
(``pipeline.py`` ``if edit_plan_data:`` block) turns every ``{filename, symbol,
description}`` step into a real surgical edit via the per-symbol surgeon
(with same-symbol grouping + large-symbol handling). We therefore do NOT need a
completion/Focus-Chain re-injection loop — forcing the *plan* is the whole fix;
the reliable executor already guarantees each step becomes an edit.

DESIGN CONTRACT (verified against the real code at integration time)
--------------------------------------------------------------------
* ``grok_agent_tools.TranslationResult`` exposes ``edit_json_strings`` (list[str]
  JSON), ``new_file_json_strings`` (list[str] JSON), ``edit_plan``
  (list[{filename,symbol,description}] | None), ``results_by_id`` (dict) and
  ``calls`` (list[{id,name,arguments}]). ``filter_translation`` only MUTATES
  those fields in place — it never invents new pipeline state.
* xAI forced-tool syntax: ``{"type":"function","function":{"name": ...}}``.

FAIL-OPEN: any internal error, or too many forced-plan attempts without a valid
plan, reverts to ``tool_choice="auto"`` so the gate can never wedge a request.

LOGGING: every state transition emits a ``grok_plan_gate_*`` structured event
via the injected ``dlog`` (pipeline's ``_dlog``). Code bodies are never logged
verbatim — only counts, symbols and short reasons. No public method raises.
"""

import re as _re

# Native tool names (kept in sync with services/grok_agent_tools.py).
TOOL_WRITE_SURGICAL_EDIT = "write_surgical_edit"
TOOL_WRITE_NEW_FILE = "write_new_file"
TOOL_WRITE_EDIT_PLAN = "write_edit_plan"
_WRITE_TOOL_NAMES = frozenset({TOOL_WRITE_SURGICAL_EDIT, TOOL_WRITE_NEW_FILE})

# xAI/OpenAI forced-tool selector for the plan tool, and the default.
FORCE_PLAN_TOOL_CHOICE = {
    "type": "function",
    "function": {"name": TOOL_WRITE_EDIT_PLAN},
}
AUTO_TOOL_CHOICE = "auto"

# After this many forced-plan turns without a captured plan, stop forcing and
# fail open to "auto" so a pathological model can never wedge the loop.
MAX_FORCE_ATTEMPTS = 3

_EDIT_PRODUCING_MODES = frozenset({"edit", "agent"})

# ── Compound-request detection vocab (deterministic, conservative) ────────────
_ACTION_VERBS = (
    "add", "create", "make", "build", "implement", "widen", "wider", "resize",
    "enlarge", "ask", "update", "change", "remove", "delete", "wire", "hook",
    "refactor", "move", "rename", "replace", "introduce", "support", "enable",
    "show", "hide", "display", "store", "pass", "guide", "allow",
)
_TARGET_NOUNS = (
    "button", "modal", "wizard", "window", "panel", "field", "form", "source",
    "sources", "input", "dropdown", "menu", "dialog", "popup", "step", "screen",
    "page", "component", "state", "handler", "endpoint", "column", "row", "card",
    "placeholder", "tip", "title", "width",
)
_ENUM_RE = _re.compile(r"(?m)^\s*(?:\d+[.)]|[-*\u2022])\s+")


def _log(dlog, event, **kw):
    """Best-effort structured log; never raises, never blocks a request."""
    try:
        if dlog is not None:
            dlog(event, **kw)
    except Exception:
        pass


def _count_distinct(text_lc, vocab):
    out = []
    for w in vocab:
        if _re.search(r"\b" + _re.escape(w) + r"\b", text_lc):
            out.append(w)
    return out


def detect_compound_request(user_request, dlog=None, session_id="", user_id=""):
    """Return ``(is_compound: bool, signals: dict)``.

    Conservative on purpose — a false positive only costs one extra plan turn,
    but we still bias toward NOT gating obviously simple edits. Compound when
    ANY strong signal holds:
      * explicit enumeration (>=2 list markers), OR
      * >=2 distinct action verbs AND >=2 distinct target nouns, OR
      * long (>=240 chars) AND >=3 distinct action verbs, OR
      * >=2 imperative clauses (clauses beginning with an action verb).
    """
    try:
        text = user_request if isinstance(user_request, str) else ""
        text_lc = text.lower()
        n_chars = len(text)

        enum_hits = len(_ENUM_RE.findall(text))
        verbs = _count_distinct(text_lc, _ACTION_VERBS)
        nouns = _count_distinct(text_lc, _TARGET_NOUNS)

        clauses = _re.split(r"[.\n;]| and | also | then | plus |, ", text_lc)
        imperative_clauses = 0
        for c in clauses:
            parts = c.strip().split()
            if parts and parts[0] in _ACTION_VERBS:
                imperative_clauses += 1

        reasons = []
        if enum_hits >= 2:
            reasons.append("enumeration>=2")
        if len(verbs) >= 2 and len(nouns) >= 2:
            reasons.append("verbs>=2_AND_nouns>=2")
        if n_chars >= 240 and len(verbs) >= 3:
            reasons.append("long_AND_verbs>=3")
        if imperative_clauses >= 2:
            reasons.append("imperative_clauses>=2")

        is_compound = bool(reasons)
        signals = {
            "chars": n_chars, "enum_hits": enum_hits, "verbs": verbs,
            "nouns": nouns, "imperative_clauses": imperative_clauses,
            "reasons": reasons,
        }
        _log(dlog, "grok_plan_gate_detect", session_id=session_id, user_id=user_id,
             is_compound=is_compound, chars=n_chars, enum_hits=enum_hits,
             verb_count=len(verbs), noun_count=len(nouns),
             imperative_clauses=imperative_clauses, reasons=reasons)
        return is_compound, signals
    except Exception as e:  # pragma: no cover - defensive
        _log(dlog, "grok_plan_gate_detect_error", session_id=session_id,
             user_id=user_id, error_type=type(e).__name__, error=str(e)[:200])
        return False, {"error": str(e)[:200]}  # fail open (do not gate)


class GrokPlanGate:
    """Per-request plan-first state machine for a single Grok edit/agent run.

    Construct ONCE before the agent turn loop. Every method is a no-op unless
    the request is compound AND the mode is edit-producing.
    """

    def __init__(self, user_request, is_agent_task, dlog=None,
                 session_id="", user_id="", mode=None):
        self._dlog = dlog
        self._session_id = session_id
        self._user_id = user_id
        self.mode = str(mode or ("agent" if is_agent_task else "edit")).strip().lower()
        self.active = self.mode in _EDIT_PRODUCING_MODES
        if self.active:
            self.compound, self.signals = detect_compound_request(
                user_request, dlog=dlog, session_id=session_id, user_id=user_id)
        else:
            self.compound, self.signals = False, {"inactive_mode": self.mode}

        self.plan_captured = False
        self.plan_step_count = 0
        self._force_plan_next = False
        self._force_attempts = 0
        self.holds = 0
        # Last held write payloads (session cb380321): plan-exec can die on
        # Anthropic credit exhaustion AFTER we force a plan; keeping the
        # Grok writes lets credit-pause resume re-apply them without
        # re-calling Claude for symbols Grok already edited.
        self.last_held_edit_json_strings: list = []
        self.last_held_new_file_json_strings: list = []

        _log(dlog, "grok_plan_gate_init", session_id=session_id, user_id=user_id,
             active=self.active, mode=self.mode, compound=self.compound,
             reasons=self.signals.get("reasons"))

    # ── Seam 1: tool_choice for the upcoming turn ────────────────────────────
    def tool_choice(self, turn):
        """Return the ``tool_choice`` for ``turn``: forced ``write_edit_plan``
        only while armed, a plan is still missing, and we are under the
        fail-open force cap; otherwise ``"auto"`` (unchanged behavior)."""
        try:
            if not (self.active and self.compound):
                return AUTO_TOOL_CHOICE
            if (self._force_plan_next and not self.plan_captured
                    and self._force_attempts < MAX_FORCE_ATTEMPTS):
                self._force_attempts += 1
                _log(self._dlog, "grok_plan_gate_force_plan", turn=turn,
                     session_id=self._session_id, user_id=self._user_id,
                     attempt=self._force_attempts, max_attempts=MAX_FORCE_ATTEMPTS)
                return FORCE_PLAN_TOOL_CHOICE
            if self._force_plan_next and self._force_attempts >= MAX_FORCE_ATTEMPTS:
                _log(self._dlog, "grok_plan_gate_force_cap_failopen", turn=turn,
                     session_id=self._session_id, user_id=self._user_id,
                     attempts=self._force_attempts)
            return AUTO_TOOL_CHOICE
        except Exception:  # pragma: no cover - defensive
            return AUTO_TOOL_CHOICE

    def is_forcing_plan(self, turn):
        """True when the upcoming ``turn`` will force the plan tool — used only
        to surface a nice progress message. Does NOT mutate state."""
        return bool(self.active and self.compound and self._force_plan_next
                    and not self.plan_captured
                    and self._force_attempts < MAX_FORCE_ATTEMPTS)

    # ── Seam 2: inspect/adjust a translation result in place ─────────────────
    def filter_translation(self, tr, turn):
        """MUTATE ``tr`` in place to enforce plan-before-write and note plan
        capture.

        Returns ``True`` when writes were held this call (pipeline must continue
        into the forced-plan turn — do NOT count the hold as a text-only exit).
        Returns ``False`` otherwise.
        """
        try:
            if not (self.active and self.compound) or tr is None:
                return False

            # (a) A plan arrived (model chose it OR was forced) — record it.
            if getattr(tr, "edit_plan", None) and not self.plan_captured:
                steps = [s for s in tr.edit_plan
                         if isinstance(s, dict) and (s.get("filename") or s.get("symbol"))]
                self.plan_captured = True
                self.plan_step_count = len(steps)
                self._force_plan_next = False
                _log(self._dlog, "grok_plan_gate_plan_captured", turn=turn,
                     session_id=self._session_id, user_id=self._user_id,
                     step_count=len(steps),
                     steps=[{"filename": s.get("filename"), "symbol": s.get("symbol")}
                            for s in steps][:40])
                return False

            # (b) Enforce plan-before-write: hold any writes emitted with no plan.
            edits = list(getattr(tr, "edit_json_strings", []) or [])
            newfiles = list(getattr(tr, "new_file_json_strings", []) or [])
            if (edits or newfiles) and not self.plan_captured:
                held = len(edits) + len(newfiles)
                # Persist copies BEFORE clearing — credit-pause resume needs them.
                self.last_held_edit_json_strings = list(edits)
                self.last_held_new_file_json_strings = list(newfiles)
                tr.edit_json_strings = []
                tr.new_file_json_strings = []
                self._force_plan_next = True
                self.holds += 1
                self._rewrite_write_results(
                    tr,
                    "This is a multi-part request. Do NOT emit a shallow single "
                    "edit. Either (A) call write_edit_plan with ONE step per "
                    "DISTINCT symbol/file, or (B) if several sites live inside "
                    "ONE large component, call request_file then multiple "
                    "write_surgical_edit snippets covering every site — do NOT "
                    "plan-split that mega-symbol.")
                _log(self._dlog, "grok_plan_gate_hold_writes", turn=turn,
                     session_id=self._session_id, user_id=self._user_id,
                     held_writes=held, total_holds=self.holds,
                     stashed_edits=len(self.last_held_edit_json_strings),
                     stashed_new_files=len(self.last_held_new_file_json_strings))
                return True
            return False
        except Exception as e:  # pragma: no cover - defensive
            _log(self._dlog, "grok_plan_gate_filter_error", turn=turn,
                 session_id=self._session_id, user_id=self._user_id,
                 error_type=type(e).__name__, error=str(e)[:200])
            return False

    def _rewrite_write_results(self, tr, message):
        """Point every held write call's tool-result at ``message`` so the xAI
        protocol still answers each ``tool_call_id`` — telling Grok the edit was
        deferred pending a plan."""
        try:
            rbi = getattr(tr, "results_by_id", None)
            if rbi is None:
                return
            for c in (getattr(tr, "calls", None) or []):
                name = (c.get("name") if isinstance(c, dict) else None) or ""
                cid = (c.get("id") if isinstance(c, dict) else None) or ""
                if name in _WRITE_TOOL_NAMES and cid:
                    rbi[cid] = message
        except Exception:  # pragma: no cover - defensive
            pass

    def status(self):
        """Compact snapshot for a final summary log."""
        return {
            "active": self.active, "compound": self.compound,
            "plan_captured": self.plan_captured,
            "plan_steps": self.plan_step_count,
            "holds": self.holds, "force_attempts": self._force_attempts,
        }
