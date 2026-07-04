"""
github_context_tools.py — GitHub App read-side context tools for the Architect
tool-use loop (new, additive, flag-gated).

Lets a user just talk to Claude naturally ("check the open PRs on my repo",
"read the comments on issue #42", "diff main against my feature branch") once
they've connected via the GitHub App. Mirrors the exact pattern already
proven by architect_search_tools.py: this module defines tool schemas that
get appended to AGENTIC_TOOLS_V2, plus a dispatcher the tool-use loop calls.

Read-only by design — every function here only ever reads. Tier enforcement
still applies (even read_only tier is fine for these; they never write).

Flag: GITHUB_CONTEXT_TOOLS_V2 is only appended in pipeline.py when both
(a) get_setting("agentic_tool_use") == "true" (existing flag, default OFF)
and (b) get_setting("github_context_tools_enabled") == "true" (new flag,
default OFF). Zero effect on the legacy ReAct loop, single-pass path,
Surgeon, QA, or correction handler — this module is not imported by any
of them.
"""
from typing import Callable, Dict, Optional

from database import list_github_app_installations, _dlog as _db_dlog
from services.github_app_auth import get_installation_client, is_github_app_configured


GITHUB_CONTEXT_TOOLS_V2 = [
    {
        "name": "list_prs",
        "description": "List open pull requests for a connected GitHub repo. Use when the user asks about open PRs, PR status, or 'what's in review'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repo owner (user or org)"},
                "repo": {"type": "string", "description": "Repo name"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Defaults to open"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "get_pr_diff",
        "description": "Get the file diff for a specific pull request number. Use when the user asks what changed in a PR.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["owner", "repo", "pr_number"],
        },
    },
    {
        "name": "get_pr_comments",
        "description": "Get review comments and discussion on a specific pull request. Use when the user asks to read PR feedback or comments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["owner", "repo", "pr_number"],
        },
    },
    {
        "name": "list_issues",
        "description": "List open issues for a connected GitHub repo. Use when the user asks about open issues or bug reports.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Defaults to open"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "get_issue_comments",
        "description": "Get comments on a specific issue number. Use when the user asks to read the discussion on an issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
            },
            "required": ["owner", "repo", "issue_number"],
        },
    },
    {
        "name": "diff_branches",
        "description": "Compare two branches and summarize what changed. Use when the user asks to diff branches or 'what's different between main and X'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "base": {"type": "string", "description": "Base branch, e.g. main"},
                "head": {"type": "string", "description": "Head branch to compare against base"},
            },
            "required": ["owner", "repo", "base", "head"],
        },
    },
]

_TOOL_NAMES = {"list_prs", "get_pr_diff", "get_pr_comments", "list_issues", "get_issue_comments", "diff_branches"}


def is_github_context_tool(tool_name: str) -> bool:
    return tool_name in _TOOL_NAMES


def _find_client_for_repo(user_id: str, owner: str, repo: str, dlog: Callable):
    """Find whichever of this user's installations grants access to owner/repo.
    Returns (client, None) on success or (None, error_message) on failure —
    never raises, so the tool-use loop can always form a valid tool_result."""
    if not is_github_context_tool.__module__:  # no-op guard, keeps linters quiet
        pass
    installations = list_github_app_installations(user_id)
    if not installations:
        dlog("github_context_tool_no_installations", user_id=user_id)
        return None, "GitHub App is not connected for this user. Connect it in Settings first."
    for inst in installations:
        installation_id = inst["installation_id"]
        try:
            g = get_installation_client(installation_id)
            g.get_repo(f"{owner}/{repo}")  # cheap access check
            return g, None
        except Exception:
            continue
    dlog("github_context_tool_repo_not_accessible", user_id=user_id, owner=owner, repo=repo)
    return None, f"{owner}/{repo} is not accessible via any of your connected GitHub App installations."


