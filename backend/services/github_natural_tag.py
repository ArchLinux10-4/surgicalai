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
import re
from typing import Callable, Optional

from services.github_tool_spec import ALL_TOOL_NAMES


# Every tool the model may name. The server still enforces the read_write
# tier inside the push tools.
_NATURAL_GH_TOOLS = ALL_TOOL_NAMES

GH_TAG_OPEN = "<github_request>"
GH_TAG_CLOSE = "</github_request>"

_PLACEHOLDER_VALUES = {
    "", "repo_name", "reponame", "repo-name", "<repo>", "<repo name>",
    "...", "repo", "owner", "your_repo", "your-repo", "somerepo",
    "<owner>", "user", "org",
}
_PR_NUMBER_TOOLS = {"get_pr", "get_pr_diff", "get_pr_comments"}
_ISSUE_NUMBER_TOOLS = {"get_issue", "get_issue_comments"}
_NO_REPO_TOOLS = {"list_repos", "check_deploy", "push_session_file"}
_PUSH_RE = re.compile(
    r"\b(push(?:ed|ing)?|commit(?:ted|ting)?|ship(?:ped| it)?|"
    r"make it live|send it to(?: the)? (?:repo|github)|"
    r"update github|save my changes)\b",
    re.IGNORECASE,
)
_DEPLOY_RE = re.compile(
    r"\b(deploy(?:ment|ed|ing)?|vercel|railway|go live|went live|"
    r"build(?: fail(?:ed|ure)?| error| log)?)\b",
    re.IGNORECASE,
)


def _safe_dlog(dlog: Optional[Callable], event: str, **kw) -> None:
    if dlog is None:
        return
    try:
        dlog(event, **kw)
    except Exception:
        pass


def shape_github_result(text: str, limit: int = 14000) -> str:
    """Trim to `limit` chars without dropping a trailing '[TRUNCATED ...]' hint.

    Cuts the body and keeps the last footer line so a paged read_file still
    tells the model which start_line to request next.
    """
    try:
        text = "" if text is None else str(text)
        if len(text) <= limit:
            return text
        lines = text.rstrip("\n").split("\n")
        footer = lines[-1] if lines and lines[-1].startswith("[") else ""
        keep = max(0, limit - len(footer) - 80)
        cut = text[:keep]
        if "\n" in cut:
            cut = cut.rsplit("\n", 1)[0]
        note = f"\n[Output shortened by {len(text) - len(cut)} chars to fit.]\n"
        out = cut + note + footer
        if len(out) <= limit:
            return out
        if footer and len(footer) <= limit:
            return footer
        return out[:limit]
    except Exception:
        return ("" if text is None else str(text))[:limit]


def note_read_file_page(result_text: str):
    """Last line number from a read_file header, or None. Never raises."""
    try:
        match = re.search(r"showing lines \d+-(\d+)", result_text or "")
        return int(match.group(1)) if match else None
    except Exception:
        return None


def maybe_resume_read_file(args: dict, progress: dict, *, rewrite: bool) -> dict:
    """Return args, rewriting start_line only when rewrite is true (Grok).

    progress maps 'owner|repo|path|ref' to the last line already shown.
    Claude and GPT keep the args they sent; Grok is resumed past page 1
    when it asks for the same file again from the top.
    """
    try:
        args = dict(args or {})
        key = "|".join(str(args.get(k, "")) for k in ("owner", "repo", "path", "ref"))
        prev = (progress or {}).get(key)
        start = args.get("start_line")
        try:
            start_n = int(start) if start not in (None, "") else None
        except (TypeError, ValueError):
            start_n = None
        if rewrite and prev and (start_n is None or start_n <= 1):
            args["start_line"] = int(prev) + 1
        return args
    except Exception:
        return dict(args or {})


