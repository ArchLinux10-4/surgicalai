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
import difflib
import itertools
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from typing import Callable, Dict, Optional

from github import InputGitTreeElement

from database import list_github_app_installations, _dlog as _db_dlog
from services.github_app_auth import get_installation_client, is_github_app_configured
from services.github_tool_spec import (
    ALL_TOOL_NAMES,
    anthropic_tool_schemas,
    context_tool_names,
    example_call_json,
    tool_by_name,
)
from services.limits import GITHUB_READ_MAX_CHARS as _READ_FILE_MAX_CHARS


GITHUB_CONTEXT_TOOLS_V2 = anthropic_tool_schemas()

_TOOL_NAMES = set(context_tool_names())


def gh_error(kind: str, **kw) -> str:
    """Model-facing error text. Always bracketed, never raises.

    A leading '[' tells the edit loop this is a failure, not a file to register.
    """
    try:
        return _gh_error_text(kind, **kw)
    except Exception as exc:
        return f"[GitHub request failed: {exc}]"


def _closest_tool(name: str) -> str:
    names = list(ALL_TOOL_NAMES)
    if not names:
        return ""
    match = difflib.get_close_matches(name or "", names, n=1, cutoff=0.3)
    if match:
        return match[0]
    ranked = sorted(
        names,
        key=lambda candidate: difflib.SequenceMatcher(None, name or "", candidate).ratio(),
        reverse=True,
    )
    return ranked[0]


def _gh_error_text(kind: str, **kw) -> str:
    if kind == "unknown_tool":
        name = str(kw.get("name") or "")
        closest = _closest_tool(name)
        example = f"<github_request>{example_call_json(closest)}</github_request>"
        listed = ", ".join(ALL_TOOL_NAMES)
        return (
            f"[Unknown GitHub tool '{name}'. Valid tools: {listed}. "
            f"Did you mean '{closest}'? Resend with {example}]"
        )
    if kind == "bad_json":
        reason = kw.get("reason") or "the body was not valid JSON"
        example = kw.get("example") or example_call_json("list_prs")
        return (
            f"[Your <github_request> could not be parsed ({reason}). "
            f"The body must be ONE JSON object, for example: {example}]"
        )
    if kind == "missing_arg":
        tool = kw.get("tool") or "this tool"
        arg = kw.get("arg") or "arg"
        spec = tool_by_name(str(tool))
        example = kw.get("example") or (
            example_call_json(spec.name) if spec else example_call_json("list_prs")
        )
        return f"[{tool} needs '{arg}'. Example: {example}]"
    if kind == "repo_inaccessible":
        owner = kw.get("owner") or "?"
        repo = kw.get("repo") or "?"
        repos = [r for r in (kw.get("repos") or []) if r][:10]
        if not repos:
            return (
                f"[{owner}/{repo} is not accessible via any of your connected "
                "GitHub App installations. Call list_repos to see what you can use.]"
            )
        shown = ", ".join(repos)
        return (
            f"[{owner}/{repo} is not available through the connected GitHub App. "
            f"Repos you CAN use: {shown}. Use one of these exactly, or call list_repos.]"
        )
    if kind == "not_found":
        return _not_found_text(**kw)
    if kind == "rate_limited":
        return (
            "[GitHub rate limit reached. Do not retry now; answer from what you "
            "already have and tell the user.]"
        )
    if kind == "auth":
        return (
            "[GitHub rejected the connection (permissions). Tell the user to "
            "reconnect GitHub in Settings.]"
        )
    if kind == "placeholder_repo":
        return (
            "[Repo name "
            f"'{kw.get('repo') or ''}' is a placeholder. Call list_repos, then "
            "use an owner/repo from that list exactly.]"
        )
    message = re.sub(r"[{}]", "", str(kw.get("message") or "unknown error"))
    message = " ".join(message.split())[:160]
    tool = kw.get("tool") or "the request"
    return (
        f"[GitHub could not complete {tool}: {message}. "
        "Try a different request or answer from what you have.]"
    )


def _not_found_text(**kw) -> str:
    tool = kw.get("tool") or ""
    repo = f"{kw.get('owner') or '?'}/{kw.get('repo') or '?'}"
    if tool in ("get_pr", "get_pr_diff", "get_pr_comments"):
        number = kw.get("number")
        return (
            f"[PR #{number} was not found in {repo}. "
            'Call list_prs (state "all") to see valid numbers.]'
        )
    if tool in ("get_issue", "get_issue_comments"):
        number = kw.get("number")
        return (
            f"[Issue #{number} was not found in {repo}. "
            'Call list_issues (state "all") to see valid numbers.]'
        )
    if tool == "read_file":
        path = kw.get("path") or ""
        ref = kw.get("ref") or "the default branch"
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        prefix = directory or ""
        extra = ""
        matches = kw.get("matches") or []
        if matches:
            extra = " Close paths: " + ", ".join(matches[:5]) + "."
        return (
            f"['{path}' does not exist on {ref}. Call list_files with "
            f'path_prefix "{prefix}" or search_code to find it.{extra}]'
        )
    if kw.get("branch") or tool in ("list_commits", "diff_branches", "list_files", "list_branches"):
        branch = kw.get("branch") or kw.get("ref") or ""
        if branch:
            return f"[Branch '{branch}' not found. Call list_branches.]"
    return (
        f"[GitHub could not find that {tool or 'object'} in {repo}. "
        "Call list_repos or list_files and use a value from the result.]"
    )


