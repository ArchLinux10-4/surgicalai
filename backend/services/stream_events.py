"""Stream event names shared with frontend/src/api/streamEvents.ts.

The parity test reads both lists. A name added on only one side fails it.
"""
import os
import re

EVENT_TYPES = frozenset({
    "aws_approval_needed",
    "aws_cli",
    "aws_done",
    "chat",
    "checkpoint",
    "compacting",
    "compacting_done",
    "credit_paused",
    "done",
    "edit_end",
    "edit_live",
    "edit_start",
    "error",
    "file_needed",
    "file_needed_cleared",
    "git_approval_needed",
    "git_cli",
    "git_done",
    "git_result",
    "git_step",
    "local_approval_needed",
    "local_cli",
    "local_done",
    "mcp_approval_needed",
    "mcp_call",
    "mcp_done",
    "phase",
    "plan_coverage",
    "plan_failed",
    "plan_locked",
    "plan_missing",
    "plan_questions",
    "plan_ready",
    "plan_step",
    "plan_unchanged",
    "plan_updated",
    "planning_started",
    "preview_check_needed",
    "progress",
    "qa_note",
    "qa_done",
    "result",
    "smart_result",
    "steer_applied",
    "steer_closed",
    "steer_ready",
    "steer_unapplied",
    "task_blocked",
    "task_cancelled",
    "task_done",
    "task_plan",
    "task_progress",
    "task_skipped",
    "task_start",
    "task_thinking",
    "tasks_complete",
    "thinking",
    "thinking_end",
    "thinking_start",
    "token",
    "tool_activity",
    "web_search_query",
    "web_search_results",
    "web_search_start",
})

_ANALYZE = re.compile(
    r"reading \d+ file|parsing file|found \d+ symbol|looking up|loaded:"
    r"|claude is analyzing|searching codebase|^loading:|🐙 github:|checking history:",
    re.IGNORECASE,
)
_CODE = re.compile(r"preparing code|narrowing:|writing code changes|✍️", re.IGNORECASE)
_QA = re.compile(r"running qa|QA\s*[✅⚠️🚫⏭]", re.IGNORECASE)


def phase_for_progress(content: str) -> str | None:
    """The pill a progress line should advance, or None."""
    text = content or ""
    if _QA.search(text):
        return "qa"
    if _CODE.search(text):
        return "code"
    if _ANALYZE.search(text):
        return "analyze"
    return None


def check(obj) -> None:
    """Reject an unknown event type when SURGICAL_DEBUG is set.

    Production streams are unchanged. A typo fails the debug run instead of
    disappearing in the client's empty catch.
    """
    flag = os.environ.get("SURGICAL_DEBUG", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return
    if not isinstance(obj, dict):
        raise ValueError("stream event must be an object")
    kind = obj.get("type")
    if kind not in EVENT_TYPES:
        raise ValueError(f"unknown stream event type: {kind!r}")