def message_mentions_push(text: str) -> bool:
    """True when the user asked to push, or the request is ambiguous about it."""
    try:
        raw = text or ""
        if _PUSH_RE.search(raw):
            return True
        mentions_github = re.search(r"\bgithub\b", raw, re.IGNORECASE)
        mentions_change = re.search(r"\b(change|update|edit|fix)\b", raw, re.IGNORECASE)
        looks_like_question = re.match(
            r"\s*(what|why|how|when|did|is|are|do|does|can|could)\b",
            raw,
            re.IGNORECASE,
        )
        return bool(mentions_github and mentions_change and not looks_like_question)
    except Exception:
        return True


def message_mentions_deploy(text: str) -> bool:
    try:
        return bool(_DEPLOY_RE.search(text or ""))
    except Exception:
        return True


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.startswith("#"):
        text = text[1:].strip()
    if text.isdigit():
        return int(text)
    return None


def _split_repo_slug(value: str):
    text = (value or "").strip()
    if not text:
        return None, None
    web = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", text, re.IGNORECASE)
    if web:
        repo = web.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return web.group(1), repo
    blob = re.search(r"github\.com/([^/\s]+)/([^/\s]+)/blob/[^/]+/(.+)$", text, re.IGNORECASE)
    if blob:
        return blob.group(1), blob.group(2)
    if "/" in text and not text.startswith("http"):
        owner, repo = text.split("/", 1)
        repo = repo.strip().lstrip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        if owner.strip() and repo and "/" not in repo:
            return owner.strip(), repo
    return None, None


def _placeholder(value) -> bool:
    return str(value or "").strip().lower() in _PLACEHOLDER_VALUES


