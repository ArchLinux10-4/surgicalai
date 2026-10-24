"""Single catalogue of GitHub tools shown to every model.

Claude, GPT, and Gemini learn these tools from the prompt text. Grok learns
them from the native ``request_github`` schema. Both are rendered from the
data below so a cheap model never sees one name in the prompt and a different
name in the tool that actually runs.
"""
import json
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Arg:
    name: str
    type: str
    required: bool
    doc: str
    enum: Tuple[str, ...] = ()


@dataclass(frozen=True)
class GhTool:
    name: str
    purpose: str
    args: Tuple[Arg, ...]
    example: dict
    write: bool = False
    # "context" tools run inside execute_github_context_tool.
    # "natural" tools are dispatched by execute_github_request itself.
    executor: str = "context"
    returns: str = ""


def _a(name, type_, required, doc, enum=()):
    return Arg(name, type_, required, doc, tuple(enum))


GH_TOOLS: Tuple[GhTool, ...] = (
    GhTool(
        name="list_repos",
        purpose="List repositories this connection can reach. Call this first when you do not know owner/repo.",
        args=(),
        example={},
        executor="natural",
        returns="One owner/repo per line, with the default branch.",
    ),
    GhTool(
        name="list_prs",
        purpose="List pull requests, newest update first. Use for 'my PRs' or 'what's in review'.",
        args=(
            _a("owner", "string", True, "Repository owner (user or org login)."),
            _a("repo", "string", True, "Repository name, not owner/repo."),
            _a("state", "string", False, "open, closed, or all. merged counts as closed. Defaults to open.", ("open", "closed", "all")),
            _a("limit", "integer", False, "How many to return (1-50). Defaults to 10."),
        ),
        example={"owner": "acme", "repo": "app", "state": "all", "limit": 5},
        returns="Lines like #57 [open] Title (by @user, updated YYYY-MM-DD) URL.",
    ),
    GhTool(
        name="get_pr",
        purpose="Read one pull request: title, author, branches, body, and link. Use before get_pr_diff.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("pr_number", "integer", True, "Pull request number, without a # sign."),
        ),
        example={"owner": "acme", "repo": "app", "pr_number": 12},
        returns="A header line, the body, and a GitHub link.",
    ),
    GhTool(
        name="get_pr_diff",
        purpose="Show what changed in one pull request. Use when the user asks what a PR changed.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("pr_number", "integer", True, "Pull request number."),
        ),
        example={"owner": "acme", "repo": "app", "pr_number": 12},
        returns="A one-line summary, the file list, then patches.",
    ),
    GhTool(
        name="get_pr_comments",
        purpose="Read review comments and discussion on one pull request.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("pr_number", "integer", True, "Pull request number."),
        ),
        example={"owner": "acme", "repo": "app", "pr_number": 12},
        returns="Comment author and body, oldest first, at most 30.",
    ),
    GhTool(
        name="list_issues",
        purpose="List issues (not pull requests), newest update first.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("state", "string", False, "open, closed, or all. Defaults to open.", ("open", "closed", "all")),
            _a("limit", "integer", False, "How many issues to return (1-50). Defaults to 10."),
        ),
        example={"owner": "acme", "repo": "app", "state": "open", "limit": 10},
        returns="Lines like #9 [open] Title (by @user, updated YYYY-MM-DD) URL.",
    ),
    GhTool(
        name="get_issue",
        purpose="Read one issue's title, body, labels, and link.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("issue_number", "integer", True, "Issue number."),
        ),
        example={"owner": "acme", "repo": "app", "issue_number": 9},
        returns="A header line, the body, and a GitHub link.",
    ),
    GhTool(
        name="get_issue_comments",
        purpose="Read the discussion on one issue.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("issue_number", "integer", True, "Issue number."),
        ),
        example={"owner": "acme", "repo": "app", "issue_number": 9},
        returns="Comment author and body, at most 30.",
    ),
    GhTool(
        name="diff_branches",
        purpose="Compare two branches. Use when the user asks what differs between two branch names.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("base", "string", True, "Base branch, for example main."),
            _a("head", "string", True, "Branch to compare against base."),
        ),
        example={"owner": "acme", "repo": "app", "base": "main", "head": "feature"},
        returns="Ahead/behind counts, a file list, then patches.",
    ),
    GhTool(
        name="list_branches",
        purpose="List branch names. Use this before guessing a branch name.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("limit", "integer", False, "How many branches to return (1-100). Defaults to 30."),
        ),
        example={"owner": "acme", "repo": "app"},
        returns="Branch names. The default branch is marked.",
    ),
    GhTool(
        name="list_commits",
        purpose="List recent commits, newest first. Optionally limit to one branch or file path.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("branch", "string", False, "Branch name. Defaults to the default branch."),
            _a("path", "string", False, "Only commits that touch this path."),
            _a("limit", "integer", False, "How many commits (1-30). Defaults to 10."),
        ),
        example={"owner": "acme", "repo": "app", "limit": 5},
        returns="Lines like abc1234 YYYY-MM-DD @user: subject.",
    ),
    GhTool(
        name="list_files",
        purpose="List files in a branch. Pass path_prefix to avoid a huge listing.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("ref", "string", False, "Branch or commit SHA. Defaults to the default branch."),
            _a("path_prefix", "string", False, "Only files under this directory, for example backend/services/."),
        ),
        example={"owner": "acme", "repo": "app", "path_prefix": "backend/"},
        returns="File paths. Tells you to narrow path_prefix when the list is cut.",
    ),
    GhTool(
        name="read_file",
        purpose="Read one file. Results are paged and numbered. Pass start_line to continue past a TRUNCATED footer.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("path", "string", True, "Path from the repo root, for example backend/services/pipeline.py."),
            _a("ref", "string", False, "Branch or commit SHA. Defaults to the default branch."),
            _a("start_line", "integer", False, "1-based line to start at. Required to read past a truncated page."),
        ),
        example={"owner": "acme", "repo": "app", "path": "backend/app.py", "start_line": 1},
        returns="Numbered lines and a header 'showing lines A-B'. A TRUNCATED footer gives the next start_line.",
    ),
    GhTool(
        name="search_code",
        purpose="Find a word or symbol in the repo, then read_file the matching path. Default branch only.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("query", "string", True, "Words to find, for example run_natural_pipeline_stream."),
        ),
        example={"owner": "acme", "repo": "app", "query": "run_natural_pipeline_stream"},
        returns="Matching paths, or line snippets when the code-search index is empty.",
    ),
    GhTool(
        name="check_deploy",
        purpose="Check the latest Vercel or Railway deployment. Use for 'did it deploy?' or a failed build.",
        args=(
            _a("provider", "string", False, "vercel, railway, or both. Defaults to both.", ("vercel", "railway", "both")),
        ),
        example={"provider": "both"},
        executor="natural",
        returns="Status, commit sha, timestamp, and the build-log tail on failure.",
    ),
    GhTool(
        name="push_files",
        purpose="Create one commit from complete file contents. Use only when the user explicitly asked to push and the file is not a session file.",
        args=(
            _a("owner", "string", True, "Repository owner."),
            _a("repo", "string", True, "Repository name."),
            _a("branch", "string", True, "Branch to commit to. Created from the default branch if missing."),
            _a("message", "string", True, "Commit message."),
            _a("files", "array", True, "Each item is {path, content} or {path, delete: true}. content replaces the whole file."),
        ),
        example={
            "owner": "acme",
            "repo": "app",
            "branch": "main",
            "message": "Update copy",
            "files": [{"path": "README.md", "content": "# App\n"}],
        },
        write=True,
        returns="Commit SHA and a GitHub link.",
    ),
    GhTool(
        name="push_session_file",
        purpose="Push a session file that was loaded from GitHub and edited here. Send only the basename and commit message.",
        args=(
            _a("filename", "string", True, "Session file basename, for example LandingPage.tsx."),
            _a("message", "string", True, "Commit message."),
            _a("branch", "string", False, "Branch override. Defaults to the branch the file was loaded from."),
        ),
        example={"filename": "LandingPage.tsx", "message": "Update pricing"},
        write=True,
        executor="natural",
        returns="Commit link, +/- line stats, and a deploy line when a host is connected.",
    ),
)


