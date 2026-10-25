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

try:  # pragma: no cover
    from services.agent_tool_spec import (
        blocked_text as _blocked_text,
        edit_ladder as _edit_ladder,
        file_blurb as _file_blurb,
        history_text as _history_text,
        limits_block as _limits_block,
        new_code_description as _new_code_description,
        old_code_description as _old_code_description,
        plan_text as _plan_text,
        repo_text as _repo_text,
        scope_block as _scope_block,
        search_blurb as _search_blurb,
        symbol_description as _symbol_description,
    )
except Exception:  # pragma: no cover
    from agent_tool_spec import (  # type: ignore
        blocked_text as _blocked_text,
        edit_ladder as _edit_ladder,
        file_blurb as _file_blurb,
        history_text as _history_text,
        limits_block as _limits_block,
        new_code_description as _new_code_description,
        old_code_description as _old_code_description,
        plan_text as _plan_text,
        repo_text as _repo_text,
        scope_block as _scope_block,
        search_blurb as _search_blurb,
        symbol_description as _symbol_description,
    )


# ── Tool name constants (single source of truth; used by tests too) ──────────
TOOL_WRITE_SURGICAL_EDIT = "write_surgical_edit"
TOOL_WRITE_NEW_FILE = "write_new_file"
TOOL_WRITE_EDIT_PLAN = "write_edit_plan"
TOOL_REQUEST_FILE = "request_file"
TOOL_REQUEST_SEARCH = "request_search"
TOOL_REQUEST_GITHUB = "request_github"
TOOL_AWS_CLI = "aws_cli"
TOOL_GIT_CLI = "git_cli"
TOOL_REQUEST_HISTORY = "request_history"
TOOL_REQUEST_PREVIEW = "request_preview"
TOOL_REQUEST_WEB_SEARCH = "request_web_search"
TOOL_REQUEST_MCP = "request_mcp"
TOOL_REQUEST_REPO = "request_repo"
TOOL_REPORT_BLOCKED = "report_blocked"

WRITE_TOOLS = frozenset({
    TOOL_WRITE_SURGICAL_EDIT, TOOL_WRITE_NEW_FILE, TOOL_WRITE_EDIT_PLAN,
})
CONTEXT_TOOLS = frozenset({
    TOOL_REQUEST_FILE, TOOL_REQUEST_SEARCH, TOOL_REQUEST_GITHUB,
    TOOL_REQUEST_HISTORY, TOOL_REQUEST_PREVIEW, TOOL_REQUEST_WEB_SEARCH,
    TOOL_REQUEST_MCP, TOOL_REQUEST_REPO,
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
                "Edit one existing file. Call once per site. "
                + _edit_ladder("function", "grok")
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
                        "description": _symbol_description(),
                    },
                    "description": {
                        "type": "string",
                        "description": "What changed and why — one clear sentence.",
                    },
                    "new_code": {
                        "type": "string",
                        "description": _new_code_description(),
                    },
                    "old_code": {
                        "type": "string",
                        "description": _old_code_description(),
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
            "description": _plan_text("function"),
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
            "description": _file_blurb("function"),
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
            "description": _search_blurb("function"),
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
    # Generated from services.github_tool_spec so the enum, argument names
    # (path_prefix, provider), and the prompt cannot drift. The paging note
    # stays here because a truncated read_file is useless unless the model
    # passes start_line. pipeline.py still auto-continues Grok when it doesn't.
    from services.github_tool_spec import grok_function_schema
    return grok_function_schema(TOOL_REQUEST_GITHUB)


def _schema_request_history():
    return {
        "type": "function",
        "function": {
            "name": TOOL_REQUEST_HISTORY,
            "description": _history_text("function"),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "File to look up."},
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional keyword to search across saved versions. "
                            "Each hit names its version number."
                        ),
                    },
                    "view": {
                        "type": "string",
                        "enum": ["list", "diff"],
                        "description": (
                            "list numbers the saved versions. "
                            "diff compares a version with the current file. "
                            "Omit to show one version's content."
                        ),
                    },
                    "version": {
                        "type": "string",
                        "description": (
                            "A version number, or first, or last. "
                            "last is the file right before the most recent edit. "
                            "first is the file before any edit in this chat."
                        ),
                    },
                    "compare_to": {
                        "type": "integer",
                        "description": (
                            "With view diff, the other version number. "
                            "Omit to compare with the current file."
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
            "description": _blocked_text("function"),
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


def _schema_git_cli():
    return {
        "type": "function",
        "function": {
            "name": TOOL_GIT_CLI,
            "description": (
                "Run one git or gh command on the open folder. Follow the git rules in the system prompt.\n"
                "command starts with git or gh."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Full command, starting with git or gh.",
                    },
                },
                "required": ["command"],
            },
        },
    }


def _schema_aws_cli():
    return {
        "type": "function",
        "function": {
            "name": TOOL_AWS_CLI,
            "description": (
                "Run one aws CLI command on the connected account. Follow the AWS rules in the system prompt.\n"
                "command starts with aws. intent is on_task or side, and only matters for reads."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Full command, starting with aws.",
                    },
                    "intent": {
                        "type": "string",
                        "enum": ["on_task", "side"],
                        "description": (
                            "on_task if this read is the next step for the user's question. "
                            "side if it is an extra read. Ignored for changes, deletes, and secret reads."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    }


def _schema_request_preview():
    return {
        "type": "function",
        "function": {
            "name": TOOL_REQUEST_PREVIEW,
            "description": (
                "Render a pending .tsx or .jsx edit in the user's live preview "
                "and return compile or runtime errors. Call this after you edit, "
                "not before. unobserved means the preview produced no evidence, "
                "not that the edit passed. Do not call it for .html files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Component filenames to render. Omit to render the "
                            "component you just edited."
                        ),
                    },
                },
                "required": [],
            },
        },
    }


def _schema_request_mcp(user_id=""):
    catalog = ""
    if user_id:
        try:
            from services.mcp_client import prompt_catalog
            catalog = prompt_catalog(user_id, native=True)
        except Exception:
            catalog = ""
    description = (
        "Call a tool on a connected MCP server. The user approves the call "
        "unless that tool is set to always allow. Pass the server id, the "
        "tool name, and arguments."
    )
    if catalog:
        description = description + "\n" + catalog
    return {
        "type": "function",
        "function": {
            "name": TOOL_REQUEST_MCP,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "Connected server id."},
                    "tool": {"type": "string", "description": "Tool name on that server."},
                    "arguments": {
                        "type": "object",
                        "description": "Arguments for the tool.",
                    },
                },
                "required": ["server", "tool"],
            },
        },
    }