def _clean_path(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r"^https?://github\.com/[^/]+/[^/]+/blob/[^/]+/", "", text, flags=re.IGNORECASE)
    text = text.replace("/./", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def normalize_github_args(tool: str, args: dict, known_repos=None) -> dict:
    """Fill and clean arguments. Never rejects a call."""
    try:
        cleaned = dict(args or {})
        known = []
        for repo in known_repos or []:
            if isinstance(repo, str) and "/" in repo and repo not in known:
                known.append(repo.strip())

        repo_val = cleaned.get("repo")
        if isinstance(repo_val, str):
            owner_part, repo_part = _split_repo_slug(repo_val)
            if owner_part and repo_part:
                if _placeholder(cleaned.get("owner")) or not str(cleaned.get("owner") or "").strip():
                    cleaned["owner"] = owner_part
                cleaned["repo"] = repo_part

        if tool not in _NO_REPO_TOOLS and known:
            owner_name, repo_name = known[0].split("/", 1)
            only_one = len(known) == 1

            def _bad(value):
                return value is not None and str(value).strip().lower() in _PLACEHOLDER_VALUES

            def _absent(value):
                return value is None

            if _bad(cleaned.get("repo")) or _bad(cleaned.get("owner")):
                if _bad(cleaned.get("owner")) or _absent(cleaned.get("owner")):
                    cleaned["owner"] = owner_name
                if _bad(cleaned.get("repo")) or _absent(cleaned.get("repo")):
                    cleaned["repo"] = repo_name
            elif only_one:
                if _absent(cleaned.get("owner")) or _bad(cleaned.get("owner")):
                    cleaned["owner"] = owner_name
                if _absent(cleaned.get("repo")) or _bad(cleaned.get("repo")):
                    cleaned["repo"] = repo_name

        if tool in _PR_NUMBER_TOOLS:
            if _as_int(cleaned.get("pr_number")) is None:
                for alias in ("pr", "number", "id", "pull_number"):
                    if alias in cleaned:
                        cleaned["pr_number"] = cleaned.pop(alias)
                        break
            number = _as_int(cleaned.get("pr_number"))
            if number is not None:
                cleaned["pr_number"] = number
        if tool in _ISSUE_NUMBER_TOOLS:
            if _as_int(cleaned.get("issue_number")) is None:
                for alias in ("issue", "number", "id"):
                    if alias in cleaned:
                        cleaned["issue_number"] = cleaned.pop(alias)
                        break
            number = _as_int(cleaned.get("issue_number"))
            if number is not None:
                cleaned["issue_number"] = number

        if "state" in cleaned and cleaned.get("state") is not None:
            state = str(cleaned.get("state")).strip().lower()
            if state == "merged":
                state = "closed"
            if state not in ("open", "closed", "all"):
                state = "open"
            cleaned["state"] = state

        for key in ("path", "path_prefix"):
            if isinstance(cleaned.get(key), str):
                cleaned[key] = _clean_path(cleaned[key])

        if "start_line" in cleaned:
            number = _as_int(cleaned.get("start_line"))
            if number is not None:
                cleaned["start_line"] = number
        if "limit" in cleaned:
            number = _as_int(cleaned.get("limit"))
            if number is not None:
                cleaned["limit"] = number
        return cleaned
    except Exception:
        return dict(args or {})


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
                                known_repos: Optional[list] = None,
                                tier: str = "read_only",
                                wants_push: bool = False,
                                wants_deploy: bool = False,
                                wants_edit: bool = False) -> str:
    """Short GitHub instructions. Extra push/deploy/edit rules only when relevant."""
    try:
        from services.github_tool_spec import render_selection_table, render_tool_list
        accounts = [str(i.get("account_login", "?")) for i in (installations or [])]
        account_text = ", ".join(a for a in accounts if a) or "?"
        repos = [r for r in (known_repos or []) if isinstance(r, str) and "/" in r]
        write_ok = (tier or "read_only") == "read_write"
        access = "read and write" if write_ok else "read"
        if repos:
            owner, repo = repos[0].split("/", 1)
            repo_note = (
                "KNOWN REPOS (use these exactly as owner/repo): "
                + ", ".join(repos)
            )
            if len(repos) == 1:
                repo_note += f"\nDefault repo: {repos[0]} (you may omit owner and repo)."
            else:
                repo_note += "\nDo NOT call list_repos when the repo you need is already listed."
        else:
            owner = accounts[0].strip() if accounts else "OWNER"
            repo = "<repo name>"
            repo_note = (
                "No repo has been used in this session yet. Call list_repos first, "
                "then copy owner and repo from that list. Do not invent a repo name."
            )
        list_example = json.dumps(
            {"tool": "list_prs", "args": {"owner": owner, "repo": repo, "state": "all", "limit": 5}},
            separators=(",", ":"),
        )
        search_example = json.dumps(
            {"tool": "search_code", "args": {"owner": owner, "repo": repo, "query": "function name"}},
            separators=(",", ":"),
        )
        parts = [
            "GITHUB ACCESS (LIVE)",
            f"This user connected GitHub ({account_text}). You have {access} access. "
            "Never say you lack access; look it up.",
            repo_note,
            "Format: one tag, then stop and wait. The body is ONE JSON object.",
            f"<github_request>{list_example}</github_request>",
            f"<github_request>{search_example}</github_request>",
            "Which tool:",
            render_selection_table(),
            "Tools:",
            render_tool_list(known_repo=repos[0] if len(repos) == 1 else None, include_write=write_ok and wants_push),
            "Rules:",
            "- ONE <github_request> per response, then stop. Type the tag in the visible reply, never only in thinking, and never announce a check without emitting the tag.",
            "- Results are paged and show line numbers. If you see TRUNCATED, call read_file again with the start_line in the footer. Do not re-read the file from the start.",
            "- Do not guess code you have not seen.",
            "- Answer with the result first. Cite #number and links, and say what you did not check.",
            "- If a result is an error, follow its instruction. Do not repeat the same call.",
            "- Use owner and repo exactly as listed. When one default repo is shown, you may omit them.",
        ]
        if wants_edit:
            parts.append(
                "EDITING A REPO FILE: after read_file, the complete file is loaded into this session. "
                "Change it with <surgical_edit> using the file BASENAME (LandingPage.tsx, not the repo path). "
                "Do not use push_files unless the user asked to push. If the lines you need are not in the page, "
                "call read_file again with the start_line from the footer."
            )
        if write_ok and wants_push:
            parts.append(
                "PUSHING: when the user asks to push, commit, or ship an edited session file, call "
                'push_session_file with the basename and a commit message only. Example: '
                '{"tool":"push_session_file","args":{"filename":"LandingPage.tsx","message":"Update pricing"}}. '
                "Do not resend file content and do not call read_file first. Never push unless the user asked. "
                "After success, give the commit link, the +/- line stats, one line on what changed, and the Deploy line if present."
            )
        if wants_deploy:
            parts.append(
                "DEPLOYS: call check_deploy. Compare the result commit sha and time to the commit you pushed. "
                "Quote the build-log lines on failure. Never claim a deploy succeeded or failed without calling check_deploy."
            )
        return "\n" + "\n".join(parts) + "\n"
    except Exception:
        return (
            "\nGITHUB ACCESS (LIVE)\n"
            "Use <github_request>{\"tool\":\"list_repos\",\"args\":{}}</github_request> "
            "to see repositories, then answer from the tool result.\n"
        )


