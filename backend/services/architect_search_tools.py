"""
architect_search_tools.py — Extends the Architect's tool-use search loop
(pipeline.py, gated by get_setting("agentic_tool_use")) with the same
call-graph / type-usage / exact-line search primitives already proven in
context_resolver.py for the Surgeon.

Adds three new tools Claude can call during the tool-use ReAct search loop:

  find_callers   — every call site of a function, across all session files
  find_usages    — every reference to a type/interface/const, across all files
  get_lines      — exact line-range slice of a known file

These do NOT reimplement search logic. Each tool builds a single-item
request dict in context_resolver's existing schema and calls the already
-tested `resolve_context_requests()` — the same function the Surgeon uses
in production. This file only adapts + formats results for the tool-use
loop's SSE progress messages and tool_result payloads.

Flag: only reachable when get_setting("agentic_tool_use") == "true"
(AGENTIC_TOOLS_V2 in pipeline.py). Default OFF. Zero effect on the legacy
ReAct loop or the single-pass path — this module is not imported by either.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Tool schemas — appended to AGENTIC_TOOLS_V2 in pipeline.py
# ─────────────────────────────────────────────────────────────────────────────

ARCHITECT_SEARCH_TOOLS_V2 = [
    {
        "name": "find_callers",
        "description": (
            "Find every place a function is CALLED across all session files. "
            "Use before changing a function's signature or behavior, to see "
            "how it's actually used by its callers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact function/method name to find callers of"
                }
            },
            "required": ["name"]
        }
    },
    {
        "name": "find_usages",
        "description": (
            "Find every place a type, interface, or const is REFERENCED across "
            "all session files. Use before changing a shared type/interface, to "
            "see every place that depends on its current shape."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact type/interface/const name to find usages of"
                }
            },
            "required": ["name"]
        }
    },
    {
        "name": "get_lines",
        "description": (
            "Fetch an exact line range from a known file. Use only when you "
            "already know the precise file and line numbers (e.g. from a QA "
            "warning or a user-pasted stack trace) — otherwise use "
            "search_codebase."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "The filename to slice (as shown in the file list)"
                },
                "start": {
                    "type": "integer",
                    "description": "First line number (1-indexed, inclusive)"
                },
                "end": {
                    "type": "integer",
                    "description": "Last line number (1-indexed, inclusive)"
                }
            },
            "required": ["filename", "start", "end"]
        }
    },
]

_TOOL_NAMES = {"find_callers", "find_usages", "get_lines"}


def is_architect_search_tool(tool_name: str) -> bool:
    """True if this tool name is one of the ones added by this module."""
    return tool_name in _TOOL_NAMES


def execute_architect_search_tool(
    tool_name: str,
    tool_input: Dict,
    symbol_maps_by_name: Dict,
    dlog: Optional[Callable] = None,
) -> str:
    """
    Execute one of find_callers / find_usages / get_lines by delegating to
    context_resolver.resolve_context_requests() — the same tested resolver
    already used by the Surgeon. Never raises; always returns a string
    (possibly a "no matches" message) so the tool-use loop can always form
    a valid tool_result block.
    """
    def _log(event, **kw):
        if dlog:
            try:
                dlog(event, **kw)
            except Exception:
                pass  # logging must never break the search loop

    _log("architect_search_tool_start", tool_name=tool_name, tool_input=tool_input)

    try:
        from services.context_resolver import resolve_context_requests
    except Exception as e:
        _log("architect_search_tool_import_error", tool_name=tool_name, error=str(e))
        return f"[search tool unavailable: {tool_name}]"

    if tool_name == "find_callers":
        name = tool_input.get("name", "")
        if not name:
            _log("architect_search_tool_missing_name", tool_name=tool_name)
            return "No function name provided for find_callers."
        requests = [{"type": "callers", "name": name}]
        empty_msg = f"No callers found for '{name}'."
    elif tool_name == "find_usages":
        name = tool_input.get("name", "")
        if not name:
            _log("architect_search_tool_missing_name", tool_name=tool_name)
            return "No type/const name provided for find_usages."
        requests = [{"type": "usages", "name": name}]
        empty_msg = f"No usages found for '{name}'."
    elif tool_name == "get_lines":
        filename = tool_input.get("filename", "")
        start = tool_input.get("start", 1)
        end = tool_input.get("end", 50)
        if not filename:
            _log("architect_search_tool_missing_filename", tool_name=tool_name)
            return "No filename provided for get_lines."
        requests = [{"type": "lines", "file": filename, "start": start, "end": end}]
        empty_msg = f"No lines resolved for {filename} L{start}-{end}. Check the filename."
    else:
        _log("architect_search_tool_unknown", tool_name=tool_name)
        return f"Unknown search tool: {tool_name}"

    try:
        result = resolve_context_requests(requests, symbol_maps_by_name)
    except Exception as e:
        _log("architect_search_tool_resolve_error", tool_name=tool_name, error=str(e))
        return f"[search failed for {tool_name}: {e}]"

    if not result:
        _log("architect_search_tool_empty", tool_name=tool_name, tool_input=tool_input)
        return empty_msg

    _log("architect_search_tool_ok", tool_name=tool_name, result_lines=result.count("\n") + 1)
    return result