def _schema_request_web_search():
    return {
        "type": "function",
        "function": {
            "name": TOOL_REQUEST_WEB_SEARCH,
            "description": (
                "Searches the public internet for current information. "
                "The query is sent to a search engine: do not include code, "
                "file names, or secrets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question to look up. No code or secrets.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this search is needed.",
                    },
                },
                "required": ["query"],
            },
        },
    }


def _schema_request_repo():
    return {
        "type": "function",
        "function": {
            "name": TOOL_REQUEST_REPO,
            "description": _repo_text("function"),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": [
                            "list_repo_files", "read_repo_file", "search_repo",
                            "git_status", "git_diff",
                        ],
                    },
                    "path": {"type": "string", "description": "Repo-relative path."},
                    "terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Search strings for search_repo.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["unstaged", "staged", "worktree", "range"],
                        "description": "git_diff only. Omit to compare local HEAD against the repository.",
                    },
                    "view": {
                        "type": "string",
                        "enum": ["stat", "patch"],
                        "description": "git_diff only. stat is the default. patch is the unified diff.",
                    },
                    "base": {"type": "string", "description": "git_diff range only. The start ref."},
                    "head": {"type": "string", "description": "git_diff range only. The end ref. Defaults to HEAD."},
                    "dots": {
                        "type": "string",
                        "enum": ["two", "three"],
                        "description": "git_diff range only. three (the default) is since the two refs diverged.",
                    },
                },
                "required": ["tool"],
            },
        },
    }


def build_grok_agent_tools(mode="edit", github_enabled=False, history_enabled=False,
                           aws_enabled=False, preview_enabled=False,
                           websearch_enabled=False, mcp_enabled=False,
                           repo_enabled=False, git_enabled=False, dlog=None,
                           session_id="", user_id=""):
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
    if aws_enabled:
        tools.append(_schema_aws_cli())
    if git_enabled:
        tools.append(_schema_git_cli())
    if preview_enabled and m in _EDIT_MODES:
        tools.append(_schema_request_preview())
    if websearch_enabled:
        tools.append(_schema_request_web_search())
    if mcp_enabled:
        tools.append(_schema_request_mcp(user_id))
    if repo_enabled:
        tools.append(_schema_request_repo())

    # report_blocked is meaningful in every mode as a clean terminal signal.
    tools.append(_schema_report_blocked())

    _log(dlog, "grok_build_tools_done", session_id=session_id, user_id=user_id,
         mode=m, tool_count=len(tools),
         tool_names=[t["function"]["name"] for t in tools])
    return tools


