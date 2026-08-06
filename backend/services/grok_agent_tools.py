"""Grok (xAI) native tool-calling adapter for the SurgicalAI Edit/Agent loop.

WHY THIS MODULE EXISTS
----------------------
The single-pass edit/agent loop in ``services/pipeline.py``
(``run_natural_pipeline_stream``) drives Claude and GPT through a *text tag*
contract: the model writes ``<surgical_edit>…</surgical_edit>`` /
``<new_file>…</new_file>`` / ``<search_request>…</search_request>`` etc. into
its normal text stream, and an incremental scanner pulls those bodies out.

Grok (grok-4.5) is a reasoning model that has been observed **narrating instead
of emitting the XML tags** — it says "I would change X" in prose and never
produces a parseable ``<surgical_edit>`` block, so ``edit_blocks_raw`` stays
empty and the user sees "no code changes" (the reported production bug). xAI's
API is, however, first-class at OpenAI-style *native* function/tool calling.

This module lets the pipeline hand Grok the SAME capabilities as native
function tools and then translate the resulting ``tool_calls`` back into the
EXACT internal shapes the existing tag scanner already produces, so 100% of the
downstream dispatch/edit-application/QA code is reused unchanged. Nothing here
touches the Claude or GPT text-tag paths — every function is only reachable from
an ``if _is_grok_model(...):`` branch in pipeline.py.

DESIGN CONTRACT (verified against the real pipeline.py at integration time)
--------------------------------------------------------------------------
* ``write_surgical_edit`` -> ``json.dumps(args)`` appended to ``edit_blocks_raw``
  (fields: filename, symbol, description, new_code [+ old_code |
  edit_start_line/edit_end_line]).
* ``write_new_file``      -> ``json.dumps(args)`` appended to
  ``new_file_blocks_raw`` (fields: filename, language, summary, content).
* ``write_edit_plan``     -> ``edit_plan_data`` list of {filename, symbol,
  description}.
* ``request_file``        -> ``pending_tool = ("file", filenames[:5])`` via the
  pipeline's own ``_capture_request("filereq", <json array>)``.
* ``request_search``      -> ``pending_tool = ("search", {"terms":[...],
  "reason":...})`` via ``_capture_request("search", <json object>)``.
* ``request_github``      -> ``_capture_request("github", <json {tool,args}>)``.
* ``request_history``     -> ``_capture_request("history", <json {filename,
  query}>)``.
* ``report_blocked``      -> terminal ``<blocked>reason</blocked>`` signal.

To reuse the pipeline's already-proven validators exactly (and avoid a circular
import), the *translator* returns a ``(tag_kind, body_json_string)`` tuple for
context requests and lets pipeline.py call its own ``_capture_request`` on it —
so ``_parse_search_content`` / ``_parse_filereq_content`` /
``_parse_history_content`` / ``parse_github_request`` run byte-for-byte the same
as on the Claude/GPT tag path.

LOGGING
-------
Every public function takes an optional ``dlog`` callable (pipeline.py's
``_dlog``, dependency-injected to avoid a circular import) and logs at entry,
success and error. Long strings are truncated and file contents / edit bodies
are never logged verbatim — only lengths and previews.
"""

import json as _json

# grok_provider does NOT import this module, so importing from it is cycle-safe.
# It gives us the HTML-entity-decoding + explicit-feedback argument parser that
# the rest of the Grok integration already relies on.
try:  # pragma: no cover - import shape differs only by cwd
    from services.grok_provider import parse_tool_call_arguments as _parse_tc_args
except Exception:  # pragma: no cover
    try:
        from grok_provider import parse_tool_call_arguments as _parse_tc_args
    except Exception:  # pragma: no cover
        _parse_tc_args = None


# ── Tool name constants (single source of truth; used by tests too) ──────────
TOOL_WRITE_SURGICAL_EDIT = "write_surgical_edit"
TOOL_WRITE_NEW_FILE = "write_new_file"
TOOL_WRITE_EDIT_PLAN = "write_edit_plan"
TOOL_REQUEST_FILE = "request_file"
TOOL_REQUEST_SEARCH = "request_search"
TOOL_REQUEST_GITHUB = "request_github"
TOOL_REQUEST_HISTORY = "request_history"
TOOL_REPORT_BLOCKED = "report_blocked"