def _clamp_limit(raw, default: int, lo: int, hi: int) -> int:
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _ymd(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:10]
    formatter = getattr(value, "strftime", None)
    if formatter is None:
        return ""
    try:
        return value.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _login(user) -> str:
    if user is None:
        return "unknown"
    if isinstance(user, str):
        return user or "unknown"
    return getattr(user, "login", None) or getattr(user, "name", None) or "unknown"


def _label_names(obj) -> list:
    names = []
    for label in getattr(obj, "labels", None) or []:
        if isinstance(label, str) and label:
            names.append(label)
        else:
            name = getattr(label, "name", None)
            if name:
                names.append(str(name))
    return names


def _total_count(seq, shown: int) -> int:
    try:
        return int(seq.totalCount)
    except Exception:
        try:
            return len(seq)
        except Exception:
            return shown


def _more_footer(shown: int, total: int, noun: str) -> str:
    if total <= shown:
        return ""
    wider = min(50, max(shown * 3, shown + 10))
    return f'\nShowing {shown} of {total}. Call again with "limit": {wider} for more {noun}.'


def _change_summary(count: int, additions: int, deletions: int) -> str:
    noun = "file" if count == 1 else "files"
    return f"{count} {noun} changed, +{additions}/-{deletions}"


def _clip_patch(patch) -> str:
    if not patch:
        return ""
    if len(patch) <= 4000:
        return patch
    return patch[:4000] + "\n[patch truncated]"


def _suggest_accessible_repos(user_id: str, limit: int = 10) -> list:
    try:
        from services.github_natural_tag import _list_repos
        text = _list_repos(user_id, None)
        names = []
        for line in (text or "").splitlines():
            token = line.split()[0] if line.split() else ""
            if "/" in token and not token.startswith("["):
                names.append(token)
            if len(names) >= limit:
                break
        return names
    except Exception:
        return []


def _file_suggestions(repo_obj, ref, path, owner, repo, _log) -> list:
    try:
        tree = _get_cached_tree(
            repo_obj, ref or getattr(repo_obj, "default_branch", "main"),
            owner, repo, _log,
        )
    except Exception:
        return []
    paths = []
    for entry in getattr(tree, "tree", None) or []:
        if getattr(entry, "type", None) == "blob" and getattr(entry, "path", None):
            paths.append(entry.path)
    if not paths or not path:
        return []
    hits = difflib.get_close_matches(path, paths, n=5, cutoff=0.5)
    if hits:
        return hits
    base = path.rsplit("/", 1)[-1]
    same_name = [item for item in paths if item.rsplit("/", 1)[-1] == base]
    if same_name:
        return same_name[:5]
    return difflib.get_close_matches(base, paths, n=5, cutoff=0.4)


def _exc_status(exc) -> Optional[int]:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    msg = str(exc)
    for code in (429, 404, 422, 403, 401):
        if str(code) in msg:
            return code
    return None


def _classify_github_exception(tool_name, tool_input, exc, repo_obj=None, _log=None) -> str:
    status = _exc_status(exc)
    message = str(exc)
    lowered = message.lower()
    tool_input = tool_input or {}
    if "rate limit" in lowered or status == 429:
        return gh_error("rate_limited")
    if status in (401, 403):
        return gh_error("auth")
    owner = tool_input.get("owner") or ""
    repo = tool_input.get("repo") or ""
    if status == 404 or "not found" in lowered:
        ref = (tool_input.get("ref") or tool_input.get("branch") or "").strip()
        if tool_name == "read_file":
            path = (tool_input.get("path") or "").strip().lstrip("/")
            matches = []
            if repo_obj is not None and _log is not None:
                matches = _file_suggestions(
                    repo_obj, ref or getattr(repo_obj, "default_branch", "main"),
                    path, owner, repo, _log,
                )
            return gh_error(
                "not_found", tool=tool_name, owner=owner, repo=repo,
                path=path, ref=ref or getattr(repo_obj, "default_branch", "the default branch"),
                matches=matches,
            )
        if tool_name in ("get_pr", "get_pr_diff", "get_pr_comments"):
            return gh_error(
                "not_found", tool=tool_name, owner=owner, repo=repo,
                number=tool_input.get("pr_number"),
            )
        if tool_name in ("get_issue", "get_issue_comments"):
            return gh_error(
                "not_found", tool=tool_name, owner=owner, repo=repo,
                number=tool_input.get("issue_number"),
            )
        branch = (
            tool_input.get("branch") or tool_input.get("base")
            or tool_input.get("head") or ref
        )
        if branch or tool_name in ("list_commits", "diff_branches", "list_files"):
            return gh_error(
                "not_found", tool=tool_name, owner=owner, repo=repo, branch=branch,
            )
        return gh_error("not_found", tool=tool_name, owner=owner, repo=repo)
    return gh_error("generic", tool=tool_name, message=message)


def _working_branch_missing(exc, tool_name: str) -> bool:
    """True when a 404 is the working branch itself, not a missing file.

    read_file returns 404 both for a bad path and a bad ref. GitHub says
    'No commit found for the ref' only for the ref. list_commits and
    list_files 404 when the ref does not exist.
    """
    status = _exc_status(exc)
    lowered = str(exc).lower()
    if status != 404 and "not found" not in lowered:
        return False
    if "no commit found" in lowered or "reference does not exist" in lowered:
        return True
    return tool_name in ("list_commits", "list_files")


# read_file paging uses GITHUB_READ_MAX_CHARS.
# list_files: cap entries so one giant monorepo can't blow up the context.
_LIST_FILES_MAX_ENTRIES = 400