# ─────────────────────────────────────────────────────────────────────────────
# 2. Progress helpers (Agent / Ask / Plan real-time UX)
# ─────────────────────────────────────────────────────────────────────────────

#: Human progress lines when a native tool name first appears on the stream.
TOOL_PROGRESS_LABELS = {
    TOOL_WRITE_SURGICAL_EDIT: "Calling write_surgical_edit…",
    TOOL_WRITE_NEW_FILE: "Calling write_new_file…",
    TOOL_WRITE_EDIT_PLAN: "Calling write_edit_plan…",
    TOOL_REQUEST_FILE: "Calling request_file…",
    TOOL_REQUEST_SEARCH: "Calling request_search…",
    TOOL_REQUEST_GITHUB: "Calling request_github…",
    TOOL_REQUEST_HISTORY: "Calling request_history…",
    TOOL_REQUEST_PREVIEW: "Checking the preview…",
    TOOL_REQUEST_WEB_SEARCH: "Searching the web…",
    TOOL_REQUEST_MCP: "Calling MCP…",
    TOOL_REQUEST_REPO: "Reading the repository…",
    TOOL_GIT_CLI: "Running git…",
    TOOL_REPORT_BLOCKED: "Reporting blocked…",
}


def format_tool_call_progress(name: str) -> str:
    """One-line progress string for a newly named tool call."""
    n = (name or "").strip()
    if not n:
        return "Calling tool…"
    if n in TOOL_PROGRESS_LABELS:
        return TOOL_PROGRESS_LABELS[n]
    return f"Calling {n.replace('_', ' ')}…"


def extract_reasoning_delta(delta) -> str:
    """Best-effort summarized reasoning text from a chat.completions delta.

    xAI documents ``reasoning_content`` on the native SDK / Responses API.
    Chat Completions may still surface it as an extra field — never raises.
    """
    if delta is None:
        return ""
    keys = ("reasoning_content", "reasoning")
    try:
        if isinstance(delta, dict):
            for k in keys:
                v = delta.get(k)
                if isinstance(v, str) and v:
                    return v
        else:
            for k in keys:
                v = getattr(delta, k, None)
                if isinstance(v, str) and v:
                    return v
            extra = getattr(delta, "model_extra", None) or getattr(
                delta, "__pydantic_extra__", None)
            if isinstance(extra, dict):
                for k in keys:
                    v = extra.get(k)
                    if isinstance(v, str) and v:
                        return v
    except Exception:
        return ""
    return ""