READ_TOOL_NAMES = frozenset(t.name for t in GH_TOOLS if not t.write)
ALL_TOOL_NAMES = tuple(t.name for t in GH_TOOLS)
_BY_NAME = {t.name: t for t in GH_TOOLS}

_SELECTION_ROWS = (
    "my latest/recent PRs → list_prs (state all)",
    "what changed in PR #N → get_pr then get_pr_diff",
    "PR discussion → get_pr_comments",
    "where is X defined/used → search_code then read_file",
    "show file → read_file",
    "what files exist → list_files with path_prefix",
    "branches → list_branches",
    "recent commits → list_commits",
    "issue #N → get_issue + get_issue_comments",
    "did it deploy → check_deploy",
)

_PUSH_FILES_SCHEMA = {
    "type": "object",
    "properties": {
        "owner": {"type": "string"},
        "repo": {"type": "string"},
        "branch": {"type": "string", "description": "Branch to push to. Created from the default branch if missing."},
        "message": {"type": "string", "description": "Commit message"},
        "files": {
            "type": "array",
            "description": "Files to write or delete in this commit. content replaces the whole file.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path from repo root"},
                    "content": {"type": "string", "description": "Full new file content (omit when deleting)"},
                    "delete": {"type": "boolean", "description": "Set true to delete this file"},
                },
                "required": ["path"],
            },
        },
    },
    "required": ["owner", "repo", "branch", "message", "files"],
}