# search_code fallback (bounded direct-content scan, only runs when GitHub's
# code-search index returns zero hits — see _search_code_fallback_scan below
# for why that happens and why retrying the search itself never helps).
_SEARCH_FALLBACK_MAX_FILES = 40
_SEARCH_FALLBACK_MAX_FILE_BYTES = 300_000
_SEARCH_FALLBACK_MAX_MATCHES = 20
_SEARCH_FALLBACK_SKIP_EXT = {
    ".lock", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2",
    ".ttf", ".eot", ".zip", ".tar", ".gz", ".mp4", ".mp3", ".wasm", ".so",
    ".dylib", ".exe", ".bin", ".svg", ".webp", ".map",
}
_SEARCH_FALLBACK_SKIP_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "cargo.lock",
    "poetry.lock", "composer.lock",
}

# Fallback content fetches run concurrently instead of serially. Confirmed
# root cause (log 46dc1cca-16c3-4643-b767-6ce0dad4829f): 40 serial
# get_contents() calls per zero-hit search_code call burned the entire
# Plan-mode round budget across a session's worth of searches, producing a
# silent 398-char stub. Bounded worker count keeps this well under GitHub's
# secondary rate limits (App installation tokens: ~5000 req/hr, and this
# never exceeds _SEARCH_FALLBACK_MAX_FILES=40 concurrent-ish requests for
# one search_code call).
_SEARCH_FALLBACK_MAX_WORKERS = 8

# Session-scoped git-tree cache. The same root cause log shows Plan mode
# issuing many search_code / list_files calls against the same repo+ref
# within one session — each one re-walked get_git_tree(recursive=True)
# from scratch even though the tree cannot have changed between calls a
# few seconds apart. Short TTL (not "forever") so a mid-session push by
# the user (or push_files from this same tool) can't leave later calls
# reading a stale tree for long.
_TREE_CACHE_TTL_SECONDS = 180
_tree_cache: Dict[str, tuple] = {}
_tree_cache_lock = threading.Lock()


def is_github_context_tool(tool_name: str) -> bool:
    return tool_name in _TOOL_NAMES


def _get_cached_tree(r, ref: str, owner: str, repo: str, _log):
    """get_git_tree(recursive=True) wrapped with a short-TTL in-process
    cache keyed by owner/repo@ref. Always returns the same object shape as
    r.get_git_tree(...) (a Tree with .tree). Never raises on cache-layer
    logic — cache errors fall through to a fresh direct call."""
    key = f"{owner}/{repo}@{ref}"
    now = time.monotonic()
    with _tree_cache_lock:
        cached = _tree_cache.get(key)
    if cached and (now - cached[0]) < _TREE_CACHE_TTL_SECONDS:
        _log("github_context_tree_cache_hit", key=key, age_s=round(now - cached[0], 1))
        return cached[1]
    t0 = time.monotonic()
    tree = r.get_git_tree(ref, recursive=True)
    elapsed_ms = round((time.monotonic() - t0) * 1000)
    with _tree_cache_lock:
        _tree_cache[key] = (now, tree)
    _log("github_context_tree_cache_miss", key=key, fetch_ms=elapsed_ms, entry_count=len(tree.tree))
    return tree


def classify_paths_against_tree(names, exact_paths, by_basename, truncated):
    """Pure, dependency-free resolution decision used by resolve_paths_via_tree.

    Kept import-free on purpose so it can be unit-tested in isolation (the
    truncation + ambiguity guards are the P0 correctness core). Given the set
    of exact repo paths, a basename -> [paths] index, and the tree's
    ``truncated`` flag, classify each requested name.

    Rules:
      * exact normalized-path match  -> "resolved"
      * requested subdir suffix uniquely matching one candidate -> "resolved"
      * exactly one basename candidate -> "resolved"
      * more than one candidate       -> "ambiguous" (never auto-pick)
      * zero candidates + COMPLETE tree -> "absent"
      * zero candidates + TRUNCATED tree -> "unknown"  (absence unprovable)
    """
    def _norm(n):
        n = n.strip()
        while n.startswith("./"):
            n = n[2:]
        return n.lstrip("/")

    resolutions = {}
    for n in names:
        nn = _norm(n)
        base = nn.rsplit("/", 1)[-1]
        if nn in exact_paths:
            resolutions[n] = {"status": "resolved", "path": nn, "candidates": []}
            continue
        cands = list(by_basename.get(base, []))
        if "/" in nn:
            suffix_hits = [p for p in cands if p == nn or p.endswith("/" + nn)]
            if len(suffix_hits) == 1:
                resolutions[n] = {"status": "resolved",
                                  "path": suffix_hits[0], "candidates": []}
                continue
            if len(suffix_hits) > 1:
                cands = suffix_hits
        if len(cands) == 1:
            resolutions[n] = {"status": "resolved", "path": cands[0],
                              "candidates": []}
        elif len(cands) > 1:
            resolutions[n] = {"status": "ambiguous", "path": None,
                              "candidates": cands[:10]}
        else:
            resolutions[n] = {"status": "unknown" if truncated else "absent",
                              "path": None, "candidates": []}
    return resolutions