def summarize_translation_progress(tr) -> str:
    """Short progress line after ``translate_tool_calls`` (empty if nothing)."""
    if tr is None:
        return ""
    parts = []
    try:
        n_edits = len(getattr(tr, "edit_json_strings", None) or [])
        n_new = len(getattr(tr, "new_file_json_strings", None) or [])
        if n_edits:
            parts.append(f"{n_edits} edit{'s' if n_edits != 1 else ''}")
        if n_new:
            parts.append(f"{n_new} new file{'s' if n_new != 1 else ''}")
        if getattr(tr, "edit_plan", None) is not None:
            n_plan = len(tr.edit_plan) if isinstance(tr.edit_plan, list) else 1
            parts.append(f"plan ({n_plan} item{'s' if n_plan != 1 else ''})")
        ctx = getattr(tr, "context_request", None)
        if ctx is not None:
            kind = ctx[0] if isinstance(ctx, (tuple, list)) and ctx else "context"
            parts.append(f"fetching {kind}")
        if getattr(tr, "blocked_reason", None):
            parts.append("blocked")
    except Exception:
        return ""
    if not parts:
        return ""
    return "Next: " + ", ".join(parts) + "…"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Streamed tool-call delta accumulator
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

    ``add_delta`` returns the list of tool names that became known for the
    *first* time in this call (for real-time progress SSE). Empty list when
    nothing new was named. Existing callers that ignore the return value are
    unchanged.
    """

    def __init__(self, dlog=None, session_id="", user_id=""):
        self._dlog = dlog
        self._session_id = session_id
        self._user_id = user_id
        self._by_index = {}
        self._order = []

    def add_delta(self, tool_calls_delta):
        newly_named = []
        if not tool_calls_delta:
            return newly_named
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
                        if not entry["name"]:
                            newly_named.append(name)
                        entry["name"] = name
                    args = _get(fn, "arguments")
                    if args:
                        entry["arguments"] += args
            except Exception as e:  # pragma: no cover - defensive
                _log(self._dlog, "grok_tc_accumulate_error",
                     session_id=self._session_id, user_id=self._user_id,
                     error_type=type(e).__name__, error=str(e)[:200])
        return newly_named

    def peek(self):
        """``[(index, name, arguments_so_far)]`` in arrival order.

        Read-only. ``name`` is None until the first fragment that carries one.
        Never raises. Safe to call before ``finalize``.
        """
        try:
            out = []
            for idx in self._order:
                entry = self._by_index.get(idx) or {}
                out.append((idx, entry.get("name"), entry.get("arguments") or ""))
            return out
        except Exception as exc:  # pragma: no cover - defensive
            _log(self._dlog, "grok_tc_peek_error",
                 session_id=self._session_id, user_id=self._user_id,
                 error_type=type(exc).__name__, error=str(exc)[:200])
            return []

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
# 4. Translator: completed tool calls -> pipeline producer shapes
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
            # ── No-op detection at the earliest possible point ──
            # Root-caused via session ecce169c (2026-08-06): Grok can emit a
            # tool call that LOOKS like a real edit (non-empty old_code/new_code,
            # first-pass QA scores it 9-10) but is actually old_code==new_code
            # verbatim — a literal no-op. The second-pass CoVe verifier catches
            # this downstream and caps the score, but by then we've lost the
            # chance to see WHAT Grok actually sent (only lengths were logged).
            # Log truncated previews + an explicit identical flag right here,
            # at the moment the model's raw tool-call args are parsed, so a
            # no-op can be confirmed/diagnosed without guessing.
            _oc = str(args.get("old_code") or "")
            _nc = str(args.get("new_code") or "")
            _log(dlog, "grok_translate_surgical_edit", session_id=session_id,
                 user_id=user_id, tool_call_id=cid,
                 filename=str(args.get("filename"))[:120],
                 symbol=str(args.get("symbol"))[:120],
                 new_code_len=len(_nc),
                 has_old_code=_nonempty_str(_oc),
                 has_lines=("edit_start_line" in args and "edit_end_line" in args),
                 old_code_preview=_oc[:400],
                 new_code_preview=_nc[:400],
                 old_new_identical=(bool(_oc) and _oc == _nc))

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

        elif name == TOOL_GIT_CLI:
            # Do not run yet. resolve_git_call decides auto-run, refusal, or a card.
            _git_cmd = str(args.get("command") or "").strip()
            if res.context_request is None:
                res.context_request = ("git", _git_cmd)
                res.context_call_id = cid
            else:
                fb = "Only one git command is handled per turn."
                res.errors.append((cid, fb))
                res.results_by_id[cid] = fb
            _log(dlog, "grok_translate_git_cli", session_id=session_id,
                 user_id=user_id, tool_call_id=cid, command=_git_cmd[:180])

        elif name == TOOL_AWS_CLI:
            # Do not run yet. resolve_aws_call decides auto-run, refusal, or a card.
            _aws_cmd = str(args.get("command") or "").strip()
            _aws_intent = str(args.get("intent") or "").strip()
            if _aws_intent not in ("on_task", "side"):
                _aws_intent = ""
            _aws_body = f"intent: {_aws_intent}\n{_aws_cmd}" if _aws_intent else _aws_cmd
            if res.context_request is None:
                res.context_request = ("aws", _aws_body)
                res.context_call_id = cid
            else:
                fb = "Only one AWS command is sent for approval per turn."
                res.errors.append((cid, fb))
                res.results_by_id[cid] = fb
            _log(dlog, "grok_translate_aws_cli", session_id=session_id,
                 user_id=user_id, tool_call_id=cid,
                 command=_aws_cmd[:180], intent=_aws_intent)

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
        # Only when the model sent them, so a filename-only call keeps its
        # old payload exactly.
        view = args.get("view")
        if isinstance(view, str) and view.strip():
            payload["view"] = view.strip().lower()
        version = args.get("version")
        if isinstance(version, bool):
            version = None
        elif isinstance(version, int):
            payload["version"] = str(version)
        elif isinstance(version, str) and version.strip():
            payload["version"] = version.strip()[:40]
        compare = args.get("compare_to")
        if isinstance(compare, str) and compare.strip().isdigit():
            compare = int(compare.strip())
        if isinstance(compare, int) and not isinstance(compare, bool) and compare > 0:
            payload["compare_to"] = compare
        return "history", _json.dumps(payload)
    if name == TOOL_REQUEST_PREVIEW:
        files = args.get("files") or []
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, list):
            files = []
        return "preview", _json.dumps({"files": files})
    if name == TOOL_REQUEST_WEB_SEARCH:
        payload = {"query": args.get("query", "")}
        if args.get("reason"):
            payload["reason"] = args.get("reason")
        return "websearch", _json.dumps(payload)
    if name == TOOL_REQUEST_MCP:
        args_obj = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}
        payload = {
            "server": args.get("server", ""),
            "tool": args.get("tool", ""),
            "arguments": args_obj,
        }
        return "mcp", _json.dumps(payload)
    if name == TOOL_REQUEST_REPO:
        payload = {
            "tool": args.get("tool", ""),
            "path": args.get("path", "") or "",
            "terms": args.get("terms") or [],
        }
        # Only git_diff reads these, and only when the model sent them, so a
        # call to the other repo tools keeps its old payload exactly.
        for field in ("scope", "base", "head", "view", "dots"):
            sent = args.get(field)
            if isinstance(sent, str) and sent.strip():
                payload[field] = sent.strip()
        return "repo", _json.dumps(payload)
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
                             history_enabled=False, aws_enabled=False,
                             dlog=None, session_id="", user_id="",
                             wants_edit=True, preview_enabled=False,
                             preflight_enabled=False, websearch_enabled=False,
                             mcp_enabled=False, repo_enabled=False,
                             git_enabled=False):
    """Function-tool contract for Grok. No XML tag names, and no instruction to ignore them.

    The main system text Grok receives is the tag-free head plus the code-quality
    rules. This suffix is the one place Grok reads how to edit.
    """
    m = (mode or "edit").strip().lower()
    lines = [
        "\n\n━━━ NATIVE TOOL PROTOCOL (THIS MODEL) ━━━",
        "You have real FUNCTION TOOLS. Call them. Do not write XML tags.",
    ]
    scope = _scope_block(
        session_files=True, repo=bool(repo_enabled), github=bool(github_enabled),
        web=bool(websearch_enabled), git=bool(git_enabled), form="function",
    )
    if scope:
        lines.append(scope)
    lines.append(_limits_block("function"))
    if m in _EDIT_MODES and wants_edit:
        lines.append(_edit_ladder("function", "grok"))
    elif m in _EDIT_MODES:
        lines.append(
            "• write_surgical_edit — edit an existing file. "
            "• write_new_file — create a brand-new file. "
            "• write_edit_plan — only for 3 or more distinct files or symbols."
        )
    lines += [
        "• request_file — load a file already in this chat when you know its name.",
        "• request_search — grep files already in this chat.",
    ]
    if m in _EDIT_MODES and wants_edit:
        from services.mega_symbol_tool_routing import (  # local import: avoid cycles
            prompt_mega_symbol_tool_routing_rules as _mega_rules,
        )
        lines += ["", _mega_rules(native_tools=True)]
    if github_enabled:
        lines.append("• request_github — read from the connected GitHub repo.")
    if repo_enabled:
        lines.append(_repo_text("function"))
    if history_enabled:
        lines.append(_history_text("function"))
    if aws_enabled:
        from services.aws_cli import aws_agent_guidance
        lines.append("• aws_cli — " + aws_agent_guidance())
    if git_enabled:
        from services.agent_tool_spec import git_cli_text
        lines.append("• git_cli — " + git_cli_text("function"))
    if preview_enabled and m in _EDIT_MODES:
        lines.append(
            "• request_preview — render a pending .tsx/.jsx edit and read "
            "preview errors. unobserved is not a pass."
        )
    if websearch_enabled:
        lines.append(
            "• request_web_search — searches the public internet for current "
            "information. The query is sent to a search engine: do not include "
            "code, file names, or secrets."
        )
    if mcp_enabled:
        lines.append(
            "• request_mcp — call a tool on a connected MCP server. The user "
            "approves each call unless that tool is set to always allow."
        )
    if preflight_enabled and m in _EDIT_MODES and wants_edit:
        lines.append(
            "A preflight check may reject an edit. If a tool result starts "
            "with [PREFLIGHT], that edit was NOT applied. Fix it. Do not "
            "resend the same edit."
        )
    lines.append("• " + _blocked_text("function"))
    if m in _EDIT_MODES and wants_edit:
        lines += [
            "",
            "Do NOT narrate what you WOULD change — actually call "
            "write_surgical_edit / write_new_file with the real code. When your "
            "edits are complete, stop. Never describe an edit in prose instead "
            "of calling the tool.",
        ]
    elif m in _EDIT_MODES:
        lines += [
            "",
            "Answer in plain text. Call write_surgical_edit, write_new_file, "
            "or write_edit_plan only if the user asked you to change code. "
            "Do not mention your mode, and do not say that no code changes "
            "were produced.",
        ]
    else:
        lines += [
            "",
            "Use request_file / request_search to gather what you need, then "
            "answer in plain text. Do not call any write/edit tools in this mode.",
        ]
    if github_enabled or repo_enabled:
        from services.github_natural_tag import LOOKUP_BEFORE_ANSWER
        lines.append(LOOKUP_BEFORE_ANSWER)
    suffix = "\n".join(lines)
    _log(dlog, "grok_system_suffix_built", session_id=session_id, user_id=user_id,
         mode=m, length=len(suffix), github_enabled=bool(github_enabled),
         history_enabled=bool(history_enabled))
    return suffix


def build_grok_agent_instruction(mode="edit", github_enabled=False,
                                 history_enabled=False, dlog=None,
                                 session_id="", user_id="",
                                 wants_edit=True, preview_enabled=False,
                                 preflight_enabled=False, websearch_enabled=False,
                                 mcp_enabled=False, repo_enabled=False):
    """Native-tool replacement for the pipeline's XML turn suffix.

    ``wants_edit`` False is a plain question on an Edit/Agent turn: same
    context tools, but the model is told to answer instead of being ordered
    to write code.
    """
    m = (mode or "edit").strip().lower()
    answering = m in _EDIT_MODES and not wants_edit
    if answering:
        parts = [
            "\n\nAnswer the user's message directly using the FUNCTION TOOLS "
            "when you need to see code. Do not write XML tags as text.",
            "Use request_file when you know the target file; use request_search"
            + (" / request_github" if github_enabled else "")
            + (" / request_history" if history_enabled else "")
            + (" / request_repo" if repo_enabled else "")
            + " only if the answer depends on code you have not seen.",
            "Answer in plain text. Call write_surgical_edit, write_new_file, "
            "or write_edit_plan only if the user asked you to change code. "
            "Do not mention your mode. If you truly cannot proceed, call "
            "report_blocked. When you have answered, stop.",
        ]
    else:
        parts = [
            "\n\n[AGENTIC EDIT MODE — NATIVE TOOLS]",
            "Complete this task autonomously using the FUNCTION TOOLS provided to "
            "you. Do not write XML tags as text.",
            "Gather context with request_file first when you know the target file; "
            "use request_search"
            + (" / request_github" if github_enabled else "")
            + (" / request_history" if history_enabled else "")
            + (" / request_repo" if repo_enabled else "")
            + " only to find unknowns.",
        ]
        if m in _EDIT_MODES:
            from services.mega_symbol_tool_routing import (
                prompt_mega_symbol_tool_routing_rules as _mega_rules,
            )
            parts.append(_mega_rules(native_tools=True))
            parts.append(
                "Produce your changes by CALLING write_surgical_edit / "
                "write_new_file (or write_edit_plan only for distinct "
                "files/symbols) — never by describing them in prose. If you "
                "already have enough context, write the edits now; never guess "
                "at code you have not seen. If you truly cannot proceed, call "
                "report_blocked. When your edits are complete, stop.")
            if preview_enabled:
                parts.append(
                    "After editing a .tsx or .jsx component, you may call "
                    "request_preview. unobserved is not a pass."
                )
            if preflight_enabled:
                parts.append(
                    "A preflight check may reject an edit. If a tool result "
                    "starts with [PREFLIGHT], that edit was NOT applied. Fix "
                    "it and send a corrected edit. Do not resend the same edit."
                )
        else:
            parts.append(
                "Answer the user's question in plain text once you have gathered "
                "enough context. Do not emit edits in this mode.")
    if websearch_enabled:
        parts.append(
            "Call request_web_search to search the public internet for current "
            "information. The query is sent to a search engine: do not include "
            "code, file names, or secrets."
        )
    if mcp_enabled:
        parts.append(
            "Call request_mcp to use a connected MCP server. The user approves "
            "each call unless that tool is set to always allow."
        )
    text = "\n".join(parts)
    _log(dlog, "grok_agent_instruction_built", session_id=session_id,
         user_id=user_id, mode=m, length=len(text), wants_edit=bool(wants_edit))
    return text
