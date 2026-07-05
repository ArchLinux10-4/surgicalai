"""
github_natural_tag.py — <github_request> tag support for the NATURAL chat
pipeline (new, additive, flag-gated).

The natural pipeline (run_natural_pipeline_stream) talks to Claude with XML
tags (<search_request>, <file_request>, ...) — it has no tool_use. This
module adds one more tag to that proven loop so a user can ask questions
like "what's in my last PR?" in plain chat and Claude can actually go look.

Claude emits:

    <github_request>
    {"tool": "list_prs", "args": {"owner": "someuser", "repo": "somerepo"}, "reason": "user asked about open PRs"}
    </github_request>

and the pipeline feeds the result back as a user turn — exactly like the
existing search loop.

Execution delegates to services.github_context_tools.execute_github_context_tool
(already built, tested, permission-checked). One extra read-only tool,
list_repos, lives here so Claude can discover which repos the user's
installation can reach without the user having to name them.

Gating (checked per-request in the pipeline, never at import time):
  (a) get_setting("github_context_tools_enabled") == "true"   AND
  (b) the GitHub App is configured on this server              AND
  (c) THIS user has at least one linked installation.
If any check fails, the tag is never registered and the prompt section is
never added — the natural pipeline is byte-identical to today.

Every function here degrades gracefully: no exception ever escapes into
the streaming loop.
"""
import json
from typing import Callable, Optional


# Tools Claude may request via the tag. All read-only.
_NATURAL_GH_TOOLS = (
    "list_repos", "list_prs", "get_pr_diff", "get_pr_comments",
    "list_issues", "get_issue_comments", "diff_branches",
)

GH_TAG_OPEN = "<github_request>"
GH_TAG_CLOSE = "</github_request>"


def _safe_dlog(dlog: Optional[Callable], event: str, **kw) -> None:
    if dlog is None:
        return
    try:
        dlog(event, **kw)
    except Exception:
        pass


def natural_github_availability(user_id: str, dlog: Optional[Callable] = None):
    """Return (enabled: bool, installations: list). Never raises.

    enabled is True only when the flag is on, the GitHub App is configured,
    and this user has at least one linked installation."""
    try:
        from database import get_setting, list_github_app_installations
        from services.github_app_auth import is_github_app_configured

        flag = get_setting("github_context_tools_enabled", "false").lower() == "true"
        if not flag:
            _safe_dlog(dlog, "natural_github_disabled_flag", user_id=user_id)
            return False, []

        if not is_github_app_configured():
            _safe_dlog(dlog, "natural_github_app_not_configured", user_id=user_id)
            return False, []

        installations = list_github_app_installations(user_id)
        if not installations:
            _safe_dlog(dlog, "natural_github_no_installations", user_id=user_id)
            return False, []

        _safe_dlog(dlog, "natural_github_enabled",
                   user_id=user_id, installations=len(installations),
                   accounts=[i.get("account_login") for i in installations])
        return True, installations

    except Exception as e:
        _safe_dlog(dlog, "natural_github_availability_error", user_id=user_id, error=str(e))
        return False, []


def build_github_prompt_section(installations: list) -> str:
    """System-prompt text telling Claude the tag exists and how to use it.
    Only ever called when natural_github_availability returned True."""
    accounts = ", ".join(
        str(i.get("account_login", "?")) for i in installations
    ) or "?"
    return f"""
━━━ GITHUB ACCESS (LIVE) ━━━
This user has connected their GitHub account ({accounts}) via the GitHub App.
You HAVE live read access to their repositories. When the user asks about
their pull requests, issues, branches, diffs, or repos, DO NOT say you lack
access — fetch the data with a github_request tag:

<github_request>
{{"tool": "list_prs", "args": {{"owner": "{accounts.split(',')[0].strip()}", "repo": "REPO_NAME"}}, "reason": "why you need this"}}
</github_request>

Available tools (all read-only):
- list_repos    args: {{}}                                    → repos the connection can reach (use FIRST if you don't know the repo name)
- list_prs      args: {{owner, repo, state?}}                 → open PRs ("state": "open"|"closed"|"all")
- get_pr_diff   args: {{owner, repo, pr_number}}              → file-by-file diff of one PR
- get_pr_comments args: {{owner, repo, pr_number}}            → review comments + discussion
- list_issues   args: {{owner, repo, state?}}                 → issues
- get_issue_comments args: {{owner, repo, issue_number}}      → issue discussion
- diff_branches args: {{owner, repo, base, head}}             → compare two branches

Rules:
- The tag body must be a single valid JSON object: {{"tool": ..., "args": {{...}}, "reason": ...}}
- ONE github_request per response. Emit it, stop, and wait for results.
- If you don't know the exact repo name, call list_repos first.
- Results come back as a user message; then answer the user's question naturally.
"""