def resolve_paths_via_tree(
    owner: str,
    repo: str,
    user_id: str,
    requested_names,
    dlog: Optional[Callable] = None,
    session_id: Optional[str] = None,
    deadline_s: Optional[float] = None,
) -> Dict:
    """Resolve many requested file names against the repo in ONE cached
    recursive-tree lookup, instead of ~8 speculative get_contents path reads
    plus code-search variants *per file* (session 05190c82: that per-file
    guessing cost ~41s/file and, for files not in the repo at all, burned
    minutes before the pipeline ever paused to ask the user).

    Returns (never raises):
      {
        "tree_sha": str|None,
        "truncated": bool,
        "entry_count": int,
        "error": str|None,
        "resolutions": {
            <requested_name>: {
                "status": "resolved"|"absent"|"ambiguous"|"unknown",
                "path": str|None,          # exact repo path when resolved
                "candidates": [str, ...],  # basename candidates (ambiguous)
            }, ...
        }
      }

    P0 correctness — GitHub Tree API truncation: get_git_tree(recursive=True)
    sets truncated=True when the repo exceeds GitHub's limits (~100k entries /
    ~7MB). When truncated, a name we did not see is NOT proven absent, so its
    status is "unknown" and the caller MUST fall back to the legacy resolver
    rather than jumping straight to a human pause. Only a COMPLETE tree yields
    "absent".

    P0 correctness — basename ambiguity: the basename index is candidate
    generation only. An exact normalized-path match always wins. A bare
    basename that maps to >1 repo path is "ambiguous" (logged) and is never
    silently resolved to an arbitrary path.

    P1 — bounded fetch: the tree fetch runs under deadline_s (when given) so a
    slow or rate-limited GitHub call can't hang the streaming phase; on timeout
    every name is "unknown" (→ legacy fallback).
    """
    def _log(event, **kw):
        kw.setdefault("session_id", session_id)
        kw.setdefault("user_id", user_id)
        if dlog:
            try:
                dlog(event, **kw)
            except Exception:
                pass
        try:
            _db_dlog(event, **kw)
        except Exception:
            pass

    names = [n for n in (requested_names or []) if n]
    out = {"tree_sha": None, "truncated": False, "entry_count": 0,
           "error": None, "resolutions": {}}
    if not names:
        return out

    def _all_unknown(reason, **extra):
        out["resolutions"] = {n: {"status": "unknown", "path": None,
                                  "candidates": []} for n in names}
        out["error"] = reason
        _log("github_filereq_tree_resolve_fallback", reason=reason,
             names=names[:10], **extra)
        return out

    if not is_github_app_configured():
        return _all_unknown("app_not_configured")

    g, inst, err = _find_client_for_repo(user_id, owner, repo, _log)
    if err:
        return _all_unknown("no_client", owner=owner, repo=repo, detail=err)

    try:
        r = g.get_repo(f"{owner}/{repo}")
        ref = r.default_branch or "main"
        if session_id:
            from services.github_natural_tag import resolve_read_ref
            filled_ref, from_target = resolve_read_ref("", session_id, owner, repo)
            if from_target and filled_ref:
                ref = filled_ref
                _log(
                    "gh_session_target_ref_used",
                    tool="resolve_paths_via_tree", ref=filled_ref,
                    owner=owner, repo=repo,
                )
    except Exception as e:
        return _all_unknown("get_repo_error", owner=owner, repo=repo,
                            detail=str(e)[:200])

    # ── P1: bounded tree fetch ──
    t0 = time.monotonic()
    try:
        if deadline_s and deadline_s > 0:
            with ThreadPoolExecutor(max_workers=1) as _pool:
                _fut = _pool.submit(_get_cached_tree, r, ref, owner, repo, _log)
                tree = _fut.result(timeout=deadline_s)
        else:
            tree = _get_cached_tree(r, ref, owner, repo, _log)
    except FuturesTimeoutError:
        return _all_unknown("tree_fetch_timeout", owner=owner, repo=repo,
                            ref=ref, deadline_s=round(float(deadline_s or 0), 1))
    except Exception as e:
        return _all_unknown("tree_fetch_error", owner=owner, repo=repo,
                            ref=ref, detail=str(e)[:200])

    # P0: truncation makes "absent" unprovable — read the completeness flag
    # from the raw payload (already populated; no extra network call).
    raw = getattr(tree, "raw_data", None) or {}
    truncated = bool(raw.get("truncated", False))
    out["truncated"] = truncated
    out["tree_sha"] = getattr(tree, "sha", None)

    exact_paths = set()
    by_basename: Dict[str, list] = {}
    for e in (getattr(tree, "tree", None) or []):
        if getattr(e, "type", None) != "blob":
            continue
        p = e.path
        exact_paths.add(p)
        base = p.rsplit("/", 1)[-1]
        by_basename.setdefault(base, []).append(p)
    out["entry_count"] = len(exact_paths)

    out["resolutions"] = classify_paths_against_tree(
        names, exact_paths, by_basename, truncated)
    for _rn, _rv in out["resolutions"].items():
        if _rv["status"] == "ambiguous":
            _log("github_filereq_tree_resolve_ambiguous", filename=_rn,
                 candidate_count=len(_rv["candidates"]),
                 candidates=_rv["candidates"])

    _log("github_filereq_tree_resolve_done", owner=owner, repo=repo, ref=ref,
         tree_sha=out["tree_sha"], truncated=truncated,
         entry_count=out["entry_count"],
         fetch_ms=round((time.monotonic() - t0) * 1000),
         resolved=[k for k, v in out["resolutions"].items() if v["status"] == "resolved"],
         absent=[k for k, v in out["resolutions"].items() if v["status"] == "absent"],
         ambiguous=[k for k, v in out["resolutions"].items() if v["status"] == "ambiguous"],
         unknown=[k for k, v in out["resolutions"].items() if v["status"] == "unknown"])
    return out


