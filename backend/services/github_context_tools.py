"""
github_context_tools.py — GitHub App read-side context tools for the Architect
tool-use loop (new, additive, flag-gated).

Lets a user just talk to Claude naturally ("check the open PRs on my repo",
"read the comments on issue #42", "diff main against my feature branch") once
they've connected via the GitHub App. Mirrors the exact pattern already
proven by architect_search_tools.py: this module defines tool schemas that
get appended to AGENTIC_TOOLS_V2, plus a dispatcher the tool-use loop calls.

Mostly read tools (list/read/search/diff — fine on any tier). The one write
tool, push_files, is hard-gated on the installation's permission_tier being
'read_write'; any other tier gets a plain-text refusal, never an exception.

Flag: GITHUB_CONTEXT_TOOLS_V2 is only appended in pipeline.py when both
(a) get_setting("agentic_tool_use") == "true" (existing flag, default OFF)
and (b) get_setting("github_context_tools_enabled") == "true" (new flag,
default OFF). Zero effect on the legacy ReAct loop, single-pass path,
Surgeon, QA, or correction handler — this module is not imported by any
of them.
"""
import itertools
from typing import Callable, Dict, Optional

from github import InputGitTreeElement

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
    {
        "name": "list_files",
        "description": "List every file in a repository branch (recursive). Optionally filter by path prefix.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "ref": {"type": "string", "description": "Branch or commit SHA. Defaults to the default branch."},
                "path_prefix": {"type": "string", "description": "Only list files under this path, e.g. 'backend/services/'"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the content of one file from a repository. Large files are paged; call again with start_line to continue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "path": {"type": "string", "description": "File path from repo root, e.g. 'backend/services/pipeline.py'"},
                "ref": {"type": "string", "description": "Branch or commit SHA. Defaults to the default branch."},
                "start_line": {"type": "integer", "description": "1-indexed line to start from (for paging large files)"},
            },
            "required": ["owner", "repo", "path"],
        },
    },
    {
        "name": "search_code",
        "description": "Search file contents in a repository (GitHub code search; default branch only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "query": {"type": "string", "description": "Search terms, e.g. 'run_natural_pipeline_stream' or 'dlog path:backend/services'"},
            },
            "required": ["owner", "repo", "query"],
        },
    },
    {
        "name": "push_files",
        "description": "Create one commit with one or more file changes and push it to a branch. Creates the branch from the default branch if it does not exist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "branch": {"type": "string", "description": "Branch to push to, e.g. 'main' or 'feature-x'. Created from the default branch if missing."},
                "message": {"type": "string", "description": "Commit message"},
                "files": {
                    "type": "array",
                    "description": "Files to write or delete in this commit",
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
        },
    },
]

_TOOL_NAMES = {"list_prs", "get_pr_diff", "get_pr_comments", "list_issues", "get_issue_comments", "diff_branches", "list_files", "read_file", "search_code", "push_files"}

# read_file paging: serve at most this many characters of file content per call.
_READ_FILE_MAX_CHARS = 14000
# list_files: cap entries so one giant monorepo can't blow up the context.
_LIST_FILES_MAX_ENTRIES = 400


def is_github_context_tool(tool_name: str) -> bool:
    return tool_name in _TOOL_NAMES