def parse_github_request(raw: str, dlog: Optional[Callable] = None):
    """Parse the tag body. Returns {"tool": str, "args": dict, "reason": str}
    or None if unusable. Never raises."""
    try:
        text = (raw or "").strip()
        # Tolerate ```json fences Claude sometimes adds
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            _safe_dlog(dlog, "natural_github_parse_not_dict", raw_preview=text[:200])
            return None
        tool = str(data.get("tool", "")).strip()
        args = data.get("args", {})
        if not isinstance(args, dict):
            args = {}
        if tool not in _NATURAL_GH_TOOLS:
            _safe_dlog(dlog, "natural_github_parse_unknown_tool", tool=tool)
            return None
        parsed = {"tool": tool, "args": args, "reason": str(data.get("reason", ""))}
        _safe_dlog(dlog, "natural_github_parsed", tool=tool, args=args)
        return parsed
    except Exception as e:
        _safe_dlog(dlog, "natural_github_parse_error", error=str(e),
                   raw_preview=(raw or "")[:200])
        return None


def _list_repos(user_id: str, dlog: Optional[Callable]) -> str:
    """List repos reachable through every installation this user linked.
    Read-only. Never raises.

    PyGithub 2.3.0 pattern (verified against library source + live API):
    GithubIntegration.get_app_installation(id) returns an Installation whose
    requester is auto-swapped to installation auth in Installation.__init__,
    so .get_repos() (GET /installation/repositories) is correctly scoped.
    A plain Github installation client has NO get_installation method."""
    try:
        from database import list_github_app_installations
        from services.github_app_auth import get_integration

        installations = list_github_app_installations(user_id)
        if not installations:
            return "GitHub App is not connected for this user."

        lines = []
        for inst in installations:
            iid = inst.get("installation_id")
            login = inst.get("account_login", "?")
            try:
                installation = get_integration().get_app_installation(int(iid))
                count = 0
                for repo in installation.get_repos():
                    lines.append(
                        f"{repo.full_name}  "
                        f"(default branch: {repo.default_branch}, "
                        f"{'private' if repo.private else 'public'})"
                    )
                    count += 1
                    if count >= 50:
                        break
                _safe_dlog(dlog, "natural_github_list_repos_ok",
                           user_id=user_id, installation_id=iid, count=count)
            except Exception as e:
                _safe_dlog(dlog, "natural_github_list_repos_error",
                           user_id=user_id, installation_id=iid, error=str(e))
                lines.append(f"[Could not list repos for account {login}: {e}]")
        return "\n".join(lines) if lines else "No repositories accessible."
    except Exception as e:
        _safe_dlog(dlog, "natural_github_list_repos_fatal", user_id=user_id, error=str(e))
        return f"[list_repos failed: {e}]"


def execute_github_request(parsed: dict, user_id: str,
                           dlog: Optional[Callable] = None) -> str:
    """Execute one parsed github_request. Always returns a string —
    never raises, so the streaming loop can always continue."""
    try:
        tool = parsed.get("tool", "")
        args = parsed.get("args", {}) or {}
        _safe_dlog(dlog, "natural_github_execute_start",
                   user_id=user_id, tool=tool, args=args)

        if tool == "list_repos":
            result = _list_repos(user_id, dlog)
        else:
            from services.github_context_tools import execute_github_context_tool
            result = execute_github_context_tool(tool, args, user_id, dlog=dlog)

        _safe_dlog(dlog, "natural_github_execute_done",
                   user_id=user_id, tool=tool, result_len=len(result))
        return result[:16000]
    except Exception as e:
        _safe_dlog(dlog, "natural_github_execute_error",
                   user_id=user_id, error=str(e))
        return f"[GitHub request failed: {e}]"