def _search_code_fallback_scan(r, ref: str, query: str, _log, owner: str = "", repo: str = "") -> str:
    """GitHub's Code Search REST index does not cover every repository —
    it commonly returns 0 hits with incomplete_results=True for new or
    private repos regardless of the query, and retrying with different
    search terms never helps (confirmed: GitHub community discussions
    #18624, #66113, #72266 — indexing lag or repo simply not indexed yet).
    When that happens, fall back to a bounded direct scan of file content
    using the same get_git_tree/get_contents calls list_files and read_file
    already use in production. Never raises — always returns a string
    (possibly empty) so the caller can append it or skip it safely."""
    tokens = [t.lower() for t in query.replace('"', " ").split() if len(t) >= 2]
    if not tokens:
        return ""
    try:
        tree = _get_cached_tree(r, ref, owner, repo, _log)
    except Exception as e:
        _log("github_context_search_fallback_tree_error", error=str(e))
        return ""

    candidates = []
    for e in tree.tree:
        if e.type != "blob":
            continue
        name = e.path.rsplit("/", 1)[-1].lower()
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        if name in _SEARCH_FALLBACK_SKIP_NAMES or ext in _SEARCH_FALLBACK_SKIP_EXT:
            continue
        if (e.size or 0) > _SEARCH_FALLBACK_MAX_FILE_BYTES:
            continue
        candidates.append(e)

    # Cheap relevance boost: scan files whose path already mentions a
    # query token before the rest, then cap total files scanned so one
    # zero-hit search can't blow the tool's time/rate-limit budget.
    def _path_hits(e):
        p = e.path.lower()
        return sum(1 for t in tokens if t in p)
    candidates.sort(key=_path_hits, reverse=True)
    candidates = candidates[:_SEARCH_FALLBACK_MAX_FILES]

    _log("github_context_search_fallback_scan_start", candidate_count=len(candidates), tokens=tokens,
         max_workers=_SEARCH_FALLBACK_MAX_WORKERS)

    import base64

    def _fetch_one(e):
        """Runs on a worker thread — must never raise; PyGithub's requests
        calls release the GIL while blocked on network I/O, which is what
        makes this fetch loop actually parallelize instead of just adding
        thread overhead."""
        try:
            contents = r.get_contents(e.path, ref=ref)
            if isinstance(contents, list) or contents.encoding != "base64" or contents.content is None:
                return (e.path, None, None)
            text = base64.b64decode(contents.content).decode("utf-8", errors="ignore")
            return (e.path, text, None)
        except Exception as ex:
            return (e.path, None, str(ex))

    matches = []
    scanned = 0
    errors = 0
    scan_t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=_SEARCH_FALLBACK_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, e): e for e in candidates}
        for fut in as_completed(futures):
            if len(matches) >= _SEARCH_FALLBACK_MAX_MATCHES:
                # Stop consuming results early. Futures already running
                # finish naturally (their threads are already spawned);
                # this only cancels ones the pool hasn't started yet, so
                # it still trims the tail instead of waiting on all 40.
                for other in futures:
                    other.cancel()
                break
            path, text, err = fut.result()
            if err is not None:
                errors += 1
                _log("github_context_search_fallback_file_error", path=path, error=err)
                continue
            if text is None:
                continue
            scanned += 1
            lower_text = text.lower()
            if all(t in lower_text for t in tokens):
                snippet = None
                for i, line in enumerate(text.splitlines(), start=1):
                    if any(t in line.lower() for t in tokens):
                        snippet = f"{path}:{i}: {line.strip()[:180]}"
                        break
                matches.append(snippet or f"{path}: (match found, no single line shows all terms)")

    scan_elapsed_ms = round((time.monotonic() - scan_t0) * 1000)
    _log("github_context_search_fallback_scan_done", scanned=scanned, matches=len(matches),
         errors=errors, elapsed_ms=scan_elapsed_ms, candidate_count=len(candidates))
    if not matches:
        return (f"\n\n[Fallback direct-content scan of {scanned} files (code-search index "
                f"returned no results for this repo) found no matches either — the terms may "
                f"genuinely not be in the code, or may be in a file skipped by the scan (binary/lock/large-file filters).]")
    lines = [
        "\n\n[GitHub's code-search index returned no results for this repo — this is a known "
        "GitHub limitation where new or private repos are often not indexed yet, not a query "
        f"problem. Falling back to a direct scan of {scanned} files:]",
        "",
    ]
    lines.extend(matches)
    if len(matches) >= _SEARCH_FALLBACK_MAX_MATCHES:
        lines.append(f"... stopped at {_SEARCH_FALLBACK_MAX_MATCHES} matches. Use read_file on a matched path for full context.")
    return "\n".join(lines)


def _work_item_line(item) -> str:
    labels = _label_names(item)
    updated = _ymd(getattr(item, "updated_at", None))
    bits = [f"by @{_login(getattr(item, 'user', None))}"]
    if updated:
        bits.append(f"updated {updated}")
    if labels:
        bits.append("labels: " + ", ".join(labels))
    url = getattr(item, "html_url", None) or ""
    link = f" {url}" if url else ""
    title = getattr(item, "title", "") or ""
    state = getattr(item, "state", "") or ""
    number = getattr(item, "number", "?")
    return f"#{number} [{state}] {title} ({', '.join(bits)}){link}"