WRITE_TOOLS = frozenset({
    TOOL_WRITE_SURGICAL_EDIT, TOOL_WRITE_NEW_FILE, TOOL_WRITE_EDIT_PLAN,
})
CONTEXT_TOOLS = frozenset({
    TOOL_REQUEST_FILE, TOOL_REQUEST_SEARCH, TOOL_REQUEST_GITHUB,
    TOOL_REQUEST_HISTORY,
})

_EDIT_MODES = frozenset({"edit", "agent"})
_READ_MODES = frozenset({"ask", "plan"})


def _log(dlog, event, **kwargs):
    """Best-effort structured log — never raises, never blocks a real request."""
    try:
        if dlog is not None:
            dlog(event, **kwargs)
    except Exception:
        pass


def _get(obj, key, default=None):
    """Read ``key`` from either an object (SDK delta) or a dict (tests)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tool schemas
# ─────────────────────────────────────────────────────────────────────────────

def _schema_write_surgical_edit():
    return {
        "type": "function",
        "function": {
            "name": TOOL_WRITE_SURGICAL_EDIT,
            "description": (
                "Emit ONE precise edit to an EXISTING file. Call this once per "
                "symbol you are changing; you may call it multiple times in a "
                "single turn. Provide the COMPLETE replacement code for the "
                "symbol/region — never a diff or an ellipsis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Exact filename as shown in the file context.",
                    },
                    "symbol": {
                        "type": "string",
                        "description": (
                            "Exact symbol name from the SYMBOL INDEX "
                            "(e.g. LoginPage, handleSubmit, process_order)."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "What changed and why — one clear sentence.",
                    },
                    "new_code": {
                        "type": "string",
                        "description": (
                            "The COMPLETE new code for the symbol/region — every "
                            "line, nothing omitted."
                        ),
                    },
                    "old_code": {
                        "type": "string",
                        "description": (
                            "The exact original text you are replacing, copied "
                            "verbatim. Your PRIMARY anchor — include it whenever "
                            "you are not certain of exact line numbers."
                        ),
                    },
                    "edit_start_line": {
                        "type": "integer",
                        "description": "Optional absolute start line of the region.",
                    },
                    "edit_end_line": {
                        "type": "integer",
                        "description": "Optional absolute end line of the region.",
                    },
                },
                "required": ["filename", "symbol", "description", "new_code"],
            },
        },
    }


def _schema_write_new_file():
    return {
        "type": "function",
        "function": {
            "name": TOOL_WRITE_NEW_FILE,
            "description": (
                "Create a brand-new file that does not exist yet. Provide the "
                "COMPLETE file content, ready to use."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Path/name of the new file."},
                    "language": {"type": "string", "description": "Language, e.g. typescript, python."},
                    "summary": {"type": "string", "description": "One sentence: what this file does."},
                    "content": {
                        "type": "string",
                        "description": "The COMPLETE file content — every import, every line.",
                    },
                },
                "required": ["filename", "language", "summary", "content"],
            },
        },
    }


def _schema_write_edit_plan():
    return {
        "type": "function",
        "function": {
            "name": TOOL_WRITE_EDIT_PLAN,
            "description": (
                "For large/multi-part changes (3+ edits): emit a symbol-level "
                "plan instead of inline edits. One step per symbol to change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Ordered list of edit steps.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "filename": {"type": "string"},
                                "symbol": {"type": "string"},
                                "description": {
                                    "type": "string",
                                    "description": "What to change and why.",
                                },
                            },
                            "required": ["filename", "symbol", "description"],
                        },
                    },
                },
                "required": ["steps"],
            },
        },
    }


def _schema_request_file():
    return {
        "type": "function",
        "function": {
            "name": TOOL_REQUEST_FILE,
            "description": (
                "Pull the FULL contents of specific file(s) you need to see "
                "before editing. Up to 5 files per call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filenames": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact filenames to load.",
                    },
                },
                "required": ["filenames"],
            },
        },
    }


def _schema_request_search():
    return {
        "type": "function",
        "function": {
            "name": TOOL_REQUEST_SEARCH,
            "description": (
                "Grep the codebase for symbols or text you need to find. Returns "
                "matching snippets with file/line context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact symbols/keywords to search for.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why you need these results (one short phrase).",
                    },
                },
                "required": ["terms"],
            },
        },
    }


def _schema_request_github():
    # NOTE (proven root cause — session 36118be1, 2026-08-06): this schema used
    # to expose only an opaque {tool, args} wrapper with ZERO documentation of
    # what "tool" values exist or what each one accepts. Claude's equivalent
    # native schema (services/github_context_tools.py) documents every op
    # individually, INCLUDING read_file's "start_line" paging parameter — Grok's
    # never did. Result: Grok never once passed start_line (confirmed: zero
    # occurrences across a 620-event debug log) and instead re-requested the
    # same file from the top repeatedly, always getting the same first ~14,000
    # characters back, burning its entire agent-turn budget on a single large
    # file without ever reaching an edit. This enum + per-op description gives
    # Grok the same fidelity Claude already has. See also the deterministic
    # start_line auto-continue backstop in pipeline.py's github dispatch
    # (_kind == "github" branch) — this schema fix and that backstop are BOTH
    # needed: the schema lets Grok ask correctly; the backstop guarantees
    # forward progress even if it still doesn't.
    return {
        "type": "function",
        "function": {
            "name": TOOL_REQUEST_GITHUB,
            "description": (
                "Read from the connected GitHub repository. Large files are "
                "PAGED: read_file returns at most ~14,000 characters per call. "
                "If the result ends with '[TRUNCATED — N more lines. Call "
                "read_file again with start_line=X to continue.]', you have "
                "NOT seen the whole file — pass that exact start_line on your "
                "next read_file call for the SAME path to continue from where "
                "you left off. Do NOT re-call read_file on the same path "
                "without start_line — that just re-fetches the same first "
                "page again and wastes a turn. Tip: call search_code first to "
                "find which lines matter, then jump straight there with "
                "start_line instead of paging through the whole file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "description": (
                            "The GitHub operation to invoke. One of: "
                            "list_repos (no args) — list connected repos; "
                            "list_files (owner, repo, path?, ref?) — list a "
                            "directory; "
                            "read_file (owner, repo, path, ref?, start_line?) "
                            "— read file content, paged at ~14,000 chars; "
                            "always pass start_line to continue past a "
                            "'[TRUNCATED]' result; "
                            "search_code (owner, repo, query) — search file "
                            "contents (default branch only); returns matching "
                            "paths, not line numbers — follow up with "
                            "read_file; "
                            "check_deploy (owner, repo) — check deploy status; "
                            "list_prs / get_pr_diff / get_pr_comments / "
                            "list_issues / get_issue_comments / diff_branches "
                            "— see 'args' for each op's fields, mirrored "
                            "from GitHub's REST API of the same name."
                        ),
                        "enum": [
                            "list_repos", "list_files", "read_file",
                            "search_code", "check_deploy", "list_prs",
                            "get_pr_diff", "get_pr_comments", "list_issues",
                            "get_issue_comments", "diff_branches",
                        ],
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Arguments for the chosen 'tool' (may be empty for "
                            "list_repos). For read_file: {owner, repo, path, "
                            "ref?, start_line?} — start_line is 1-indexed and "
                            "REQUIRED to see past a truncated first page."
                        ),
                    },
                },
                "required": ["tool"],
            },
        },
    }


def _schema_request_history():
    return {
        "type": "function",
        "function": {
            "name": TOOL_REQUEST_HISTORY,
            "description": (
                "Look up an OLDER/original version of a file from THIS session's "
                "edit history. Returned content is HISTORICAL — never treat it as "
                "the current file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "File to look up."},
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional keyword to search across saved versions. "
                            "Omit for the very first (pre-edit) version."
                        ),
                    },
                },
                "required": ["filename"],
            },
        },
    }


def _schema_report_blocked():
    return {
        "type": "function",
        "function": {
            "name": TOOL_REPORT_BLOCKED,
            "description": (
                "Call this ONLY when required context is genuinely unavailable "
                "and you cannot proceed. Explain exactly what you need. This "
                "ends the turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "What is blocking you and what you need.",
                    },
                },
                "required": ["reason"],
            },
        },
    }


def build_grok_agent_tools(mode="edit", github_enabled=False, history_enabled=False,
                           dlog=None, session_id="", user_id=""):
    """Return the OpenAI-style tool schemas Grok should be offered for ``mode``.

    * ``edit`` / ``agent`` — full write + read toolset + report_blocked.
    * ``ask`` / ``plan``   — READ-ONLY request tools + report_blocked (no
      write/edit tools: those modes never run the edit pipeline).

    ``github_enabled`` / ``history_enabled`` gate the two conditional context
    tools, mirroring the pipeline's ``_gh_nat_enabled`` / ``is_agent_task``
    registration of the corresponding XML tags.
    """
    m = (mode or "edit").strip().lower()
    _log(dlog, "grok_build_tools_entry", session_id=session_id, user_id=user_id,
         mode=m, github_enabled=bool(github_enabled),
         history_enabled=bool(history_enabled))

    tools = []
    if m in _EDIT_MODES:
        tools.append(_schema_write_surgical_edit())
        tools.append(_schema_write_new_file())
        tools.append(_schema_write_edit_plan())

    # Read/context tools are available in every mode.
    tools.append(_schema_request_file())
    tools.append(_schema_request_search())
    if github_enabled:
        tools.append(_schema_request_github())
    if history_enabled:
        tools.append(_schema_request_history())

    # report_blocked is meaningful in every mode as a clean terminal signal.
    tools.append(_schema_report_blocked())

    _log(dlog, "grok_build_tools_done", session_id=session_id, user_id=user_id,
         mode=m, tool_count=len(tools),
         tool_names=[t["function"]["name"] for t in tools])
    return tools


# ─────────────────────────────────────────────────────────────────────────────
# 2. Streamed tool-call delta accumulator
# ─────────────────────────────────────────────────────────────────────────────

class StreamedToolCallAccumulator:
    """Merge OpenAI/xAI streamed ``delta.tool_calls`` fragments into complete
    ``{"id", "name", "arguments"}`` records.

    Real streaming tool calls arrive in fragments indexed by ``.index``: the
    first fragment for an index usually carries ``id`` + ``function.name`` and
    an ``arguments`` prefix, and later fragments append more ``arguments``
    characters (and nothing else). Accumulation MUST be keyed on ``index`` —
    ``id`` is only present on the first fragment. Handles both SDK objects and
    plain dicts (used by tests). Never raises.
    """

    def __init__(self, dlog=None, session_id="", user_id=""):
        self._dlog = dlog
        self._session_id = session_id
        self._user_id = user_id
        self._by_index = {}
        self._order = []

    def add_delta(self, tool_calls_delta):
        if not tool_calls_delta:
            return
        for tc in tool_calls_delta:
            try:
                idx = _get(tc, "index")
                if idx is None:
                    # Some providers omit index for a single call — fold into 0.
                    idx = 0
                if idx not in self._by_index:
                    self._by_index[idx] = {"id": None, "name": None, "arguments": ""}
                    self._order.append(idx)
                entry = self._by_index[idx]

                tcid = _get(tc, "id")
                if tcid:
                    entry["id"] = tcid
                fn = _get(tc, "function")
                if fn is not None:
                    name = _get(fn, "name")
                    if name:
                        entry["name"] = name
                    args = _get(fn, "arguments")
                    if args:
                        entry["arguments"] += args
            except Exception as e:  # pragma: no cover - defensive
                _log(self._dlog, "grok_tc_accumulate_error",
                     session_id=self._session_id, user_id=self._user_id,
                     error_type=type(e).__name__, error=str(e)[:200])

    def has_calls(self):
        return bool(self._by_index)

    def finalize(self):
        """Return completed calls in arrival order, each a dict with a
        guaranteed-non-empty ``id`` (synthesized if the provider omitted one)."""
        calls = []
        for i, idx in enumerate(self._order):
            entry = self._by_index[idx]
            cid = entry["id"] or f"call_{idx}_{i}"
            calls.append({
                "id": cid,
                "name": entry["name"] or "",
                "arguments": entry["arguments"] or "",
            })
        _log(self._dlog, "grok_tc_finalize",
             session_id=self._session_id, user_id=self._user_id,
             call_count=len(calls),
             names=[c["name"] for c in calls],
             arg_lens=[len(c["arguments"]) for c in calls])
        return calls


# ─────────────────────────────────────────────────────────────────────────────
# 3. Translator: completed tool calls -> pipeline producer shapes
# ─────────────────────────────────────────────────────────────────────────────

class TranslationResult:
    """Neutral adapter output. Contains NO pipeline globals — pipeline.py maps
    ``context_request`` through its own ``_capture_request`` so the existing
    validators run unchanged."""

    def __init__(self):
        self.edit_json_strings = []          # -> extend edit_blocks_raw
        self.new_file_json_strings = []      # -> extend new_file_blocks_raw
        self.edit_plan = None                # -> edit_plan_data (list) or None
        self.context_request = None          # (tag_kind, body_json_str) or None
        self.context_call_id = None          # tool_call_id of the context req
        self.blocked_reason = None           # -> terminal <blocked> signal
        self.results_by_id = {}              # tool_call_id -> tool-result text
        self.errors = []                     # list of (tool_call_id, feedback)
        self.calls = []                      # raw completed calls (for messages)

    def produced_any(self):
        return bool(
            self.edit_json_strings or self.new_file_json_strings
            or self.edit_plan is not None or self.context_request is not None
            or self.blocked_reason is not None
        )


def _parse_args(raw, dlog, session_id, user_id, tool_name):
    """Parse a tool-call ``arguments`` JSON string, HTML-decoding first.

    Prefers ``grok_provider.parse_tool_call_arguments`` (shared with the rest of
    the Grok integration; handles HTML-entity-encoded args + gives explicit
    model feedback). Falls back to a local decode+json.loads if that helper is
    unavailable. Returns ``(ok, dict, feedback)``.
    """
    if _parse_tc_args is not None:
        return _parse_tc_args(raw, dlog=dlog, session_id=session_id,
                              user_id=user_id, tool_name=tool_name)
    # Fallback (only if grok_provider import failed): minimal, still HTML-safe.
    import html as _html
    try:
        decoded = _html.unescape(raw) if isinstance(raw, str) else raw
        parsed = _json.loads(decoded) if isinstance(decoded, str) else decoded
        if isinstance(parsed, dict):
            return True, parsed, ""
        return False, {}, f"`{tool_name}` arguments were not a JSON object."
    except Exception as e:
        return False, {}, (
            f"`{tool_name}` had malformed JSON arguments ({type(e).__name__}). "
            "Re-emit valid JSON."
        )


def _nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def translate_tool_calls(calls, dlog=None, session_id="", user_id=""):
    """Translate completed Grok tool calls into pipeline producer shapes.

    Precedence policy (matches the tag-path behavior of dispatching ONE context
    request per turn while allowing MANY writes): every valid write call is
    collected; the FIRST valid context request is dispatched and any later
    context requests are recorded as an error/no-op so the model re-asks next
    turn. A ``report_blocked`` call sets a terminal signal.
    """
    res = TranslationResult()
    res.calls = list(calls or [])
    _log(dlog, "grok_translate_entry", session_id=session_id, user_id=user_id,
         call_count=len(res.calls), names=[_get(c, "name") for c in res.calls])

    for c in res.calls:
        cid = _get(c, "id") or ""
        name = _get(c, "name") or ""
        raw_args = _get(c, "arguments") or ""
        ok, args, feedback = _parse_args(raw_args, dlog, session_id, user_id, name)

        if not ok:
            res.errors.append((cid, feedback))
            res.results_by_id[cid] = feedback
            _log(dlog, "grok_translate_bad_args", session_id=session_id,
                 user_id=user_id, tool_name=name, tool_call_id=cid)
            continue

        if name == TOOL_WRITE_SURGICAL_EDIT:
            if not _nonempty_str(args.get("filename")) or not _nonempty_str(args.get("new_code")):
                fb = ("`write_surgical_edit` requires non-empty `filename` and "
                      "`new_code`. Re-emit with the complete replacement code.")
                res.errors.append((cid, fb))
                res.results_by_id[cid] = fb
                _log(dlog, "grok_translate_edit_missing_fields",
                     session_id=session_id, user_id=user_id, tool_call_id=cid,
                     has_filename=_nonempty_str(args.get("filename")),
                     has_new_code=_nonempty_str(args.get("new_code")))
                continue
            res.edit_json_strings.append(_json.dumps(args))
            res.results_by_id[cid] = "Surgical edit recorded."
            _log(dlog, "grok_translate_surgical_edit", session_id=session_id,
                 user_id=user_id, tool_call_id=cid,
                 filename=str(args.get("filename"))[:120],
                 symbol=str(args.get("symbol"))[:120],
                 new_code_len=len(str(args.get("new_code"))),
                 has_old_code=_nonempty_str(args.get("old_code")),
                 has_lines=("edit_start_line" in args and "edit_end_line" in args))

        elif name == TOOL_WRITE_NEW_FILE:
            if not _nonempty_str(args.get("filename")) or not _nonempty_str(args.get("content")):
                fb = ("`write_new_file` requires non-empty `filename` and "
                      "`content`. Re-emit with the complete file content.")
                res.errors.append((cid, fb))
                res.results_by_id[cid] = fb
                _log(dlog, "grok_translate_newfile_missing_fields",
                     session_id=session_id, user_id=user_id, tool_call_id=cid)
                continue
            res.new_file_json_strings.append(_json.dumps(args))
            res.results_by_id[cid] = "New file recorded."
            _log(dlog, "grok_translate_new_file", session_id=session_id,
                 user_id=user_id, tool_call_id=cid,
                 filename=str(args.get("filename"))[:120],
                 content_len=len(str(args.get("content"))))

        elif name == TOOL_WRITE_EDIT_PLAN:
            steps = args.get("steps")
            valid = []
            if isinstance(steps, list):
                valid = [s for s in steps
                         if isinstance(s, dict) and s.get("filename") and s.get("symbol")]
            if not valid:
                fb = ("`write_edit_plan` needs a non-empty `steps` array where "
                      "each step has `filename` and `symbol`.")
                res.errors.append((cid, fb))
                res.results_by_id[cid] = fb
                _log(dlog, "grok_translate_plan_empty", session_id=session_id,
                     user_id=user_id, tool_call_id=cid)
                continue
            # First valid plan wins (mirrors edit_plan_data single-assignment).
            if res.edit_plan is None:
                res.edit_plan = valid
            res.results_by_id[cid] = "Edit plan recorded."
            _log(dlog, "grok_translate_edit_plan", session_id=session_id,
                 user_id=user_id, tool_call_id=cid, step_count=len(valid))

        elif name == TOOL_REPORT_BLOCKED:
            reason = args.get("reason")
            res.blocked_reason = str(reason).strip() if _nonempty_str(reason) else \
                "Blocked: required context is unavailable."
            res.results_by_id[cid] = "Blocked signal acknowledged."
            _log(dlog, "grok_translate_report_blocked", session_id=session_id,
                 user_id=user_id, tool_call_id=cid,
                 reason_preview=res.blocked_reason[:200])

        elif name in CONTEXT_TOOLS:
            # Only the FIRST context request is dispatched this turn.
            if res.context_request is not None:
                fb = ("Only one context request is handled per turn. Your other "
                      "requests were not run — re-request them next turn if still "
                      "needed.")
                res.errors.append((cid, fb))
                res.results_by_id[cid] = fb
                _log(dlog, "grok_translate_extra_context_ignored",
                     session_id=session_id, user_id=user_id, tool_call_id=cid,
                     tool_name=name)
                continue
            tag_kind, body = _context_body(name, args)
            res.context_request = (tag_kind, body)
            res.context_call_id = cid
            # NOTE: context result text is filled in by pipeline.py after it runs
            # the tool (search results / file contents), so we do NOT set
            # results_by_id[cid] here.
            _log(dlog, "grok_translate_context_request", session_id=session_id,
                 user_id=user_id, tool_call_id=cid, tool_name=name,
                 tag_kind=tag_kind, body_preview=body[:200])

        else:
            fb = f"Unknown tool `{name or '(none)'}`. Use only the provided tools."
            res.errors.append((cid, fb))
            res.results_by_id[cid] = fb
            _log(dlog, "grok_translate_unknown_tool", session_id=session_id,
                 user_id=user_id, tool_call_id=cid, tool_name=name)

    _log(dlog, "grok_translate_done", session_id=session_id, user_id=user_id,
         edits=len(res.edit_json_strings), new_files=len(res.new_file_json_strings),
         has_plan=res.edit_plan is not None,
         has_context=res.context_request is not None,
         blocked=res.blocked_reason is not None, errors=len(res.errors))
    return res


def _context_body(name, args):
    """Map a native context tool's args to the ``(tag_kind, json_body)`` pair
    the pipeline's ``_capture_request`` expects. The body is exactly what the
    corresponding XML tag would have contained, so the same parser runs."""
    if name == TOOL_REQUEST_FILE:
        filenames = args.get("filenames")
        if isinstance(filenames, str):
            filenames = [filenames]
        if not isinstance(filenames, list):
            filenames = []
        return "filereq", _json.dumps(filenames)
    if name == TOOL_REQUEST_SEARCH:
        payload = {"terms": args.get("terms", []), "reason": args.get("reason", "")}
        return "search", _json.dumps(payload)
    if name == TOOL_REQUEST_GITHUB:
        payload = {"tool": args.get("tool", ""), "args": args.get("args", {}) or {}}
        return "github", _json.dumps(payload)
    if name == TOOL_REQUEST_HISTORY:
        payload = {"filename": args.get("filename", "")}
        if args.get("query"):
            payload["query"] = args.get("query")
        return "history", _json.dumps(payload)
    # unreachable given the CONTEXT_TOOLS gate
    return name, _json.dumps(args)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Native tool-result conversation messages
# ─────────────────────────────────────────────────────────────────────────────

def build_assistant_tool_calls_message(assistant_text, calls):
    """Build the assistant message that CARRIES the tool calls.

    xAI 400s on ``content: null`` alongside ``tool_calls`` (langchain#34140), so
    ``content`` is always a string (empty allowed). Every call is echoed with
    its id/name/arguments so the following ``role:tool`` messages line up.
    """
    tool_calls = []
    for i, c in enumerate(calls or []):
        cid = _get(c, "id") or f"call_{i}"
        tool_calls.append({
            "id": cid,
            "type": "function",
            "function": {
                "name": _get(c, "name") or "",
                "arguments": _get(c, "arguments") or "{}",
            },
        })
    return {
        "role": "assistant",
        "content": assistant_text if isinstance(assistant_text, str) else "",
        "tool_calls": tool_calls,
    }


def build_tool_result_messages(calls, results_by_id, context_call_id=None,
                               context_result=None, default_result="ok"):
    """Build one ``role:tool`` message per tool call, in call order.

    xAI (like OpenAI) requires EVERY ``tool_call`` in the assistant message to
    be answered by a matching ``role:tool`` / ``tool_call_id`` message before
    the next model turn. Write-tool results are short acknowledgements; the
    single context call's result is the real observation (search hits / file
    contents) supplied by pipeline.py.
    """
    msgs = []
    for i, c in enumerate(calls or []):
        cid = _get(c, "id") or f"call_{i}"
        if context_call_id is not None and cid == context_call_id and context_result is not None:
            content = context_result
        else:
            content = results_by_id.get(cid, default_result)
        if not isinstance(content, str):
            content = str(content)
        msgs.append({"role": "tool", "tool_call_id": cid, "content": content})
    return msgs


def build_native_followup_messages(assistant_text, calls, results_by_id,
                                   context_call_id=None, context_result=None,
                                   dlog=None, session_id="", user_id=""):
    """Full native continuation block: [assistant(tool_calls)] + [tool results].

    This REPLACES the plain assistant-text/user-observation echo pair the
    tag-based path uses, so Grok sees a protocol-correct tool conversation and
    keeps its tool state across turns.
    """
    assistant_msg = build_assistant_tool_calls_message(assistant_text, calls)
    tool_msgs = build_tool_result_messages(
        calls, results_by_id, context_call_id=context_call_id,
        context_result=context_result)
    _log(dlog, "grok_build_followup_messages", session_id=session_id,
         user_id=user_id, tool_call_count=len(assistant_msg["tool_calls"]),
         tool_result_count=len(tool_msgs),
         has_context_result=context_result is not None)
    return [assistant_msg] + tool_msgs


def normalize_dispatch_pair(current_messages, native_turn, dlog=None,
                            session_id="", user_id=""):
    """Rewrite the last [assistant(text), user(observation)] pair the pipeline's
    context-tool dispatch appended into protocol-correct native tool messages
    for Grok.

    ``native_turn`` is a dict: ``{"calls", "results_by_id", "context_call_id",
    "assistant_text"}``. If the tail of ``current_messages`` does not match the
    expected echo/observation shape, the list is returned UNCHANGED (still valid
    for xAI, just not native) and the reason is logged — never raises.
    """
    try:
        calls = native_turn.get("calls") or []
        if not calls:
            _log(dlog, "grok_normalize_skip_no_calls",
                 session_id=session_id, user_id=user_id)
            return current_messages
        if len(current_messages) < 2:
            _log(dlog, "grok_normalize_skip_too_short",
                 session_id=session_id, user_id=user_id,
                 msg_count=len(current_messages))
            return current_messages
        last = current_messages[-1]
        prev = current_messages[-2]
        if not (isinstance(prev, dict) and prev.get("role") == "assistant"
                and isinstance(last, dict) and last.get("role") == "user"):
            _log(dlog, "grok_normalize_skip_shape_mismatch",
                 session_id=session_id, user_id=user_id,
                 prev_role=_get(prev, "role"), last_role=_get(last, "role"))
            return current_messages

        observation = last.get("content")
        assistant_text = native_turn.get("assistant_text")
        if not isinstance(assistant_text, str) or not assistant_text.strip():
            # Fall back to whatever the dispatch used as the echo.
            assistant_text = prev.get("content") if isinstance(prev.get("content"), str) else ""

        followup = build_native_followup_messages(
            assistant_text, calls, native_turn.get("results_by_id") or {},
            context_call_id=native_turn.get("context_call_id"),
            context_result=observation, dlog=dlog,
            session_id=session_id, user_id=user_id)
        rebuilt = list(current_messages[:-2]) + followup
        _log(dlog, "grok_normalize_applied", session_id=session_id,
             user_id=user_id, removed=2, added=len(followup))
        return rebuilt
    except Exception as e:  # pragma: no cover - defensive
        _log(dlog, "grok_normalize_error", session_id=session_id, user_id=user_id,
             error_type=type(e).__name__, error=str(e)[:200])
        return current_messages


# ─────────────────────────────────────────────────────────────────────────────
# 5. Grok-specific system-prompt projection (native tools, not XML tags)
# ─────────────────────────────────────────────────────────────────────────────

def build_grok_system_suffix(mode="edit", github_enabled=False,
                             history_enabled=False, dlog=None,
                             session_id="", user_id=""):
    """A short contract appended to Grok's system text telling it to USE the
    native function tools instead of the XML-tag protocol described earlier in
    the shared prompt (which targets Claude/GPT).

    We do NOT edit the shared ``NATURAL_SYSTEM`` constant (that would change the
    Claude/GPT paths); this suffix explicitly overrides the tag instructions for
    Grok only.
    """
    m = (mode or "edit").strip().lower()
    lines = [
        "\n\n━━━ NATIVE TOOL PROTOCOL (THIS MODEL) ━━━",
        "You have been given real FUNCTION TOOLS. IGNORE any instructions above "
        "about emitting XML tags such as <surgical_edit>, <new_file>, "
        "<edit_plan>, <search_request>, or <file_request> — do NOT write those "
        "tags as text. Instead, CALL the corresponding tool:",
    ]
    if m in _EDIT_MODES:
        lines += [
            "• write_surgical_edit — make a precise edit to an existing file "
            "(call once per symbol; include the COMPLETE new_code, and old_code "
            "as your anchor).",
            "• write_new_file — create a brand-new file.",
            "• write_edit_plan — for 3+ edits, emit a symbol-level plan.",
        ]
    lines += [
        "• request_file — load full file contents you need before editing.",
        "• request_search — grep the codebase for symbols/keywords.",
    ]
    if github_enabled:
        lines.append("• request_github — read from the connected GitHub repo.")
    if history_enabled:
        lines.append("• request_history — read an older version of a session file.")
    lines.append(
        "• report_blocked — ONLY if you truly cannot proceed without context "
        "you cannot obtain.")
    if m in _EDIT_MODES:
        lines += [
            "",
            "Do NOT narrate what you WOULD change — actually call "
            "write_surgical_edit / write_new_file with the real code. When your "
            "edits are complete, stop. Never describe an edit in prose instead "
            "of calling the tool.",
        ]
    else:
        lines += [
            "",
            "Use request_file / request_search to gather what you need, then "
            "answer in plain text. Do not call any write/edit tools in this mode.",
        ]
    suffix = "\n".join(lines)
    _log(dlog, "grok_system_suffix_built", session_id=session_id, user_id=user_id,
         mode=m, length=len(suffix), github_enabled=bool(github_enabled),
         history_enabled=bool(history_enabled))
    return suffix


def build_grok_agent_instruction(mode="edit", github_enabled=False,
                                 history_enabled=False, dlog=None,
                                 session_id="", user_id=""):
    """Native-tool replacement for the pipeline's XML ``_AGENT_INSTRUCTION``
    (appended to the latest user turn). Same intent, native phrasing."""
    m = (mode or "edit").strip().lower()
    parts = [
        "\n\n[AGENTIC EDIT MODE — NATIVE TOOLS]",
        "Complete this task autonomously using the FUNCTION TOOLS provided to "
        "you. Do not write XML tags as text.",
        "Gather context with request_search / request_file"
        + (" / request_github" if github_enabled else "")
        + (" / request_history" if history_enabled else "")
        + " as many times as you need.",
    ]
    if m in _EDIT_MODES:
        parts.append(
            "Produce your changes by CALLING write_surgical_edit / "
            "write_new_file / write_edit_plan — never by describing them in "
            "prose. If you already have enough context, write the edits now; "
            "never guess at code you have not seen. If you truly cannot "
            "proceed, call report_blocked. When your edits are complete, stop.")
    else:
        parts.append(
            "Answer the user's question in plain text once you have gathered "
            "enough context. Do not emit edits in this mode.")
    text = "\n".join(parts)
    _log(dlog, "grok_agent_instruction_built", session_id=session_id,
         user_id=user_id, mode=m, length=len(text))
    return text