def execute_github_context_tool(
    tool_name: str,
    tool_input: Dict,
    user_id: str,
    dlog: Optional[Callable] = None,
) -> str:
    """Execute one of the GitHub context tools. Never raises — always
    returns a string so the tool-use loop can always form a valid
    tool_result block, even on total failure."""

    def _log(event, **kw):
        if dlog:
            try:
                dlog(event, **kw)
            except Exception:
                pass
        try:
            _db_dlog(event, **kw)
        except Exception:
            pass

    _log("github_context_tool_start", tool_name=tool_name, tool_input=tool_input, user_id=user_id)

    if not is_github_context_tool(tool_name):
        _log("github_context_tool_unknown", tool_name=tool_name)
        return f"Unknown GitHub context tool: {tool_name}"

    if not is_github_app_configured():
        _log("github_context_tool_app_not_configured", tool_name=tool_name)
        return "GitHub App is not configured on this server yet."

    owner = tool_input.get("owner", "")
    repo = tool_input.get("repo", "")
    if not owner or not repo:
        _log("github_context_tool_missing_owner_repo", tool_name=tool_name)
        return "Missing owner/repo for this GitHub tool call."

    g, err = _find_client_for_repo(user_id, owner, repo, _log)
    if err:
        return err

    try:
        r = g.get_repo(f"{owner}/{repo}")

        if tool_name == "list_prs":
            state = tool_input.get("state", "open")
            prs = r.get_pulls(state=state)
            lines = []
            for pr in prs[:30]:
                lines.append(f"#{pr.number} [{pr.state}] {pr.title} (by {pr.user.login}, base={pr.base.ref} <- head={pr.head.ref})")
            result = "\n".join(lines) if lines else f"No {state} pull requests found."

        elif tool_name == "get_pr_diff":
            pr_number = tool_input.get("pr_number")
            pr = r.get_pull(int(pr_number))
            files = pr.get_files()
            lines = [f"PR #{pr_number}: {pr.title}", ""]
            for f in files[:50]:
                lines.append(f"--- {f.filename} (+{f.additions}/-{f.deletions}) ---")
                if f.patch:
                    lines.append(f.patch[:4000])
            result = "\n".join(lines)

        elif tool_name == "get_pr_comments":
            pr_number = tool_input.get("pr_number")
            pr = r.get_pull(int(pr_number))
            lines = []
            for c in pr.get_issue_comments():
                lines.append(f"{c.user.login}: {c.body}")
            for c in pr.get_review_comments():
                lines.append(f"{c.user.login} (on {c.path}:{c.line}): {c.body}")
            result = "\n\n".join(lines) if lines else f"No comments on PR #{pr_number}."

        elif tool_name == "list_issues":
            state = tool_input.get("state", "open")
            issues = r.get_issues(state=state)
            lines = []
            for issue in issues[:30]:
                if issue.pull_request:  # GitHub API returns PRs as issues too — skip
                    continue
                lines.append(f"#{issue.number} [{issue.state}] {issue.title} (by {issue.user.login})")
            result = "\n".join(lines) if lines else f"No {state} issues found."

        elif tool_name == "get_issue_comments":
            issue_number = tool_input.get("issue_number")
            issue = r.get_issue(int(issue_number))
            lines = [f"{c.user.login}: {c.body}" for c in issue.get_comments()]
            result = "\n\n".join(lines) if lines else f"No comments on issue #{issue_number}."

        elif tool_name == "diff_branches":
            base = tool_input.get("base", "")
            head = tool_input.get("head", "")
            comparison = r.compare(base, head)
            lines = [f"Comparing {base}...{head}: {comparison.ahead_by} ahead, {comparison.behind_by} behind", ""]
            for f in comparison.files[:50]:
                lines.append(f"--- {f.filename} (+{f.additions}/-{f.deletions}) ---")
                if f.patch:
                    lines.append(f.patch[:4000])
            result = "\n".join(lines)

        else:
            result = f"Unhandled GitHub tool: {tool_name}"

        _log("github_context_tool_ok", tool_name=tool_name, result_len=len(result))
        return result[:16000]

    except Exception as e:
        _log("github_context_tool_error", tool_name=tool_name, error=str(e))
        return f"[GitHub tool failed for {tool_name}: {e}]"