def _format_pr_detail(pr, number, owner, repo) -> str:
    state = "merged" if getattr(pr, "merged", False) else (getattr(pr, "state", None) or "open")
    base = getattr(getattr(pr, "base", None), "ref", "?")
    head = getattr(getattr(pr, "head", None), "ref", "?")
    additions = getattr(pr, "additions", None)
    deletions = getattr(pr, "deletions", None)
    changed = getattr(pr, "changed_files", None)
    stats = ""
    if additions is not None and deletions is not None:
        nfiles = changed if changed is not None else "?"
        stats = f", +{additions}/-{deletions} in {nfiles} files"
    updated = _ymd(getattr(pr, "updated_at", None))
    updated_bit = f", updated {updated}" if updated else ""
    title = getattr(pr, "title", "") or ""
    header = (
        f'PR #{number} [{state}] "{title}" by @{_login(getattr(pr, "user", None))}, '
        f"{base} <- {head}{stats}{updated_bit}"
    )
    body = (getattr(pr, "body", None) or "").strip() or "(no description)"
    if len(body) > 1500:
        body = body[:1500] + "\n[body truncated]"
    url = getattr(pr, "html_url", None) or f"https://github.com/{owner}/{repo}/pull/{number}"
    return f"{header}\n\nBody:\n{body}\n\nLink: {url}"


def _format_issue_detail(issue, number, owner, repo) -> str:
    title = getattr(issue, "title", "") or ""
    state = getattr(issue, "state", None) or "open"
    labels = _label_names(issue)
    label_bit = f", labels: {', '.join(labels)}" if labels else ""
    updated = _ymd(getattr(issue, "updated_at", None))
    updated_bit = f", updated {updated}" if updated else ""
    header = (
        f'Issue #{number} [{state}] "{title}" by @{_login(getattr(issue, "user", None))}'
        f"{label_bit}{updated_bit}"
    )
    body = (getattr(issue, "body", None) or "").strip() or "(no description)"
    if len(body) > 1500:
        body = body[:1500] + "\n[body truncated]"
    url = getattr(issue, "html_url", None) or f"https://github.com/{owner}/{repo}/issues/{number}"
    return f"{header}\n\nBody:\n{body}\n\nLink: {url}"


def _comment_lines(comments, located=False):
    collected = []
    for comment in itertools.islice(comments, 200):
        body = (getattr(comment, "body", "") or "").strip()
        if len(body) > 1200:
            body = body[:1200] + "…"
        who = _login(getattr(comment, "user", None))
        if located:
            path = getattr(comment, "path", "") or ""
            line_no = getattr(comment, "line", None) or getattr(comment, "original_line", None) or "?"
            collected.append(f"{who} (on {path}:{line_no}): {body}")
        else:
            collected.append(f"{who}: {body}")
    hidden = 0
    if len(collected) > 30:
        hidden = len(collected) - 30
        collected = collected[-30:]
    return collected, hidden


def _commit_line(commit) -> str:
    sha = (getattr(commit, "sha", "") or "")[:7]
    inner = getattr(commit, "commit", None)
    subject = ""
    date = ""
    if inner is not None:
        subject = ((getattr(inner, "message", "") or "").splitlines() or [""])[0][:120]
        author = getattr(inner, "author", None)
        date = _ymd(getattr(author, "date", None))
    user = _login(getattr(commit, "author", None))
    if user == "unknown" and inner is not None:
        named = getattr(getattr(inner, "author", None), "name", None)
        if named:
            user = str(named)
    return f"{sha} {date} @{user}: {subject}".strip()


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
        return None, None, (
            "[GitHub App is not connected for this user. Tell the user to "
            "connect GitHub in Settings.]"
        )
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
    return None, None, gh_error(
        "repo_inaccessible", owner=owner, repo=repo,
        repos=_suggest_accessible_repos(user_id),
    )


def default_branch_push_block(branch: str, default_branch: str) -> str:
    """Refuse a push that would commit straight onto the default branch."""
    if (branch or "").strip() and (branch or "").strip() == (default_branch or "").strip():
        return (
            "Pushing to the default branch is blocked. "
            "Use git_cli: create a branch, commit, push it, then gh pr create."
        )
    return ""