def _find_client_for_repo(user_id: str, owner: str, repo: str, dlog: Callable):
    """Find whichever of this user's installations grants access to owner/repo.
    Returns (client, installation_dict, None) on success or
    (None, None, error_message) on failure — never raises, so the tool-use
    loop can always form a valid tool_result. installation_dict carries
    permission_tier so write tools can enforce it."""
    installations = list_github_app_installations(user_id)
    dlog("github_context_tool_find_client", user_id=user_id, owner=owner, repo=repo, installation_count=len(installations))
    if not installations:
        dlog("github_context_tool_no_installations", user_id=user_id)
        return None, None, "GitHub App is not connected for this user. Connect it in Settings first."
    for inst in installations:
        installation_id = inst["installation_id"]
        try:
            g = get_installation_client(installation_id)
            g.get_repo(f"{owner}/{repo}")  # cheap access check
            dlog("github_context_tool_client_matched", installation_id=installation_id, tier=inst.get("permission_tier"))
            return g, inst, None
        except Exception as e:
            dlog("github_context_tool_client_no_access", installation_id=installation_id, error=str(e))
            continue
    dlog("github_context_tool_repo_not_accessible", user_id=user_id, owner=owner, repo=repo)
    return None, None, f"{owner}/{repo} is not accessible via any of your connected GitHub App installations."


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

    g, inst, err = _find_client_for_repo(user_id, owner, repo, _log)
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

        elif tool_name == "list_files":
            ref = tool_input.get("ref") or r.default_branch
            path_prefix = tool_input.get("path_prefix") or ""
            _log("github_context_list_files", ref=ref, path_prefix=path_prefix)
            tree = r.get_git_tree(ref, recursive=True)
            blobs = [e for e in tree.tree if e.type == "blob"]
            if path_prefix:
                blobs = [e for e in blobs if e.path.startswith(path_prefix)]
            total = len(blobs)
            lines = [f"Files in {owner}/{repo} @ {ref}" + (f" under '{path_prefix}'" if path_prefix else "") + f" ({total} files):", ""]
            for e in blobs[:_LIST_FILES_MAX_ENTRIES]:
                lines.append(f"{e.path} ({e.size} bytes)")
            if total > _LIST_FILES_MAX_ENTRIES:
                lines.append(f"... {total - _LIST_FILES_MAX_ENTRIES} more files not shown. Call list_files again with a narrower path_prefix.")
            if total == 0:
                lines.append("(no files matched)")
            result = "\n".join(lines)

        elif tool_name == "read_file":
            path = (tool_input.get("path") or "").strip().lstrip("/")
            if not path:
                _log("github_context_read_file_missing_path")
                return "Missing 'path' for read_file."
            ref = tool_input.get("ref") or r.default_branch
            try:
                start_line = max(1, int(tool_input.get("start_line") or 1))
            except (TypeError, ValueError):
                start_line = 1
            _log("github_context_read_file", path=path, ref=ref, start_line=start_line)
            contents = r.get_contents(path, ref=ref)
            if isinstance(contents, list):
                # Path is a directory — tell the model what's inside instead of failing.
                _log("github_context_read_file_is_dir", path=path, entries=len(contents))
                entries = [f"{c.type}: {c.path}" for c in contents[:200]]
                result = f"'{path}' is a directory in {owner}/{repo} @ {ref}, not a file. Contents:\n" + "\n".join(entries)
            elif contents.encoding != "base64" or contents.content is None:
                # Files >1MB come back via a different mechanism with no inline content.
                _log("github_context_read_file_too_large", path=path, size=contents.size, encoding=str(contents.encoding))
                result = f"'{path}' is too large to read inline ({contents.size} bytes; GitHub only inlines files up to 1MB)."
            else:
                raw = contents.decoded_content
                if b"\x00" in raw[:8000]:
                    _log("github_context_read_file_binary", path=path, size=contents.size)
                    result = f"'{path}' looks like a binary file ({contents.size} bytes) — cannot display as text."
                else:
                    text = raw.decode("utf-8", errors="replace")
                    all_lines = text.splitlines()
                    total_lines = len(all_lines)
                    if start_line > total_lines:
                        _log("github_context_read_file_start_past_eof", start_line=start_line, total_lines=total_lines)
                        result = f"'{path}' has only {total_lines} lines; start_line={start_line} is past the end."
                    else:
                        chunk_lines = []
                        used = 0
                        end_line = start_line - 1
                        for i in range(start_line - 1, total_lines):
                            line = all_lines[i]
                            if used + len(line) + 1 > _READ_FILE_MAX_CHARS and chunk_lines:
                                break
                            chunk_lines.append(line)
                            used += len(line) + 1
                            end_line = i + 1
                        header = f"File: {path} @ {ref} ({total_lines} lines, {contents.size} bytes) — showing lines {start_line}-{end_line}"
                        body = "\n".join(chunk_lines)
                        footer = ""
                        if end_line < total_lines:
                            footer = f"\n\n[TRUNCATED — {total_lines - end_line} more lines. Call read_file again with start_line={end_line + 1} to continue.]"
                        _log("github_context_read_file_ok", path=path, total_lines=total_lines, shown_from=start_line, shown_to=end_line)
                        result = f"{header}\n\n{body}{footer}"

        elif tool_name == "search_code":
            query = (tool_input.get("query") or "").strip()
            if not query:
                _log("github_context_search_code_missing_query")
                return "Missing 'query' for search_code."
            full_query = f"repo:{owner}/{repo} {query}"
            _log("github_context_search_code", query=full_query)
            hits = g.search_code(query=full_query)
            lines = [f"Code search in {owner}/{repo} for '{query}':", ""]
            count = 0
            # NOTE: use islice iteration, NOT hits[:30]. PaginatedList slice-indexing
            # raises IndexError when GitHub search returns a 'next' Link header but the
            # follow-up page has empty items (incomplete_results eventual-consistency).
            # The iterator path ends cleanly on an empty page. Repro: session ff4ff718.
            for hit in itertools.islice(hits, 30):
                lines.append(f"{hit.path}")
                count += 1
            if count == 0:
                lines.append("(no matches — note: GitHub code search only indexes the default branch)")
            _log("github_context_search_code_ok", query=query, hit_count=count)
            result = "\n".join(lines)

        elif tool_name == "push_files":
            # ── Hard tier gate: only 'read_write' installations may push ──
            tier = (inst or {}).get("permission_tier", "read_only")
            if tier != "read_write":
                _log("github_context_push_files_tier_denied", tier=tier)
                return (f"Push blocked: this GitHub connection's permission tier is '{tier}'. "
                        f"Set it to 'read_write' in Settings → GitHub to allow pushes.")

            branch = (tool_input.get("branch") or "").strip()
            message = (tool_input.get("message") or "").strip()
            files = tool_input.get("files") or []
            _log("github_context_push_files_start", branch=branch, message=message, file_count=len(files))
            if not branch:
                _log("github_context_push_files_missing_branch")
                return "Missing 'branch' for push_files."
            if not message:
                _log("github_context_push_files_missing_message")
                return "Missing commit 'message' for push_files."
            if not files or not isinstance(files, list):
                _log("github_context_push_files_no_files")
                return "push_files needs a non-empty 'files' array."

            # Validate every file BEFORE touching the repo — all-or-nothing.
            for f in files:
                path = (f.get("path") or "").strip().lstrip("/") if isinstance(f, dict) else ""
                if not path:
                    _log("github_context_push_files_bad_entry", entry=str(f)[:200])
                    return "Every entry in 'files' needs a 'path'."
                if not f.get("delete") and f.get("content") is None:
                    _log("github_context_push_files_missing_content", path=path)
                    return f"File '{path}' has no 'content' and is not marked delete:true."

            # Resolve the target branch; create it from the default branch if missing.
            default_branch = r.default_branch
            try:
                ref = r.get_git_ref(f"heads/{branch}")
                _log("github_context_push_files_branch_exists", branch=branch, head_sha=ref.object.sha)
            except Exception as e:
                _log("github_context_push_files_branch_missing", branch=branch, error=str(e))
                base_sha = r.get_git_ref(f"heads/{default_branch}").object.sha
                ref = r.create_git_ref(f"refs/heads/{branch}", base_sha)
                _log("github_context_push_files_branch_created", branch=branch, from_branch=default_branch, base_sha=base_sha)

            base_commit = r.get_git_commit(ref.object.sha)
            _log("github_context_push_files_base_commit", sha=base_commit.sha)

            # Build one atomic tree with every change (create/update/delete).
            elements = []
            for f in files:
                path = f["path"].strip().lstrip("/")
                if f.get("delete"):
                    _log("github_context_push_files_delete", path=path)
                    elements.append(InputGitTreeElement(path, "100644", "blob", sha=None))
                else:
                    blob = r.create_git_blob(f["content"], "utf-8")
                    _log("github_context_push_files_blob", path=path, blob_sha=blob.sha, content_len=len(f["content"]))
                    elements.append(InputGitTreeElement(path, "100644", "blob", sha=blob.sha))

            new_tree = r.create_git_tree(elements, base_tree=base_commit.tree)
            _log("github_context_push_files_tree", tree_sha=new_tree.sha)
            commit = r.create_git_commit(message, new_tree, [base_commit])
            _log("github_context_push_files_commit", commit_sha=commit.sha)
            ref.edit(commit.sha)
            _log("github_context_push_files_ok", branch=branch, commit_sha=commit.sha, file_count=len(files))

            changed = ", ".join(f["path"] for f in files)
            result = (f"Pushed commit {commit.sha[:10]} to '{branch}' in {owner}/{repo} "
                      f"({len(files)} file(s): {changed}).\n"
                      f"View: https://github.com/{owner}/{repo}/commit/{commit.sha}")

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