def tool_by_name(name: str) -> Optional[GhTool]:
    return _BY_NAME.get(name or "")


def context_tool_names():
    return frozenset(t.name for t in GH_TOOLS if t.executor == "context")


def example_call_json(name: str) -> str:
    tool = _BY_NAME.get(name) or GH_TOOLS[0]
    return json.dumps({"tool": tool.name, "args": tool.example}, separators=(",", ":"))


def render_tool_list(known_repo: Optional[str] = None, include_write: bool = False) -> str:
    lines = []
    if known_repo:
        lines.append(f"Default repo for these calls: {known_repo}.")
    for tool in GH_TOOLS:
        if tool.write and not include_write:
            continue
        rendered = []
        for arg in tool.args:
            rendered.append(arg.name if arg.required else (
            arg.name + " (optional)" if arg.name == "path" else arg.name + "?"
        ))
        args = ", ".join(rendered)
        lines.append(f"- {tool.name} {{{args}}} — {tool.purpose}")
    return "\n".join(lines)


def render_selection_table() -> str:
    return "\n".join(f"- {row}" for row in _SELECTION_ROWS)


def anthropic_tool_schemas():
    """Schemas for the architect tool-use list (context-executor tools only)."""
    out = []
    for tool in GH_TOOLS:
        if tool.executor != "context":
            continue
        if tool.name == "push_files":
            schema = _PUSH_FILES_SCHEMA
        else:
            props = {}
            required = []
            for arg in tool.args:
                prop = {"type": arg.type, "description": arg.doc}
                if arg.enum:
                    prop["enum"] = list(arg.enum)
                props[arg.name] = prop
                if arg.required:
                    required.append(arg.name)
            schema = {"type": "object", "properties": props, "required": required}
        out.append({
            "name": tool.name,
            "description": tool.purpose,
            "input_schema": schema,
        })
    return out


def grok_function_schema(name: str = "request_github") -> dict:
    """Native function schema. One tool, flat {tool, args} envelope."""
    listing = render_tool_list(include_write=True)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                "Read or update the connected GitHub repository. "
                "Pick the tool from the list below. Large files are PAGED: "
                "read_file returns at most ~14,000 characters per call and "
                "numbers every line. If the result contains "
                "'[TRUNCATED — N more lines. Call read_file again with "
                "start_line=X to continue.]', pass that exact start_line on "
                "the next read_file for the SAME path. Do not re-call read_file "
                "on the same path without start_line. Prefer search_code, then "
                "jump with start_line. "
                "Only push_files or push_session_file when the user explicitly "
                "asked to push or commit.\n"
                + listing
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "description": (
                            "Which GitHub operation to run. Arguments for each "
                            "tool are listed in the tool description. "
                            "list_files takes path_prefix, not path. "
                            "check_deploy takes provider (vercel, railway, or both), "
                            "not owner/repo.\n"
                            + listing
                        ),
                        "enum": list(ALL_TOOL_NAMES),
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Arguments for tool. Omit owner and repo when the "
                            "prompt names exactly one default repo. "
                            "read_file: {owner, repo, path, ref?, start_line?}. "
                            "start_line is 1-based and required to see past a "
                            "truncated page. list_files uses path_prefix. "
                            "check_deploy uses {provider?}."
                        ),
                    },
                },
                "required": ["tool"],
            },
        },
    }