def execute_github_context_tool(
    tool_name: str,
    tool_input: Dict,
    user_id: str,
    dlog: Optional[Callable] = None,
    session_id: Optional[str] = None,
) -> str:
    """Execute one of the GitHub context tools. Never raises — always
    returns a string so the tool-use loop can always form a valid
    tool_result block, even on total failure.

    ``session_id`` (added alongside the PR #134 search_code fallback
    investigation) is purely additive: every _log(...) call inside this
    function — including calls made through the ``_log`` closure passed
    down into ``_find_client_for_repo`` and ``_search_code_fallback_scan``
    — now carries it automatically via kw.setdefault below, so these
    events show up in per-session debug exports instead of an untagged
    bucket. No behavior/return-value change for any caller (Claude, GPT,
    or Grok) — logging only.
    """

    def _log(event, **kw):
        kw.setdefault("session_id", session_id)
        kw.setdefault("user_id", user_id)
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
        return gh_error("unknown_tool", name=tool_name)

    if not is_github_app_configured():
        _log("github_context_tool_app_not_configured", tool_name=tool_name)
        return (
            "[GitHub App is not configured on this server yet. Tell the user "
            "an admin must configure the GitHub App.]"
        )

    owner = tool_input.get("owner", "")
    repo = tool_input.get("repo", "")
    if not owner or not repo:
        _log("github_context_tool_missing_owner_repo", tool_name=tool_name)
        missing = "owner" if not owner else "repo"
        return gh_error("missing_arg", tool=tool_name, arg=missing)

    g, inst, err = _find_client_for_repo(user_id, owner, repo, _log)
    if err:
        return err

    tool_input = dict(tool_input or {})
    _used_session_ref = False
    _session_ref_name = ""

    def _read_ref(explicit, key="ref"):
        nonlocal _used_session_ref, _session_ref_name
        from services.github_natural_tag import resolve_read_ref
        filled, from_target = resolve_read_ref(explicit, session_id, owner, repo)
        if from_target:
            _used_session_ref = True
            _session_ref_name = filled
            if not str(tool_input.get(key) or "").strip():
                tool_input[key] = filled
            _log(
                "gh_session_target_ref_used",
                tool=tool_name, ref=filled, owner=owner, repo=repo,
            )
        return filled

    try:
        r = g.get_repo(f"{owner}/{repo}")

        if tool_name == "list_prs":
            state = tool_input.get("state") or "open"
            limit = _clamp_limit(tool_input.get("limit"), 10, 1, 50)
            prs = r.get_pulls(state=state, sort="updated", direction="desc")
            shown = list(itertools.islice(prs, limit))
            if not shown:
                if state == "all":
                    result = "No pull requests found."
                else:
                    result = (
                        f'No {state} pull requests. Call list_prs with '
                        '"state": "all" to include closed and merged.'
                    )
            else:
                lines = []
                for pr in shown:
                    lines.append(_work_item_line(pr))
                total = _total_count(prs, len(shown))
                result = "\n".join(lines) + _more_footer(len(shown), total, "pull requests")

        elif tool_name == "get_pr":
            pr_number = int(tool_input.get("pr_number"))
            pr = r.get_pull(pr_number)
            result = _format_pr_detail(pr, pr_number, owner, repo)

        elif tool_name == "get_pr_diff":
            pr_number = int(tool_input.get("pr_number"))
            pr = r.get_pull(pr_number)
            files = list(itertools.islice(pr.get_files(), 50))
            additions = sum(getattr(f, "additions", 0) or 0 for f in files)
            deletions = sum(getattr(f, "deletions", 0) or 0 for f in files)
            lines = [
                f"PR #{pr_number}: {pr.title}",
                _change_summary(len(files), additions, deletions),
                "",
                "Files:",
            ]
            for f in files:
                lines.append(f"- {f.filename} (+{getattr(f, 'additions', 0)}/-{getattr(f, 'deletions', 0)})")
            lines.append("")
            for f in files:
                lines.append(f"--- {f.filename} (+{getattr(f, 'additions', 0)}/-{getattr(f, 'deletions', 0)}) ---")
                patch = _clip_patch(getattr(f, "patch", None))
                if patch:
                    lines.append(patch)
            result = "\n".join(lines)

        elif tool_name == "get_pr_comments":
            pr_number = int(tool_input.get("pr_number"))
            pr = r.get_pull(pr_number)
            issue_lines, hidden_issue = _comment_lines(pr.get_issue_comments(), located=False)
            review_lines, hidden_review = _comment_lines(pr.get_review_comments(), located=True)
            # Cap the combined list at 30, keeping the newest (end) of each source.
            combined = issue_lines + review_lines
            hidden = hidden_issue + hidden_review
            if len(combined) > 30:
                hidden += len(combined) - 30
                combined = combined[-30:]
            if not combined:
                result = (
                    f"No comments on PR #{pr_number}. The description may still "
                    "be on the PR — call get_pr."
                )
            else:
                note = f"\n\n({hidden} older comments not shown)" if hidden else ""
                result = "\n\n".join(combined) + note

        elif tool_name == "list_issues":
            state = tool_input.get("state") or "open"
            limit = _clamp_limit(tool_input.get("limit"), 10, 1, 50)
            issues = r.get_issues(state=state, sort="updated", direction="desc")
            shown = []
            scanned = 0
            for issue in issues:
                scanned += 1
                if getattr(issue, "pull_request", None):
                    continue
                shown.append(issue)
                if len(shown) >= limit or scanned >= 200:
                    break
            if not shown:
                if state == "all":
                    result = "No issues found."
                else:
                    result = (
                        f'No {state} issues. Call list_issues with "state": "all" '
                        "to include closed issues."
                    )
            else:
                lines = [_work_item_line(issue) for issue in shown]
                total = _total_count(issues, len(shown))
                # totalCount includes pull requests, so only advertise a remainder
                # when the source itself reports more rows than we returned.
                result = "\n".join(lines) + _more_footer(len(shown), total, "issues")

        elif tool_name == "get_issue":
            issue_number = int(tool_input.get("issue_number"))
            issue = r.get_issue(issue_number)
            result = _format_issue_detail(issue, issue_number, owner, repo)

        elif tool_name == "get_issue_comments":
            issue_number = int(tool_input.get("issue_number"))
            issue = r.get_issue(issue_number)
            lines, hidden = _comment_lines(issue.get_comments(), located=False)
            if not lines:
                result = (
                    f"No comments on issue #{issue_number}. The description may "
                    "still be on the issue — call get_issue."
                )
            else:
                note = f"\n\n({hidden} older comments not shown)" if hidden else ""
                result = "\n\n".join(lines) + note

        elif tool_name == "list_branches":
            limit = _clamp_limit(tool_input.get("limit"), 30, 1, 100)
            default_branch = r.default_branch
            branches = r.get_branches()
            shown = list(itertools.islice(branches, limit))
            if not shown:
                result = "No branches found. Call list_repos to confirm the repository."
            else:
                lines = [f"Branches in {owner}/{repo}:", ""]
                for branch in shown:
                    name = getattr(branch, "name", None) or str(branch)
                    mark = " (default)" if name == default_branch else ""
                    lines.append(f"{name}{mark}")
                total = _total_count(branches, len(shown))
                result = "\n".join(lines) + _more_footer(len(shown), total, "branches")

        elif tool_name == "list_commits":
            limit = _clamp_limit(tool_input.get("limit"), 10, 1, 30)
            branch = _read_ref(tool_input.get("branch"), key="branch") or r.default_branch
            path = (tool_input.get("path") or "").strip().lstrip("/")
            kwargs = {"sha": branch}
            if path:
                kwargs["path"] = path
            commits = r.get_commits(**kwargs)
            shown = list(itertools.islice(commits, limit))
            if not shown:
                result = (
                    f"No commits on '{branch}'. Call list_branches to see valid branch names."
                )
            else:
                lines = [f"Commits on {branch} in {owner}/{repo}:", ""]
                for commit in shown:
                    lines.append(_commit_line(commit))
                total = _total_count(commits, len(shown))
                result = "\n".join(lines) + _more_footer(len(shown), total, "commits")

        elif tool_name == "list_files":
            ref = _read_ref(tool_input.get("ref")) or r.default_branch
            path_prefix = tool_input.get("path_prefix") or ""
            _log("github_context_list_files", ref=ref, path_prefix=path_prefix)
            tree = _get_cached_tree(r, ref, owner, repo, _log)
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
                lines.append(
                    "(no files matched). Try a shorter path_prefix, or omit it to list the repo."
                )
            result = "\n".join(lines)

        elif tool_name == "read_file":
            path = (tool_input.get("path") or "").strip().lstrip("/")
            if not path:
                _log("github_context_read_file_missing_path")
                return gh_error("missing_arg", tool="read_file", arg="path")
            ref = _read_ref(tool_input.get("ref")) or r.default_branch
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
                            numbered = f"{i + 1:>5}| {line}"
                            if used + len(numbered) + 1 > _READ_FILE_MAX_CHARS and chunk_lines:
                                break
                            chunk_lines.append(numbered)
                            used += len(numbered) + 1
                            end_line = i + 1
                        header = f"File: {path} @ {ref} ({total_lines} lines, {contents.size} bytes) — showing lines {start_line}-{end_line}"
                        body = "\n".join(chunk_lines)
                        footer = ""
                        if end_line < total_lines:
                            footer = (
                                f"\n\n[TRUNCATED — {total_lines - end_line} more lines. "
                                f"Call read_file again with start_line={end_line + 1} to continue. "
                                f'Next: read_file with {{"path": "{path}", "start_line": {end_line + 1}}}]'
                            )
                        _log("github_context_read_file_ok", path=path, total_lines=total_lines, shown_from=start_line, shown_to=end_line)
                        result = f"{header}\n\n{body}{footer}"

        elif tool_name == "search_code":
            query = (tool_input.get("query") or "").strip()
            if not query:
                _log("github_context_search_code_missing_query")
                return gh_error("missing_arg", tool="search_code", arg="query")
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
                try:
                    ref = _read_ref(tool_input.get("ref")) or r.default_branch
                    _log("github_context_search_code_zero_hits_fallback_trigger", query=query, ref=ref)
                    fallback_text = _search_code_fallback_scan(r, ref, query, _log, owner=owner, repo=repo)
                    if fallback_text:
                        lines.append(fallback_text)
                except Exception as e:
                    # Fallback must never break the tool call — search_code
                    # still returns its original (if unhelpful) result.
                    _log("github_context_search_code_fallback_unexpected_error", error=str(e))
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
                return gh_error("missing_arg", tool="push_files", arg="branch")
            if not message:
                _log("github_context_push_files_missing_message")
                return gh_error("missing_arg", tool="push_files", arg="message")
            if not files or not isinstance(files, list):
                _log("github_context_push_files_no_files")
                return gh_error("missing_arg", tool="push_files", arg="files")

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
            _blocked_default = default_branch_push_block(branch, default_branch)
            if _blocked_default:
                _log("github_context_push_files_default_branch_blocked", branch=branch)
                return _blocked_default
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
            if not base or not head:
                return gh_error("missing_arg", tool="diff_branches", arg="base" if not base else "head")
            comparison = r.compare(base, head)
            files = list(itertools.islice(getattr(comparison, "files", None) or [], 50))
            additions = sum(getattr(f, "additions", 0) or 0 for f in files)
            deletions = sum(getattr(f, "deletions", 0) or 0 for f in files)
            lines = [
                f"Comparing {base}...{head}: {comparison.ahead_by} ahead, {comparison.behind_by} behind",
                _change_summary(len(files), additions, deletions),
                "",
                "Files:",
            ]
            for f in files:
                lines.append(f"- {f.filename} (+{getattr(f, 'additions', 0)}/-{getattr(f, 'deletions', 0)})")
            lines.append("")
            for f in files:
                lines.append(f"--- {f.filename} (+{getattr(f, 'additions', 0)}/-{getattr(f, 'deletions', 0)}) ---")
                patch = _clip_patch(getattr(f, "patch", None))
                if patch:
                    lines.append(patch)
            result = "\n".join(lines)

        else:
            result = gh_error("unknown_tool", name=tool_name)

        _log("github_context_tool_ok", tool_name=tool_name, result_len=len(result))
        from services.github_natural_tag import shape_github_result
        return shape_github_result(result, 16000)

    except Exception as e:
        _log("github_context_tool_error", tool_name=tool_name, error=str(e))
        repo_obj = locals().get("r")
        msg = _classify_github_exception(tool_name, tool_input, e, repo_obj=repo_obj, _log=_log)
        if _used_session_ref and _working_branch_missing(e, tool_name):
            msg = (
                f"[The working branch '{_session_ref_name}' was not found. "
                "Call list_branches.]"
            )
        return msg