def parse_github_request(raw: str, dlog: Optional[Callable] = None,
                         known_repos=None):
    """Parse the tag body.

    Returns {"tool", "args", "reason"} on success, {"_error": message} when
    the tool name is not one we know, or None when the body is not JSON.
    Never raises.
    """
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
            from services.github_context_tools import gh_error
            return {"_error": gh_error("unknown_tool", name=tool)}
        args = normalize_github_args(tool, args, known_repos)
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
                           dlog: Optional[Callable] = None,
                           session_id: Optional[str] = None,
                           known_repos=None) -> str:
    """Execute one parsed github_request. Always returns a string —
    never raises, so the streaming loop can always continue.

    ``session_id`` is threaded into this function's own events and into
    execute_github_context_tool. ``known_repos`` fills a missing or
    placeholder owner/repo before the call runs.
    """
    def _log(event, **kw):
        kw.setdefault("session_id", session_id)
        _safe_dlog(dlog, event, **kw)

    try:
        if isinstance(parsed, dict) and parsed.get("_error"):
            return str(parsed["_error"])
        tool = (parsed or {}).get("tool", "")
        if tool not in _NATURAL_GH_TOOLS:
            from services.github_context_tools import gh_error
            return gh_error("unknown_tool", name=tool)
        args = normalize_github_args(tool, (parsed or {}).get("args", {}) or {}, known_repos)
        parsed = {"tool": tool, "args": args, "reason": (parsed or {}).get("reason", "")}
        if tool not in _NO_REPO_TOOLS and (
            _placeholder(args.get("owner")) or _placeholder(args.get("repo"))
        ):
            from services.github_context_tools import gh_error
            return gh_error("placeholder_repo", repo=args.get("repo") or args.get("owner") or "")
        _log("natural_github_execute_start",
             user_id=user_id, tool=tool, args=args)

        if tool == "list_repos":
            result = _list_repos(user_id, dlog)
        elif tool == "check_deploy":
            from services.deploy_status import check_deploy_status
            result = check_deploy_status(user_id, args, dlog=dlog)
        elif tool == "push_session_file":
            # The model sends only a basename and commit message. The server
            # loads the applied file content and repo coordinates itself.
            result = push_session_file_from_db(parsed, user_id, session_id, dlog=dlog)
        else:
            from services.github_context_tools import execute_github_context_tool
            result = execute_github_context_tool(tool, args, user_id, dlog=dlog, session_id=session_id)

        _log("natural_github_execute_done",
             user_id=user_id, tool=tool, result_len=len(result or ""))
        return shape_github_result(result or "", 16000)
    except Exception as e:
        _log("natural_github_execute_error",
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
