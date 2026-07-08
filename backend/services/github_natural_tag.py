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


# Tools Claude may request via the tag. Read tools work on any tier;
# push_files is additionally gated on the read_write tier inside
# execute_github_context_tool (server-side, never trusted to the model).
_NATURAL_GH_TOOLS = (
    "list_repos", "list_prs", "get_pr_diff", "get_pr_comments",
    "list_issues", "get_issue_comments", "diff_branches",
    "list_files", "read_file", "search_code", "push_files",
    "push_session_file", "check_deploy",
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


def get_known_repos(user_id: str, session_id: str,
                    dlog: Optional[Callable] = None,
                    limit: int = 3) -> list:
    """Return 'owner/repo' strings the user has actually worked with —
    this session's registered GitHub files first, then the user's most
    recently touched repos across sessions (session_files.github_meta
    joined to chat_sessions.user_id). Read-only; NEVER raises.

    Purpose (session ff4ff718 fix): sessions that start with zero files
    burned GitHub round 1 on list_repos just to rediscover a repo name
    already sitting in the user's session history."""
    try:
        from database import get_db_ctx
        repos: list = []
        with get_db_ctx() as conn:
            rows = conn.execute(
                """SELECT sf.github_meta,
                          CASE WHEN sf.session_id = ? THEN 0 ELSE 1 END AS pri
                   FROM session_files sf
                   JOIN chat_sessions cs ON cs.id = sf.session_id
                   WHERE cs.user_id = ?
                     AND sf.github_meta IS NOT NULL AND sf.github_meta != ''
                   ORDER BY pri ASC, sf.updated_at DESC
                   LIMIT 25""",
                (session_id, user_id)).fetchall()
        for row in rows:
            raw = row[0] if not hasattr(row, "keys") else row["github_meta"]
            try:
                meta = json.loads(raw or "{}")
            except Exception:
                continue
            owner = (meta.get("owner") or "").strip()
            repo = (meta.get("repo") or "").strip()
            if owner and repo:
                full = f"{owner}/{repo}"
                if full not in repos:
                    repos.append(full)
            if len(repos) >= limit:
                break
        _safe_dlog(dlog, "gh_known_repos",
                   user_id=user_id, session_id=session_id, repos=repos)
        return repos
    except Exception as e:
        _safe_dlog(dlog, "gh_known_repos_error", user_id=user_id, error=str(e))
        return []


def build_github_prompt_section(installations: list,
                                known_repos: Optional[list] = None) -> str:
    """System-prompt text telling Claude the tag exists and how to use it.
    Only ever called when natural_github_availability returned True."""
    accounts = ", ".join(
        str(i.get("account_login", "?")) for i in installations
    ) or "?"
    known_line = ""
    if known_repos:
        known_line = (
            "\nKNOWN REPOS (from this user's session history, most recent "
            f"first): {', '.join(known_repos)}\n"
            "Use these owner/repo values directly. Do NOT call list_repos "
            "when the repo you need is already listed here.\n"
        )
    return f"""
━━━ GITHUB ACCESS (LIVE) ━━━
This user has connected their GitHub account ({accounts}) via the GitHub App.
You HAVE live read access to their repositories. When the user asks about
their pull requests, issues, branches, diffs, or repos, DO NOT say you lack
access — fetch the data with a github_request tag:
{known_line}

<github_request>
{{"tool": "list_prs", "args": {{"owner": "{accounts.split(',')[0].strip()}", "repo": "REPO_NAME"}}, "reason": "why you need this"}}
</github_request>

Available tools:
- list_repos    args: {{}}                                    → repos the connection can reach (use FIRST if you don't know the repo name)
- list_prs      args: {{owner, repo, state?}}                 → open PRs ("state": "open"|"closed"|"all")
- get_pr_diff   args: {{owner, repo, pr_number}}              → file-by-file diff of one PR
- get_pr_comments args: {{owner, repo, pr_number}}            → review comments + discussion
- list_issues   args: {{owner, repo, state?}}                 → issues
- get_issue_comments args: {{owner, repo, issue_number}}      → issue discussion
- diff_branches args: {{owner, repo, base, head}}             → compare two branches
- list_files    args: {{owner, repo, ref?, path_prefix?}}     → list all files in a branch (filter with path_prefix)
- read_file     args: {{owner, repo, path, ref?, start_line?}} → read one file (paged; follow the TRUNCATED hint to continue)
- search_code   args: {{owner, repo, query}}                  → search file contents (default branch only)
- push_files    args: {{owner, repo, branch, message, files: [{{path, content}} or {{path, delete: true}}]}} → one commit to a branch (created from the default branch if missing); requires the connection's read_write tier
- push_session_file args: {{filename, message, branch?}}      → push a session file that was loaded from the repo (and edited/applied here) back to GitHub. Send ONLY the file's basename and a commit message — the server supplies the file content and repo location itself. Requires the connection's read_write tier
- check_deploy  args: {{provider?}}                           → latest deployment status from the user's connected deploy platforms ("provider": "vercel"|"railway"|"both", default "both"). On a FAILED deploy the result includes the build-log tail so you can diagnose the error

Rules:
- The tag body must be a single valid JSON object: {{"tool": ..., "args": {{...}}, "reason": ...}}
- ONE github_request per response. Emit it, stop, and wait for results.
- If you don't know the exact repo name, call list_repos first.
- Before push_files: read the current file with read_file first, and always send the COMPLETE new file content — partial content overwrites the whole file.
- PUSHING AN EDITED SESSION FILE: when the user expresses ANY intent to get an
  edited session file into GitHub — "push it", "ship it", "commit that",
  "send it to the repo", "make it live", "update GitHub", "save my changes",
  or anything similar — treat it as a push request. Specifically: when the user asks to push/commit a file that
  was loaded from the repo into this session (edited via surgical_edit + diff
  cards), ALWAYS use push_session_file with just the basename and a commit
  message — e.g. {{"tool": "push_session_file", "args": {{"filename": "LandingPage.tsx", "message": "Update pricing"}}}}.
- AFTER A SUCCESSFUL PUSH: present a short, well-formatted summary — the
  commit as a markdown link, the file with the +/- line stats from the tool
  result, a one-line "What changed" describing the edits you made in this
  session, and the Deploy line from the tool result if present. Keep it
  brief and celebratory. Do NOT re-read the file or take any further action.
  NEVER retype the file content and NEVER use push_files for these files — the
  server pushes the exact applied content from the session. This is a simple,
  instant request: do not re-read the file first.
- Never push unless the user explicitly asked for a change to be pushed/committed.
- DEPLOY QUESTIONS: when the user expresses ANY intent to check a deployment —
  "did it deploy?", "is the build done?", "did my push go live?", "any deploy
  errors?", "why did the build fail?", or anything similar — use check_deploy.
  The result includes the deployment's commit sha and timestamp: compare them
  against the commit you just pushed to confirm you are looking at the right
  deploy (a deploy created BEFORE your push is not yours — say it hasn't
  started or is still queued). If the status is a failure, the build-log tail
  is included — quote the actual error lines and explain the likely fix.
  Never claim a deploy succeeded or failed without calling check_deploy.
- CRITICAL — TAGS MUST BE TYPED, NOT THOUGHT: the <github_request> tag only
  executes when it appears in your VISIBLE response text. A tag written inside
  your thinking is never executed and the user gets nothing. NEVER end your
  response after announcing a check (e.g. "Let me check the deployment
  status") — if you announce an action, you MUST emit the tag in that same
  message. Announcing without acting is a silent failure.
- Results come back as a user message; then answer the user's question naturally.
- EDITING REPO FILES: when you read_file a code file, the COMPLETE file is
  automatically loaded into this session as an editable file — exactly as if
  the user uploaded it. To change it, emit standard <surgical_edit> blocks
  using the file's BASENAME as the filename (e.g. "LandingPage.tsx", not the
  full repo path). The user then gets a diff card with a QA score and applies
  the change themselves. For ordinary edit requests this is the ONLY correct
  flow — do NOT use push_files unless the user explicitly asked to push/commit.
- If the file is large and you have not yet seen the region you need to edit,
  keep calling read_file with start_line until you have — never guess code you
  have not seen.
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
        elif tool == "check_deploy":
            from services.deploy_status import check_deploy_status
            result = check_deploy_status(user_id, args, dlog=dlog)
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


def fetch_and_register_github_file(parsed: dict, user_id: str,
                                   session_id: str,
                                   dlog: Optional[Callable] = None):
    """After a successful read_file <github_request>, fetch the COMPLETE file
    and persist it into session_files — exactly the same row shape the
    /api/github-app/load endpoint writes (basename as filename, full repo
    path + sha inside github_meta). That makes the repo file a first-class
    session file: surgical edits, diff cards, QA, apply, and push-back all
    work on it with zero pipeline changes.

    Returns a session-file dict (id, filename, content, file_type, language,
    lines, symbol_count, github_meta) or None. NEVER raises — any failure
    logs and returns None so the chat continues with read-only behavior."""
    try:
        if parsed.get("tool") != "read_file":
            _safe_dlog(dlog, "gh_session_register_skip_tool",
                       user_id=user_id, tool=parsed.get("tool"))
            return None

        args = parsed.get("args", {}) or {}
        owner = (args.get("owner") or "").strip()
        repo = (args.get("repo") or "").strip()
        path = (args.get("path") or "").strip().lstrip("/")
        if not owner or not repo or not path:
            _safe_dlog(dlog, "gh_session_register_missing_args",
                       user_id=user_id, owner=owner, repo=repo, path=path)
            return None

        # ── Fetch the COMPLETE file (read_file results are paged; we need
        #    the whole thing to register it as an editable session file) ──
        from services.github_context_tools import _find_client_for_repo

        def _log(event, **kw):
            _safe_dlog(dlog, event, **kw)

        g, inst, err = _find_client_for_repo(user_id, owner, repo, _log)
        if err:
            _safe_dlog(dlog, "gh_session_register_no_client",
                       user_id=user_id, owner=owner, repo=repo, error=err)
            return None

        r = g.get_repo(f"{owner}/{repo}")
        ref = (args.get("ref") or "").strip() or r.default_branch
        contents = r.get_contents(path, ref=ref)
        if isinstance(contents, list):
            _safe_dlog(dlog, "gh_session_register_is_dir",
                       user_id=user_id, path=path)
            return None
        if contents.encoding != "base64" or contents.content is None:
            # GitHub only inlines files up to 1MB — same limit as read_file.
            _safe_dlog(dlog, "gh_session_register_too_large",
                       user_id=user_id, path=path, size=contents.size)
            return None
        raw = contents.decoded_content
        if b"\x00" in raw[:8000]:
            _safe_dlog(dlog, "gh_session_register_binary",
                       user_id=user_id, path=path, size=contents.size)
            return None
        text = raw.decode("utf-8", errors="replace")

        # ── Build the row exactly like /api/github-app/load does ─────────
        import uuid as _uuid
        from pathlib import Path as _Path

        filename = _Path(path).name  # basename convention (path lives in github_meta)
        ext = _Path(filename).suffix.lower()
        lang_map = {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".jsx": "javascriptreact", ".tsx": "typescriptreact",
            ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp",
            ".cpp": "cpp", ".c": "c", ".h": "c", ".hpp": "cpp",
            ".html": "html", ".css": "css", ".json": "json",
            ".md": "markdown", ".sh": "bash", ".sql": "sql",
            ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
            ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
        }
        language = lang_map.get(ext, "plaintext")
        line_count = len(text.splitlines())

        symbol_count = 0
        try:
            from services.ast_parser import ASTParser
            smap = ASTParser().parse(text, filename)
            symbol_count = len(smap.symbols)
        except Exception as pe:
            _safe_dlog(dlog, "gh_session_register_parse_failed",
                       user_id=user_id, filename=filename, error=str(pe))

        github_meta = json.dumps({
            "owner": owner, "repo": repo, "branch": ref,
            "path": path, "sha": contents.sha, "source": "github_app",
            "installation_id": (inst or {}).get("installation_id"),
        })

        # ── Persist (INSERT or UPDATE — same upsert logic as /load) ──────
        from database import get_db_ctx
        with get_db_ctx() as conn:
            existing = conn.execute(
                "SELECT id FROM session_files WHERE session_id = ? AND filename = ?",
                (session_id, filename)
            ).fetchone()
            if existing:
                file_id = existing["id"] if hasattr(existing, "__getitem__") else existing[0]
                conn.execute(
                    "UPDATE session_files SET content = ?, language = ?, lines = ?, symbol_count = ?, github_meta = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (text, language, line_count, symbol_count, github_meta, file_id)
                )
                _safe_dlog(dlog, "gh_session_register_updated",
                           user_id=user_id, session_id=session_id,
                           file_id=file_id, filename=filename)
            else:
                file_id = str(_uuid.uuid4())
                conn.execute(
                    "INSERT INTO session_files (id, session_id, filename, content, language, lines, symbol_count, file_type, github_meta, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'code', ?, CURRENT_TIMESTAMP)",
                    (file_id, session_id, filename, text, language, line_count, symbol_count, github_meta)
                )
                _safe_dlog(dlog, "gh_session_register_inserted",
                           user_id=user_id, session_id=session_id,
                           file_id=file_id, filename=filename)
            conn.commit()

        _safe_dlog(dlog, "gh_session_register_ok",
                   user_id=user_id, session_id=session_id, file_id=file_id,
                   filename=filename, repo=f"{owner}/{repo}", ref=ref,
                   path=path, lines=line_count, symbol_count=symbol_count,
                   content_chars=len(text))
        return {
            "id": file_id, "filename": filename, "content": text,
            "file_type": "code", "language": language, "lines": line_count,
            "symbol_count": symbol_count, "github_meta": github_meta,
        }
    except Exception as e:
        _safe_dlog(dlog, "gh_session_register_error",
                   user_id=user_id, session_id=session_id, error=str(e))
        return None


def push_session_file_from_db(parsed: dict, user_id: str,
                              session_id: str,
                              dlog: Optional[Callable] = None) -> str:
    """Push an already-edited session file back to its GitHub repo.

    The model sends ONLY {"filename": ..., "message": ..., "branch"?: ...}.
    The server supplies the file content from the session_files row (which
    the Apply flow keeps current) and the repo coordinates from the
    github_meta written at registration time. The model never carries file
    content — a 2-line change on a 2,700-line file costs the same as any
    other request.

    Mirrors the proven /api/github-app/commit endpoint: update_file with
    the stored blob sha (GitHub rejects stale shas, so a file changed
    upstream can never be silently clobbered), then refresh github_meta +
    github_pushed_at. Always returns a string; never raises."""
    try:
        args = parsed.get("args", {}) or {}
        filename = (args.get("filename") or "").strip().lstrip("/").rsplit("/", 1)[-1]
        message = (args.get("message") or "").strip()
        _safe_dlog(dlog, "gh_push_session_start",
                   user_id=user_id, session_id=session_id,
                   filename=filename, message=message[:200])

        if not filename:
            _safe_dlog(dlog, "gh_push_session_missing_filename", user_id=user_id)
            return ("push_session_file needs a 'filename' — the session file's "
                    "basename, e.g. \"LandingPage.tsx\".")
        if not message:
            _safe_dlog(dlog, "gh_push_session_missing_message",
                       user_id=user_id, filename=filename)
            return f"push_session_file needs a commit 'message' for '{filename}'."

        # ── 1. Load the applied content + repo coordinates from the DB ──
        from database import get_db_ctx
        with get_db_ctx() as conn:
            row = conn.execute(
                "SELECT id, content, github_meta, edited FROM session_files "
                "WHERE session_id = ? AND filename = ?",
                (session_id, filename)
            ).fetchone()

        if not row:
            _safe_dlog(dlog, "gh_push_session_no_row",
                       user_id=user_id, session_id=session_id, filename=filename)
            return (f"No session file named '{filename}' in this session. "
                    "Read it from the repo first (read_file), edit it, apply, "
                    "then push.")

        file_id = row["id"]
        content = row["content"] or ""
        meta_raw = row["github_meta"]
        edited = row["edited"]
        if not meta_raw:
            _safe_dlog(dlog, "gh_push_session_no_meta",
                       user_id=user_id, file_id=file_id, filename=filename)
            return (f"'{filename}' was not loaded from GitHub (no repo "
                    "coordinates stored), so push_session_file cannot push it. "
                    "Use push_files with the complete content instead.")

        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
        owner = (meta.get("owner") or "").strip()
        repo = (meta.get("repo") or "").strip()
        path = (meta.get("path") or "").strip().lstrip("/")
        sha = (meta.get("sha") or "").strip()
        branch = (args.get("branch") or "").strip() or (meta.get("branch") or "").strip()
        _safe_dlog(dlog, "gh_push_session_meta",
                   user_id=user_id, file_id=file_id, owner=owner, repo=repo,
                   path=path, branch=branch, sha=sha, edited=edited,
                   content_chars=len(content))

        if not (owner and repo and path and sha):
            _safe_dlog(dlog, "gh_push_session_meta_incomplete",
                       user_id=user_id, file_id=file_id, meta_keys=list(meta.keys()))
            return (f"'{filename}' has incomplete GitHub metadata "
                    "(owner/repo/path/sha) — cannot push safely.")
        if not content.strip():
            _safe_dlog(dlog, "gh_push_session_empty_content",
                       user_id=user_id, file_id=file_id, filename=filename)
            return (f"'{filename}' has empty content in this session — "
                    "refusing to push an empty file.")

        # ── 2. Tier-gated client (identical gate to push_files) ──────────
        from services.github_context_tools import _find_client_for_repo

        def _log(event, **kw):
            _safe_dlog(dlog, event, **kw)

        g, inst, err = _find_client_for_repo(user_id, owner, repo, _log)
        if err:
            _safe_dlog(dlog, "gh_push_session_no_client",
                       user_id=user_id, owner=owner, repo=repo, error=err)
            return err
        tier = (inst or {}).get("permission_tier", "read_only")
        if tier != "read_write":
            _safe_dlog(dlog, "gh_push_session_tier_denied",
                       user_id=user_id, tier=tier)
            return (f"Push blocked: this GitHub connection's permission tier "
                    f"is '{tier}'. Set it to 'read_write' in Settings → GitHub "
                    "to allow pushes.")

        # ── 3. Push with the stored blob sha ─────────────────────────────
        r = g.get_repo(f"{owner}/{repo}")
        branch = branch or r.default_branch
        try:
            result = r.update_file(
                path=path, message=message,
                content=content.encode("utf-8"), sha=sha, branch=branch,
            )
        except Exception as ge:
            _safe_dlog(dlog, "gh_push_session_update_failed",
                       user_id=user_id, path=path, branch=branch, error=str(ge))
            if "409" in str(ge) or "does not match" in str(ge):
                return (f"Push rejected: '{path}' changed on GitHub after it "
                        "was loaded into this session (stale sha). Re-read the "
                        "file, re-apply the change, then push again.")
            return f"GitHub rejected the push for '{path}': {ge}"

        new_sha = result["content"].sha if result.get("content") else sha
        commit_sha = result["commit"].sha if result.get("commit") else ""

        # ── 4. Refresh stored sha so the NEXT push isn't stale ───────────
        try:
            updated_meta = {**meta, "sha": new_sha, "branch": branch}
            with get_db_ctx() as conn:
                conn.execute(
                    "UPDATE session_files SET github_meta = ?, github_pushed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (json.dumps(updated_meta), file_id)
                )
                conn.commit()
            _safe_dlog(dlog, "gh_push_session_meta_refreshed",
                       user_id=user_id, file_id=file_id, new_sha=new_sha)
        except Exception as me:
            # The push itself succeeded — only the local sha cache refresh
            # failed. Report success; the next push would surface a stale sha.
            _safe_dlog(dlog, "gh_push_session_meta_refresh_failed",
                       user_id=user_id, file_id=file_id, error=str(me))

        _safe_dlog(dlog, "gh_push_session_ok",
                   user_id=user_id, session_id=session_id, filename=filename,
                   path=path, branch=branch, commit_sha=commit_sha,
                   new_sha=new_sha, content_chars=len(content))
        note = "" if edited else ("\n(Note: this file has no applied edits in "
                                  "this session — the pushed content matches "
                                  "what was loaded.)")

        # ── 5. Enrich summary: real +/- line stats (best-effort) ─────────
        stats_line = ""
        try:
            if commit_sha:
                commit_obj = r.get_commit(commit_sha)
                additions = commit_obj.stats.additions
                deletions = commit_obj.stats.deletions
                stats_line = f"\nChanges: +{additions} / -{deletions} lines."
                _safe_dlog(dlog, "gh_push_session_stats",
                           user_id=user_id, commit_sha=commit_sha,
                           additions=additions, deletions=deletions)
        except Exception as se:
            _safe_dlog(dlog, "gh_push_session_stats_failed",
                       user_id=user_id, commit_sha=commit_sha, error=str(se))

        # ── 6. Deploy awareness: is Vercel/Railway connected? (DB-only) ──
        deploy_line = ""
        try:
            from database import get_user_api_key
            vercel_connected = bool(get_user_api_key(user_id, "vercel"))
            railway_connected = bool(get_user_api_key(user_id, "railway"))
            _safe_dlog(dlog, "gh_push_session_deploy_check",
                       user_id=user_id, vercel=vercel_connected,
                       railway=railway_connected)
            connected = [n for n, ok in
                         (("Vercel", vercel_connected),
                          ("Railway", railway_connected)) if ok]
            if connected:
                deploy_line = ("\nDeploy: " + " and ".join(connected) +
                               " connected — the deploy should start "
                               "automatically; the user can watch the build "
                               "live in the Deploys panel.")
            else:
                deploy_line = ("\nDeploy tip: connecting Vercel or Railway in "
                               "Settings lets the user watch this deploy "
                               "build live.")
        except Exception as de:
            _safe_dlog(dlog, "gh_push_session_deploy_check_failed",
                       user_id=user_id, error=str(de))

        return (f"Pushed '{path}' to '{branch}' in {owner}/{repo} "
                f"(commit {commit_sha[:10]}).\n"
                f"View: https://github.com/{owner}/{repo}/commit/{commit_sha}"
                + stats_line + deploy_line + note)
    except Exception as e:
        _safe_dlog(dlog, "gh_push_session_error",
                   user_id=user_id, session_id=session_id, error=str(e))
        return f"[push_session_file failed: {e}]"
