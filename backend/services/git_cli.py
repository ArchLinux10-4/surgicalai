"""Git and pull-request commands for the folder open in this chat.

The model sends one command inside <git_cli>, or the git_cli function.
The server classifies it. Reads, and local edits on a branch that is not
the default, run immediately. Push, pull requests, and a commit on the
default branch wait for an approval card. Commands that delete work or
rewrite history are refused.

Nothing here opens a shell. The GitHub token is placed only in the
environment of the git child, and the result text is scrubbed of it.
Hooks are left enabled. On a Linux Docker host, files git creates in a
bind mount are owned by the container user. Docker Desktop on macOS maps
that back to the host user. A hook that needs a tool the image does not
have fails in the tool result. There is no bypass.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time

_FORBIDDEN = set(";|&`$<>")
_OUTPUT_CAP = 32_000
_TRUNC_NOTE = "[output truncated at 32000 characters]"
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_APPROVAL_CARDS = 4
_NETWORK_TIMEOUT = 90
_LOCAL_TIMEOUT = 30

ASK_PLAN_STOP_NOTE = "Do not call git again this turn. Answer the user now."
ASK_PLAN_MUTATION_NOTE = (
    "Git commands that change the repository are not available in Ask or Plan mode. "
    "Describe the command, or tell them to switch to Edit or Agent mode."
)
REJECT_NOTE = "The user rejected this command."
TIMEOUT_NOTE = "The approval request timed out. Ask the user what to do."
NO_INBOX_NOTE = (
    "Git command was not run because this connection cannot ask the user to approve it."
)
DESTRUCTIVE_NOTE = (
    "This git command can delete work or rewrite history, so it was not run. "
    "Run it yourself in a terminal if you still want it."
)
UNKNOWN_NOTE = (
    "That git command is not available. "
    "Use status, diff, log, branch, switch, add, commit, push, pull, fetch, or gh pr."
)
MERGE_EDIT_NOTE = "Merge commands cannot be edited. Reject this and send a new command."

_LEVEL_RANK = {"read": 0, "local": 1, "remote": 2, "merge": 3}
_READ_CMDS = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "fetch"}
_DESTRUCTIVE_CMDS = {
    "reset", "clean", "rebase", "filter-branch", "filter-repo", "gc", "submodule",
}
_DANGEROUS_FLAGS = {
    "--force", "--force-with-lease", "--force-if-includes",
    "--no-verify", "--no-gpg-sign",
    "--git-dir", "--work-tree", "--exec-path", "--upload-pack", "--receive-pack",
    "--exec", "--output",
}
_SECRET_DIRS = {"node_modules", ".surgicalai"}


def git_agent_tools_enabled() -> bool:
    """True when the git_cli tool should be offered. Env or the settings row."""
    try:
        from database import get_setting
        raw = get_setting("git_agent_tools", "false")
    except Exception:
        raw = os.environ.get("GIT_AGENT_TOOLS", "")
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def allow_default_push() -> bool:
    try:
        from database import get_setting
        raw = get_setting("git_agent_allow_default_push", "false")
    except Exception:
        raw = os.environ.get("GIT_AGENT_ALLOW_DEFAULT_PUSH", "")
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def git_agent_guidance() -> str:
    """Prompt text for Claude, GPT, and Gemini. Grok gets the function form."""
    from services.agent_tool_spec import git_cli_text
    return git_cli_text("tag")


def normalize_git_command(command: str) -> str:
    """Join backslash continuations. A real newline that is not one stays."""
    text = (command or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    i = 0
    quote = ""
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\":
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if nxt == "\n" or nxt == "" or nxt in " \t":
                i += 1
                while i < len(text) and text[i] in " \t\n":
                    i += 1
                out.append(" ")
                continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _argv(command: str) -> list[str]:
    try:
        return shlex.split(command or "")
    except ValueError:
        return []


def _reject_syntax(command: str) -> str | None:
    raw = command or ""
    if not raw.strip():
        return "Command is empty."
    if "\n" in raw or "\r" in raw or any(ch in raw for ch in _FORBIDDEN):
        return "Only a single git or gh command is allowed."
    try:
        argv = shlex.split(raw)
    except ValueError:
        return "Command could not be parsed."
    if not argv or argv[0] not in ("git", "gh"):
        return "Command must start with git or gh."
    return None


def _traverses(token: str) -> bool:
    norm = (token or "").replace("\\", "/")
    return ".." in norm.split("/")


def _absolute_outside(token: str, root: str) -> bool:
    if not token or not os.path.isabs(token):
        return False
    if not root:
        return True
    real = os.path.realpath(token)
    base = os.path.realpath(root)
    return real != base and not real.startswith(base + os.sep)


def _bad_path(token: str, root: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if _traverses(token):
        return True
    if _absolute_outside(token, root):
        return True
    return False


def _flag_name(token: str) -> str:
    if "=" in token:
        return token.split("=", 1)[0]
    return token


def _flag_value(argv: list[str], *names: str) -> str:
    """The value of the first matching flag. ``-c name`` and ``-c=name`` both count."""
    wanted = set(names)
    for i, tok in enumerate(argv):
        name = _flag_name(tok)
        if name in wanted and "=" in tok:
            return tok.split("=", 1)[1].strip()
        if tok in wanted and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            return argv[i + 1]
    return ""


def _dangerous_flag(argv: list[str]) -> str | None:
    for tok in argv[1:]:
        name = _flag_name(tok)
        if name in _DANGEROUS_FLAGS or name.startswith("--force"):
            return name
        if name in ("-c", "-C") and argv[1] == tok:
            return name
    if len(argv) > 1 and argv[1].startswith("-"):
        return argv[1]
    return None


def _has(argv: list[str], *names: str) -> bool:
    wanted = set(names)
    return any(_flag_name(tok) in wanted for tok in argv)


def _positionals(argv: list[str], value_flags: set[str]) -> list[str]:
    """Arguments that are not flags. A value flag consumes the next token."""
    out = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        name = _flag_name(tok)
        if tok == "--":
            out.extend(argv[i + 1:])
            break
        if tok.startswith("-") and "=" not in tok and name in value_flags:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _refspec_problem(spec: str) -> str | None:
    if not spec or spec == "HEAD":
        return None
    if spec.startswith("+"):
        return "force"
    if spec.startswith(":") or spec.endswith(":") or ":*" in spec:
        return "delete"
    if "*" in spec:
        return "wildcard"
    return None


def _dest_of(spec: str, head: str) -> str:
    if not spec or spec == "HEAD":
        return head or ""
    if ":" in spec:
        return spec.split(":", 1)[1]
    return spec


def _blank(command: str, note: str, argv: list[str] | None = None) -> dict:
    return {
        "command": command,
        "level": "",
        "title": "",
        "target": "",
        "note": note,
        "argv": argv or [],
        "kind": "",
        "action": "",
    }


def _ok(command: str, level: str, title: str, target: str, argv: list[str], kind: str, action: str) -> dict:
    return {
        "command": command,
        "level": level,
        "title": title,
        "target": target,
        "note": "",
        "argv": argv,
        "kind": kind,
        "action": action,
    }


def _classify_gh(command: str, argv: list[str]) -> dict:
    if len(argv) < 3 or argv[1] != "pr":
        return _blank(command, UNKNOWN_NOTE, argv)
    action = argv[2]
    pos, flags, err = _gh_flags(argv[3:])
    if err:
        return _blank(command, err, argv)
    if action == "create":
        if not (flags.get("--title") or "").strip():
            return _blank(command, "gh pr create needs --title.", argv)
        return _ok(command, "remote", "Open a pull request", flags.get("--base") or "", argv, "gh", "pr_create")
    if action == "view":
        target = pos[0] if pos else ""
        if len(pos) > 1:
            return _blank(command, UNKNOWN_NOTE, argv)
        return _ok(command, "read", "View a pull request", target, argv, "gh", "pr_view")
    if action == "list":
        state = flags.get("--state") or "open"
        if state not in ("open", "closed", "all"):
            return _blank(command, "gh pr list --state must be open, closed, or all.", argv)
        if pos:
            return _blank(command, UNKNOWN_NOTE, argv)
        return _ok(command, "read", "List pull requests", state, argv, "gh", "pr_list")
    if action == "checks":
        if len(pos) != 1 or not str(pos[0]).isdigit():
            return _blank(command, "gh pr checks needs a pull request number.", argv)
        return _ok(command, "read", "Pull request checks", pos[0], argv, "gh", "pr_checks")
    if action == "ready":
        if len(pos) != 1 or not str(pos[0]).isdigit():
            return _blank(command, "gh pr ready needs a pull request number.", argv)
        return _ok(command, "remote", f"Mark pull request #{pos[0]} ready", pos[0], argv, "gh", "pr_ready")
    if action == "merge":
        if len(pos) != 1 or not str(pos[0]).isdigit():
            return _blank(command, "gh pr merge needs a pull request number.", argv)
        styles = [name for name in ("--merge", "--squash", "--rebase") if flags.get(name)]
        if len(styles) > 1:
            return _blank(command, "Choose one of --merge, --squash, or --rebase.", argv)
        return _ok(command, "merge", f"Merge pull request #{pos[0]}", pos[0], argv, "gh", "pr_merge")
    return _blank(command, UNKNOWN_NOTE, argv)


def _gh_flags(argv: list[str]) -> tuple[list[str], dict, str]:
    bools = {"--draft", "--merge", "--squash", "--rebase"}
    values = {"--title", "--body", "--base", "--head", "--state"}
    positionals: list[str] = []
    flags: dict = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in bools:
            flags[tok] = True
            i += 1
            continue
        name = _flag_name(tok)
        if name in values and "=" in tok:
            flags[name] = tok.split("=", 1)[1]
            i += 1
            continue
        if tok in values:
            if i + 1 >= len(argv):
                return [], {}, f"{tok} needs a value."
            flags[tok] = argv[i + 1]
            i += 2
            continue
        if tok.startswith("-"):
            return [], {}, f"Unknown flag {tok}."
        positionals.append(tok)
        i += 1
    return positionals, flags, ""


def _classify_git(command: str, argv: list[str], root: str, head: str, default_branch: str, allow_default: bool) -> dict:
    if len(argv) < 2:
        return _blank(command, UNKNOWN_NOTE, argv)
    cmd = argv[1]
    rest = argv[2:]
    if any(_bad_path(tok, root) for tok in rest):
        return _blank(command, "That path is outside the open folder.", argv)
    if any("://" in tok or tok.startswith("git@") for tok in rest):
        return _blank(command, "Only the remote name origin is allowed.", argv)

    if cmd in _DESTRUCTIVE_CMDS or cmd in ("config", "tag", "reflog", "cherry-pick", "revert", "rm", "mv", "am"):
        if cmd in _DESTRUCTIVE_CMDS or cmd in ("reset", "clean", "rebase"):
            return _blank(command, DESTRUCTIVE_NOTE, argv)
        if cmd == "config":
            return _blank(command, DESTRUCTIVE_NOTE, argv)
        return _blank(command, UNKNOWN_NOTE, argv)

    if cmd in _READ_CMDS:
        if cmd == "fetch" and not _fetch_ok(rest):
            return _blank(command, "git fetch can only fetch origin.", argv)
        return _ok(command, "read", cmd, "", argv, "git", cmd)

    if cmd == "remote":
        return _classify_remote(command, argv, rest)
    if cmd == "branch":
        return _classify_branch(command, argv, rest)
    if cmd == "switch":
        return _classify_switch(command, argv, rest)
    if cmd == "checkout":
        return _classify_checkout(command, argv, rest)
    if cmd == "add":
        if _has(rest, "-p", "--patch", "-i", "--interactive", "--pathspec-from-file"):
            return _blank(command, DESTRUCTIVE_NOTE, argv)
        return _ok(command, "local", "Stage files", "", argv, "git", "add")
    if cmd == "restore":
        staged = _has(rest, "--staged", "-S")
        worktree = _has(rest, "--worktree", "-W") or not staged
        if worktree:
            return _blank(command, DESTRUCTIVE_NOTE, argv)
        return _ok(command, "local", "Unstage files", "", argv, "git", "restore")
    if cmd == "commit":
        if _has(rest, "--amend", "-C", "-c", "-F", "--file"):
            return _blank(command, DESTRUCTIVE_NOTE, argv)
        if not _commit_message(rest):
            return _blank(command, "git commit needs -m or --message so it does not open an editor.", argv)
        if head and default_branch and head == default_branch:
            return _ok(command, "remote", f"Commit on {head}", head, argv, "git", "commit")
        if not head:
            return _ok(command, "remote", "Commit on a detached HEAD", "", argv, "git", "commit")
        return _ok(command, "local", "Commit", head, argv, "git", "commit")
    if cmd == "stash":
        return _classify_stash(command, argv, rest)
    if cmd == "merge":
        if "--abort" in rest:
            return _ok(command, "local", "Abort the merge", "", argv, "git", "merge_abort")
        if _has(rest, "--continue"):
            return _ok(command, "local", "Continue the merge", "", argv, "git", "merge_continue")
        branch = next((tok for tok in _positionals(rest, set()) if tok != "--abort"), "")
        if not branch:
            return _blank(command, "git merge needs a branch, or --abort.", argv)
        return _ok(command, "merge", f"Merge {branch} into {head or 'HEAD'}", branch, argv, "git", "merge")
    if cmd == "pull":
        if _has(rest, "--rebase"):
            return _blank(command, DESTRUCTIVE_NOTE, argv)
        if not _remote_only_origin(rest):
            return _blank(command, "git pull can only use origin.", argv)
        if _has(rest, "--no-ff"):
            return _ok(command, "merge", "Pull without fast-forward", "", argv, "git", "pull")
        return _ok(command, "local", "Pull (fast-forward only)", "", argv, "git", "pull")
    if cmd == "push":
        return _classify_push(command, argv, rest, head, default_branch, allow_default)
    return _blank(command, UNKNOWN_NOTE, argv)


def _fetch_ok(rest: list[str]) -> bool:
    if _has(rest, "--all", "--prune", "-p", "--tags", "--mirror"):
        return False
    pos = _positionals(rest, set())
    if not pos:
        return True
    return pos[0] == "origin"


def _remote_only_origin(rest: list[str]) -> bool:
    pos = [tok for tok in _positionals(rest, set()) if tok]
    if not pos:
        return True
    return pos[0] == "origin"


def _classify_remote(command: str, argv: list[str], rest: list[str]) -> dict:
    if any(tok in ("add", "set-url", "remove", "rename", "rm") for tok in rest):
        return _blank(command, DESTRUCTIVE_NOTE, argv)
    pos = _positionals(rest, set())
    if not pos or pos == ["-v"] or set(rest).issubset({"-v", "--verbose"}):
        return _ok(command, "read", "remote", "", argv, "git", "remote")
    if pos[:1] == ["get-url"] and (len(pos) == 1 or pos[1] == "origin"):
        return _ok(command, "read", "remote", "origin", argv, "git", "remote")
    return _blank(command, UNKNOWN_NOTE, argv)


def _classify_branch(command: str, argv: list[str], rest: list[str]) -> dict:
    if _has(rest, "-d", "-D", "--delete", "-m", "-M", "--move", "--force"):
        return _blank(command, DESTRUCTIVE_NOTE, argv)
    pos = _positionals(rest, {"-u", "--set-upstream-to"})
    list_flags = {"-v", "-vv", "-a", "-r", "--list", "--show-current", "--verbose"}
    if not pos and (not rest or set(_flag_name(t) for t in rest).issubset(list_flags)):
        return _ok(command, "read", "branch", "", argv, "git", "branch")
    if len(pos) == 1 and not pos[0].startswith("-"):
        return _ok(command, "local", f"Create branch {pos[0]}", pos[0], argv, "git", "branch")
    return _blank(command, UNKNOWN_NOTE, argv)


def _classify_switch(command: str, argv: list[str], rest: list[str]) -> dict:
    if _has(rest, "--discard-changes", "-f", "--force", "--detach", "--orphan", "-C"):
        return _blank(command, DESTRUCTIVE_NOTE, argv)
    if _has(rest, "-c", "--create"):
        name = _flag_value(rest, "-c", "--create")
        if not name:
            return _blank(command, "git switch -c needs a branch name.", argv)
        return _ok(command, "local", f"Create and switch to {name}", name, argv, "git", "switch")
    pos = _positionals(rest, {"-c", "--create"})
    if len(pos) == 1:
        return _ok(command, "local", f"Switch to {pos[0]}", pos[0], argv, "git", "switch")
    return _blank(command, UNKNOWN_NOTE, argv)


def _classify_checkout(command: str, argv: list[str], rest: list[str]) -> dict:
    # Only `checkout -b` is a branch create. Every other form can overwrite files.
    if "--" in rest or _has(rest, "-f", "--force", "-B") or not _has(rest, "-b"):
        return _blank(command, DESTRUCTIVE_NOTE, argv)
    name = _flag_value(rest, "-b")
    if not name:
        return _blank(command, "git checkout -b needs a branch name.", argv)
    return _ok(command, "local", f"Create and switch to {name}", name, argv, "git", "checkout")


def _classify_stash(command: str, argv: list[str], rest: list[str]) -> dict:
    if any(tok in ("drop", "clear") for tok in rest):
        return _blank(command, DESTRUCTIVE_NOTE, argv)
    action = next((tok for tok in rest if not tok.startswith("-")), "push")
    if action in ("push", "pop", "apply") or not rest:
        return _ok(command, "local", f"Stash {action if rest else 'push'}", "", argv, "git", "stash")
    return _blank(command, UNKNOWN_NOTE, argv)


def _classify_push(command: str, argv: list[str], rest: list[str], head: str, default_branch: str, allow_default: bool) -> dict:
    if _has(rest, "-f", "--force", "--force-with-lease", "--force-if-includes", "--delete", "--mirror", "--all", "--tags", "--follow-tags"):
        return _blank(command, DESTRUCTIVE_NOTE, argv)
    pos = _positionals(rest, {"-o", "--push-option"})
    if any(_refspec_problem(tok) for tok in pos):
        return _blank(command, DESTRUCTIVE_NOTE, argv)
    remote = ""
    specs = list(pos)
    if specs and specs[0] == "origin":
        remote = "origin"
        specs = specs[1:]
    elif specs and ":" not in specs[0] and specs[0] != "HEAD":
        return _blank(command, "Only the remote name origin is allowed.", argv)
    else:
        remote = "origin"
    if not specs:
        dests = [head or ""]
    else:
        dests = [_dest_of(spec, head) for spec in specs]
    if any(not d for d in dests):
        return _blank(command, "git push needs a branch name when HEAD is detached.", argv)
    default_hit = any(d == default_branch or d == f"refs/heads/{default_branch}" for d in dests)
    target = "origin/" + dests[0]
    if default_hit and not allow_default:
        return _blank(
            command,
            "Pushing to the default branch is blocked. Create a branch, push that, and open a pull request.",
            argv,
        )
    if default_hit:
        return _ok(command, "merge", f"Push {dests[0]} to the default branch", target, argv, "git", "push")
    return _ok(command, "remote", f"Push {dests[0]}", target, argv, "git", "push")


def _commit_message(rest: list[str]) -> str:
    for i, tok in enumerate(rest):
        if tok in ("-m", "--message") and i + 1 < len(rest):
            return rest[i + 1].strip()
        if tok.startswith("--message="):
            return tok.split("=", 1)[1].strip()
        if tok in ("-am",) and i + 1 < len(rest):
            return rest[i + 1].strip()
    return ""


def classify(command: str, *, root: str = "", head_branch: str = "", default_branch: str = "main", allow_default_push: bool = False) -> dict:
    """Level for one command. No I/O. ``note`` set means do not run it."""
    command = normalize_git_command(command)
    syntax = _reject_syntax(command)
    argv = _argv(command)
    if syntax:
        return _blank(command, syntax, argv)
    danger = _dangerous_flag(argv)
    if danger:
        return _blank(command, DESTRUCTIVE_NOTE, argv)
    if argv[0] == "gh":
        return _classify_gh(command, argv)
    return _classify_git(command, argv, root, head_branch, default_branch or "main", allow_default_push)


def _ask_plan(mode: str) -> bool:
    return (mode or "").strip().lower() in ("ask", "plan")


def level_is_worse(old: str, new: str) -> bool:
    return _LEVEL_RANK.get(new, 1) > _LEVEL_RANK.get(old, 1)


def begin_git_call(
    command: str,
    mode: str,
    calls_so_far: int,
    *,
    root: str = "",
    head_branch: str = "",
    default_branch: str = "main",
    allow_default: bool | None = None,
    summary: str = "",
) -> dict:
    """Decide whether a command runs, is refused, or needs a card. No network."""
    if allow_default is None:
        allow_default = allow_default_push()
    classified = classify(
        command,
        root=root,
        head_branch=head_branch,
        default_branch=default_branch,
        allow_default_push=bool(allow_default),
    )
    state = {
        "command": classified["command"],
        "level": classified["level"] or "",
        "title": classified["title"],
        "target": classified["target"],
        "summary": summary or "",
        "needs_approval": False,
        "run": False,
        "note": classified["note"],
        "mode": (mode or "").strip().lower(),
        "action": classified.get("action") or "",
    }
    if state["note"]:
        return state
    if _ask_plan(mode):
        if calls_so_far >= 3:
            state["note"] = ASK_PLAN_STOP_NOTE
            return state
        if state["level"] != "read":
            state["note"] = ASK_PLAN_MUTATION_NOTE
            return state
        state["run"] = True
        return state
    if state["level"] in ("read", "local"):
        state["run"] = True
        return state
    state["needs_approval"] = True
    if not state["summary"] and root and state["level"] in ("remote", "merge"):
        state["summary"] = change_summary(root, default_branch, head_branch)
    return state


def _git(root: str, args: list[str], timeout: int = 20, env: dict | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [_git_bin(), *args],
            cwd=root or None,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=env or _plain_env(),
        )
        return proc.returncode, proc.stdout or "", (proc.stderr or "")
    except Exception as exc:
        return 1, "", str(exc)


def _git_bin() -> str:
    return shutil.which("git") or "git"


def _plain_env() -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return env


def _head_branch(root: str) -> str:
    if not root:
        return ""
    code, out, _err = _git(root, ["branch", "--show-current"])
    return out.strip() if code == 0 else ""


def _default_branch(root: str, repo: dict | None) -> str:
    if root:
        code, out, _err = _git(root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        if code == 0 and out.strip():
            name = out.strip().split("/")[-1]
            if name:
                return name
    stored = ((repo or {}).get("default_branch") or "").strip()
    return stored or "main"


def _origin_url(root: str) -> str:
    code, out, _err = _git(root, ["remote", "get-url", "origin"])
    return out.strip() if code == 0 else ""


def _parse_user_config(path: str) -> tuple[str, str]:
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return "", ""
    name = ""
    email = ""
    in_user = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_user = stripped.lower() == "[user]"
            continue
        if not in_user or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == "name":
            name = value.strip()
        elif key.strip() == "email":
            email = value.strip()
    return name, email


def _gitconfig_path() -> str:
    explicit = (os.environ.get("SURGICALAI_GITCONFIG") or "").strip()
    if explicit:
        return explicit
    browse = (os.environ.get("SURGICALAI_BROWSE_ROOT") or "").strip()
    if browse:
        return os.path.join(browse, ".gitconfig")
    return os.path.expanduser("~/.gitconfig")


def read_identity(root: str, fallback_login: str = "") -> tuple[str, str]:
    name, email = "", ""
    if root:
        code, out, _err = _git(root, ["config", "--local", "--get", "user.name"])
        if code == 0:
            name = out.strip()
        code, out, _err = _git(root, ["config", "--local", "--get", "user.email"])
        if code == 0:
            email = out.strip()
    if not name or not email:
        file_name, file_email = _parse_user_config(_gitconfig_path())
        name = name or file_name
        email = email or file_email
    login = (fallback_login or "").strip()
    if not name:
        name = login or "SurgicalAI"
    if not email:
        who = login or "surgicalai"
        email = f"{who}@users.noreply.github.com"
    return name, email


def build_child_env(root: str, *, network_token: str = "", login: str = "") -> dict:
    """Environment for the git child. The server process environment is not copied.

    ``network_token`` is sent only as an http extraheader. It is never written
    to a config file.
    """
    tmp = tempfile.mkdtemp(prefix="gitcli-")
    empty = os.path.join(tmp, "gitconfig")
    with open(empty, "w", encoding="utf-8") as fh:
        fh.write("")
    name, email = read_identity(root, login)
    env = _plain_env()
    env["HOME"] = tmp
    env["GIT_CONFIG_GLOBAL"] = empty
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = name
    env["GIT_COMMITTER_EMAIL"] = email
    if network_token:
        basic = base64.b64encode(f"x-access-token:{network_token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    env["_surgicalai_tmp"] = tmp
    return env


def _cleanup_env(env: dict | None) -> None:
    if not env:
        return
    tmp = env.pop("_surgicalai_tmp", "")
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


def _redact(text: str, secrets: list[str]) -> str:
    out = text or ""
    for secret in secrets:
        if secret and secret in out:
            out = out.replace(secret, "[redacted]")
    if len(out) > _OUTPUT_CAP:
        out = out[:_OUTPUT_CAP] + "\n" + _TRUNC_NOTE
    return out


def _repo_label(root: str, repo: dict | None) -> str:
    owner = ((repo or {}).get("github_owner") or "").strip()
    name = ((repo or {}).get("github_repo") or "").strip()
    if owner and name:
        return f"{owner}/{name}"
    url = _origin_url(root) if root else ""
    parsed = _parse_remote(url) if url else None
    if parsed:
        return f"{parsed['owner']}/{parsed['repo']}"
    return "origin"


def _parse_remote(url: str):
    from services.repositories import parse_git_remote
    return parse_git_remote(url or "")


def remote_allowed(url: str, user_id: str, repo: dict | None) -> str | None:
    """None when origin may be used. A message when it must not."""
    raw = (url or "").strip()
    if os.environ.get("GIT_AGENT_ALLOW_FILE_REMOTE") == "1" and (
        raw.startswith("/") or raw.startswith("file://")
    ):
        return None
    parsed = _parse_remote(raw)
    if not parsed or parsed.get("host") not in ("github.com", "www.github.com"):
        return "origin is not a GitHub repository this account can update."
    owner = (parsed.get("owner") or "")
    name = (parsed.get("repo") or "")
    linked_owner = ((repo or {}).get("github_owner") or "").strip()
    linked_repo = ((repo or {}).get("github_repo") or "").strip()
    if linked_owner and linked_repo:
        if owner.lower() == linked_owner.lower() and name.lower() == linked_repo.lower():
            return None
        return "origin does not match the GitHub repo linked to this folder."
    try:
        from database import list_github_app_installations
        installs = list_github_app_installations(user_id) or []
    except Exception:
        installs = []
    if any((i.get("account_login") or "").lower() == owner.lower() for i in installs):
        return None
    return "No GitHub connection for this origin."


def _token_from_client(client) -> str:
    requester = getattr(client, "_Github__requester", None)
    auth = getattr(requester, "auth", None) if requester is not None else None
    token = getattr(auth, "token", "") if auth is not None else ""
    if callable(token):
        token = token()
    return str(token or "")


def open_github_repo(user_id: str, owner: str, repo_name: str):
    """(repo, installation, token, error). Tests replace this."""
    try:
        from services.github_context_tools import _find_client_for_repo
    except Exception as exc:
        return None, None, "", str(exc)

    def _log(event, **_kw):
        return None

    client, inst, err = _find_client_for_repo(user_id, owner, repo_name, _log)
    if err:
        return None, None, "", err
    try:
        gh_repo = client.get_repo(f"{owner}/{repo_name}")
    except Exception as exc:
        return None, inst, "", str(exc)
    return gh_repo, inst, _token_from_client(client), ""


def _github_coords(root: str, repo: dict | None) -> tuple[str, str]:
    owner = ((repo or {}).get("github_owner") or "").strip()
    name = ((repo or {}).get("github_repo") or "").strip()
    if owner and name:
        return owner, name
    parsed = _parse_remote(_origin_url(root))
    if not parsed:
        return "", ""
    return parsed.get("owner") or "", parsed.get("repo") or ""


def _secret_path(rel: str) -> bool:
    norm = (rel or "").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    parts = [p for p in norm.split("/") if p]
    if any(p in _SECRET_DIRS for p in parts):
        return True
    base = (parts[-1] if parts else "").lower()
    if base == ".env" or base.startswith(".env."):
        return True
    if base.endswith((".pem", ".db", ".sqlite", ".sqlite3")):
        return True
    if base == "id_rsa" or base.startswith("id_rsa."):
        return True
    return False


def commit_block_reason(root: str) -> str:
    """Refuse the commit when the index holds a secret or a huge file."""
    code, out, err = _git(root, ["diff", "--cached", "--name-only", "-z"])
    if code != 0:
        return err.strip() or "Could not read the staged files."
    names = [n for n in out.split("\0") if n]
    blocked = [n for n in names if _secret_path(n)]
    huge = []
    for name in names:
        size_code, size_out, _size_err = _git(root, ["cat-file", "-s", f":{name}"])
        if size_code != 0:
            continue
        try:
            size = int((size_out or "").strip() or "0")
        except ValueError:
            continue
        if size > _MAX_FILE_BYTES:
            huge.append(name)
    if not blocked and not huge:
        return ""
    lines = ["Commit blocked."]
    if blocked:
        lines.append("These files look like secrets or local data: " + ", ".join(blocked))
    if huge:
        lines.append("These files are larger than 5 MB: " + ", ".join(huge))
    lines.append("Unstage them with git restore --staged <path> and commit again.")
    return " ".join(lines)


def session_repo_files(session_id: str) -> list[dict]:
    if not session_id:
        return []
    try:
        from database import get_db_ctx
        with get_db_ctx() as conn:
            rows = conn.execute(
                "SELECT filename, repo_path, content, base_sha FROM session_files "
                "WHERE session_id = ? AND repo_path IS NOT NULL AND repo_path != ''",
                (session_id,),
            ).fetchall()
    except Exception:
        return []
    out = []
    for row in rows:
        if hasattr(row, "keys"):
            out.append({k: row[k] for k in ("filename", "repo_path", "content", "base_sha")})
        else:
            out.append({
                "filename": row[0], "repo_path": row[1], "content": row[2], "base_sha": row[3],
            })
    return out


def unapplied_edits_note(session_id: str, root: str) -> str:
    rows = session_repo_files(session_id)
    if not rows or not root:
        return ""
    try:
        from services.repo_apply import disk_state_for
    except Exception:
        return ""
    names = []
    for row in rows:
        rel = row.get("repo_path") or ""
        state = disk_state_for(rel, root, row.get("content") or "", row.get("base_sha") or "")
        if state == "pending":
            names.append(row.get("filename") or rel)
    if not names:
        return ""
    shown = ", ".join(names[:12])
    return (
        f"{len(names)} edited files are not applied to disk yet: {shown}. "
        "Apply them or they are not part of this commit."
    )


def change_summary(root: str, default_branch: str, head: str) -> str:
    if not root or not head:
        return ""
    base = f"origin/{default_branch}" if default_branch else ""
    commits = ""
    stat = ""
    if base:
        code, out, _err = _git(root, ["rev-parse", "--verify", "--quiet", base])
        if code != 0:
            base = default_branch
        code, out, _err = _git(root, ["log", "--oneline", f"{base}..{head}"])
        if code == 0:
            n = len([ln for ln in out.splitlines() if ln.strip()])
            commits = f"{n} commit(s)"
        code, out, _err = _git(root, ["diff", "--shortstat", f"{base}...{head}"])
        if code == 0:
            stat = (out or "").strip()
    return ", ".join(p for p in (commits, stat) if p)


def _with_ff_only(argv: list[str]) -> list[str]:
    if "--ff-only" in argv or "--no-ff" in argv or "--rebase" in argv:
        return argv
    return [*argv, "--ff-only"]


def _divergence(root: str, upstream: str) -> str:
    code, out, _err = _git(root, ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
    if code != 0:
        return ""
    parts = (out or "").split()
    if len(parts) != 2:
        return ""
    return f"Local is {parts[0]} ahead and {parts[1]} behind {upstream}."


def _conflict_note(root: str) -> str:
    code, out, _err = _git(root, ["diff", "--name-only", "--diff-filter=U"])
    if code != 0:
        return ""
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not names:
        return ""
    return (
        "Merge conflict in: " + ", ".join(names) + ". "
        "Read them with read_repo_file, edit, ask the user to Apply, then git add and git commit. "
        "git merge --abort undoes the merge."
    )


def _run_argv(argv: list[str], root: str, env: dict, timeout: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env={k: v for k, v in env.items() if k != "_surgicalai_tmp"},
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out."
    except Exception as exc:
        return 1, "", str(exc)


def _rewrite_argv(classified: dict) -> list[str]:
    argv = list(classified["argv"])
    if classified.get("action") == "pull" and classified.get("level") == "local":
        return _with_ff_only(argv)
    return argv


def run_git_cli(
    command: str,
    *,
    root: str,
    user_id: str = "",
    session_id: str = "",
    repo: dict | None = None,
    head_branch: str = "",
    default_branch: str = "",
) -> dict:
    """Run one already-classified command. Returns exit_code, stdout, stderr."""
    head = head_branch or _head_branch(root)
    default = default_branch or _default_branch(root, repo)
    classified = classify(
        command,
        root=root,
        head_branch=head,
        default_branch=default,
        allow_default_push=allow_default_push(),
    )
    if classified["note"]:
        return {"exit_code": 1, "stdout": "", "stderr": classified["note"]}
    if not root or not os.path.isdir(root):
        return {"exit_code": 1, "stdout": "", "stderr": "This chat has no open folder."}
    action = classified.get("action") or ""
    if action == "commit":
        blocked = commit_block_reason(root)
        if blocked:
            return {"exit_code": 1, "stdout": "", "stderr": blocked}
    network = action in ("fetch", "push", "pull") or classified.get("kind") == "gh"
    token = ""
    login = ""
    if network and classified.get("kind") == "git":
        url = _origin_url(root)
        problem = remote_allowed(url, user_id, repo)
        if problem:
            return {"exit_code": 1, "stdout": "", "stderr": problem}
        if url.startswith("https://") or url.startswith("http://"):
            owner, name = _github_coords(root, repo)
            _gh_repo, inst, token, err = open_github_repo(user_id, owner, name) if owner and name else (None, None, "", "")
            if action == "push":
                tier = (inst or {}).get("permission_tier") or ""
                if tier != "read_write":
                    return {
                        "exit_code": 1, "stdout": "", "stderr":
                        "Push blocked: this GitHub connection is not read_write.",
                    }
            if not token and not (os.environ.get("GIT_AGENT_ALLOW_FILE_REMOTE") == "1"):
                return {"exit_code": 1, "stdout": "", "stderr": err or "Could not get a GitHub token."}
            login = (inst or {}).get("account_login") or ""
    if classified.get("kind") == "gh":
        return _run_gh(classified, root, user_id, repo, head, default)
    env = build_child_env(root, network_token=token, login=login)
    extra = ""
    if action in ("commit", "push"):
        extra = unapplied_edits_note(session_id, root)
    try:
        argv = _rewrite_argv(classified)
        if not argv or argv[0] != "git":
            return {"exit_code": 1, "stdout": "", "stderr": "Command must start with git."}
        argv = [_git_bin(), *argv[1:]]
        timeout = _NETWORK_TIMEOUT if action in ("fetch", "push", "pull") else _LOCAL_TIMEOUT
        if action in ("status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "remote", "branch"):
            env["GIT_OPTIONAL_LOCKS"] = "0"
        code, stdout, stderr = _run_argv(argv, root, env, timeout)
        if action in ("pull", "merge") and code != 0:
            conflict = _conflict_note(root)
            if conflict:
                stderr = (stderr + "\n" + conflict).strip()
            elif action == "pull":
                upstream = f"origin/{default}" if default else "origin/HEAD"
                diverged = _divergence(root, upstream)
                if diverged:
                    stderr = (
                        stderr + "\n" + diverged + " "
                        "This cannot fast-forward. Propose git merge " + upstream + " to combine the histories."
                    ).strip()
        if extra:
            stdout = (stdout + ("\n" if stdout else "") + extra).strip()
        secrets = [token] if token else []
        return {
            "exit_code": code,
            "stdout": _redact(stdout, secrets),
            "stderr": _redact(stderr, secrets),
        }
    finally:
        _cleanup_env(env)


def _gh_parsed(argv: list[str]) -> tuple[str, list[str], dict]:
    _pos, flags, _err = _gh_flags(argv[3:])
    pos = _pos
    return argv[2], pos, flags


def _run_gh(classified: dict, root: str, user_id: str, repo: dict | None, head: str, default: str) -> dict:
    owner, name = _github_coords(root, repo)
    if not owner or not name:
        return {"exit_code": 1, "stdout": "", "stderr": "This folder is not linked to a GitHub repository."}
    url = _origin_url(root)
    problem = remote_allowed(url, user_id, repo) if url else "origin is not set."
    if problem:
        return {"exit_code": 1, "stdout": "", "stderr": problem}
    gh_repo, inst, token, err = open_github_repo(user_id, owner, name)
    if err or gh_repo is None:
        return {"exit_code": 1, "stdout": "", "stderr": err or "Could not open the GitHub repository."}
    action = classified.get("action")
    if action in ("pr_create", "pr_ready", "pr_merge") and (inst or {}).get("permission_tier") != "read_write":
        return {"exit_code": 1, "stdout": "", "stderr": "This GitHub connection is not read_write."}
    _cmd, pos, flags = _gh_parsed(classified["argv"])
    try:
        if action == "pr_list":
            text = _gh_list(gh_repo, flags.get("--state") or "open")
        elif action == "pr_view":
            text = _gh_view(gh_repo, pos[0] if pos else "", head)
        elif action == "pr_checks":
            text = _gh_checks(gh_repo, int(pos[0]))
        elif action == "pr_ready":
            text = _gh_ready(gh_repo, int(pos[0]))
        elif action == "pr_merge":
            text = _gh_merge(gh_repo, int(pos[0]), flags)
        elif action == "pr_create":
            text = _gh_create(gh_repo, root, flags, head, default, token)
        else:
            text = UNKNOWN_NOTE
            return {"exit_code": 1, "stdout": "", "stderr": text}
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": _redact(str(exc), [token] if token else [])}
    return {"exit_code": 0, "stdout": _redact(text, [token] if token else []), "stderr": ""}


def _gh_list(gh_repo, state: str) -> str:
    lines = []
    for pr in gh_repo.get_pulls(state=state):
        lines.append(
            f"#{pr.number} {getattr(pr, 'title', '')} ({getattr(pr, 'state', '')}) {getattr(pr, 'html_url', '')}"
        )
        if len(lines) >= 20:
            break
    if not lines:
        return f"No {state} pull requests."
    return "\n".join(lines)


def _gh_view(gh_repo, number: str, head: str) -> str:
    if number:
        pr = gh_repo.get_pull(int(number))
    else:
        pr = _pr_for_branch(gh_repo, head)
        if pr is None:
            return f"No open pull request for {head or 'the current branch'}."
    return (
        f"#{pr.number} {pr.title}\n"
        f"state: {getattr(pr, 'state', '')} draft: {getattr(pr, 'draft', False)} "
        f"mergeable: {getattr(pr, 'mergeable_state', '')}\n"
        f"{getattr(pr.head, 'ref', '')} -> {getattr(pr.base, 'ref', '')}\n"
        f"{getattr(pr, 'html_url', '')}"
    )


def _pr_for_branch(gh_repo, head: str):
    if not head:
        return None
    for pr in list(gh_repo.get_pulls(state="open"))[:30]:
        if getattr(getattr(pr, "head", None), "ref", "") == head:
            return pr
    return None


def _gh_checks(gh_repo, number: int) -> str:
    pr = gh_repo.get_pull(number)
    return _check_line(gh_repo, pr)


def _check_line(gh_repo, pr) -> str:
    sha = getattr(getattr(pr, "head", None), "sha", "") or ""
    if not sha:
        return "No head commit."
    try:
        commit = gh_repo.get_commit(sha)
        runs = list(commit.get_check_runs())
    except Exception as exc:
        return f"Could not read checks: {exc}"
    if not runs:
        return f"PR #{pr.number} has no checks."
    failing = []
    pending = []
    for run in runs:
        conclusion = (getattr(run, "conclusion", None) or "") or ""
        status = getattr(run, "status", "") or ""
        label = getattr(run, "name", "") or "check"
        if status != "completed":
            pending.append(label)
        elif conclusion not in ("success", "neutral", "skipped"):
            failing.append(f"{label} ({conclusion or status})")
    if failing:
        return f"Checks failing: {', '.join(failing)}"
    if pending:
        return f"Checks still running: {', '.join(pending)}"
    return f"Checks passing ({len(runs)})."


def pr_merge_preview(user_id: str, owner: str, repo_name: str, number: int) -> tuple[str, str]:
    """(block_reason, summary). block_reason set means do not show an approve button."""
    gh_repo, _inst, _token, err = open_github_repo(user_id, owner, repo_name)
    if err or gh_repo is None:
        return err or "Could not open the GitHub repository.", ""
    try:
        pr = gh_repo.get_pull(int(number))
    except Exception as exc:
        return f"Could not read pull request #{number}: {exc}", ""
    if getattr(pr, "draft", False):
        return f"Pull request #{number} is a draft. Run gh pr ready {number} first.", ""
    state = (getattr(pr, "mergeable_state", None) or "") or ""
    mergeable = getattr(pr, "mergeable", None)
    if mergeable is False or state == "dirty":
        return f"Pull request #{number} has merge conflicts.", ""
    if state == "blocked":
        return f"Pull request #{number} is blocked by failing checks or reviews.", ""
    checks = _check_line(gh_repo, pr)
    if checks.startswith("Checks failing") or checks.startswith("Checks still running"):
        return checks, ""
    base = getattr(getattr(pr, "base", None), "ref", "") or ""
    return "", f"PR #{number} into {base}. {checks}"


def _gh_ready(gh_repo, number: int) -> str:
    pr = gh_repo.get_pull(number)
    requester = getattr(pr, "_requester", None)
    base = getattr(pr, "base", None)
    repo_obj = getattr(base, "repo", None)
    owner = getattr(getattr(repo_obj, "owner", None), "login", "") if repo_obj is not None else ""
    name = getattr(repo_obj, "name", "") if repo_obj is not None else ""
    if requester is None or not owner or not name:
        raise RuntimeError("Could not mark the pull request ready.")
    requester.requestJsonAndCheck("POST", f"/repos/{owner}/{name}/pulls/{number}/ready_for_review")
    return f"Pull request #{number} is ready: {getattr(pr, 'html_url', '')}"


def _gh_merge(gh_repo, number: int, flags: dict) -> str:
    if flags.get("--squash"):
        method = "squash"
    elif flags.get("--rebase"):
        method = "rebase"
    else:
        method = "merge"
    pr = gh_repo.get_pull(number)
    if getattr(pr, "draft", False):
        return f"Pull request #{number} is a draft. Run gh pr ready {number} first."
    state = (getattr(pr, "mergeable_state", None) or "") or ""
    if getattr(pr, "mergeable", None) is False or state in ("dirty", "blocked"):
        raise RuntimeError(f"Pull request #{number} is not mergeable ({state or 'unknown'}).")
    checks = _check_line(gh_repo, pr)
    if checks.startswith("Checks failing") or checks.startswith("Checks still running"):
        raise RuntimeError(checks)
    result = pr.merge(merge_method=method)
    sha = getattr(result, "sha", "") or ""
    url = getattr(pr, "html_url", "")
    return (
        f"Merged pull request #{number} with {method}. {sha}\n{url}\n"
        "To update the local default branch, run git switch <default> and then git pull --ff-only."
    )


def _branch_pushed(root: str, branch: str, token: str) -> str:
    env = build_child_env(root, network_token=token)
    try:
        code, out, err = _run_argv(
            [_git_bin(), "ls-remote", "--heads", "origin", branch],
            root, env, _NETWORK_TIMEOUT,
        )
    finally:
        _cleanup_env(env)
    if code != 0:
        return _redact(err.strip() or "Could not check whether the branch is on origin.", [token] if token else [])
    wanted = f"refs/heads/{branch}"
    found = any(line.split()[-1] == wanted for line in (out or "").splitlines() if line.split())
    if not found:
        return f"Push {branch} before opening a pull request."
    return ""


def _gh_create(gh_repo, root: str, flags: dict, head: str, default: str, token: str) -> str:
    branch = (flags.get("--head") or head or "").strip()
    base = (flags.get("--base") or default or "main").strip()
    if not branch:
        return "gh pr create needs a branch. Switch to one or pass --head."
    if branch == base:
        return "The pull request head is the base branch. Push a different branch."
    missing = _branch_pushed(root, branch, token)
    if missing:
        return missing
    title = flags.get("--title") or ""
    body = flags.get("--body") or ""
    draft = bool(flags.get("--draft"))
    try:
        pr = gh_repo.create_pull(title=title, body=body, base=base, head=branch, draft=draft)
    except TypeError:
        pr = gh_repo.create_pull(title=title, body=body, base=base, head=branch)
    return f"Opened {getattr(pr, 'html_url', '')} (#{getattr(pr, 'number', '')})"


def format_git_result(command: str, out: dict) -> str:
    return (
        f"git exit {out.get('exit_code')}\n"
        f"command: {command}\n"
        f"stdout:\n{out.get('stdout') or ''}\n"
        f"stderr:\n{out.get('stderr') or ''}"
    )


def _repo_for(session_id: str, user_id: str):
    try:
        from services.repo_tools import session_repo
        repo = session_repo(session_id)
    except Exception as exc:
        return None, f"Repo tool failed: {exc}"
    if not repo:
        return None, "This chat has no open folder."
    if user_id and repo.get("user_id") and repo["user_id"] != user_id:
        return None, "This folder belongs to a different account."
    return repo, ""


def _audit(dlog, **kwargs) -> None:
    if dlog is None:
        return
    try:
        dlog("git_cli_run", **kwargs)
    except Exception:
        return


async def wait_git_decision(command: str, level: str, client_inbox, timeout: float = 180) -> dict:
    """Wait for approve, edit, or reject.

    approve returns the server-stored command. The string on the client
    message is ignored. A merge card ignores edit.
    """
    if client_inbox is None:
        return {"action": "no_inbox", "command": ""}
    try:
        while True:
            client_inbox.get_nowait()
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(client_inbox.get(), timeout=min(5.0, remaining))
        except asyncio.TimeoutError:
            continue
        if not isinstance(msg, dict) or msg.get("type") != "git_cli_response":
            continue
        action = str(msg.get("action") or "").lower()
        if action == "reject":
            return {"action": "reject", "command": ""}
        if action == "approve":
            return {"action": "approve", "command": command}
        if action == "edit":
            if level == "merge":
                return {"action": "merge_edit", "command": command}
            edited = str(msg.get("command") or "").strip()
            return {"action": "edit", "command": edited or command}
    return {"action": "timeout", "command": ""}


async def _after_decision(state: dict, client_inbox, timeout: float, root: str, head: str, default: str) -> dict:
    decision = await wait_git_decision(state["command"], state.get("level") or "", client_inbox, timeout=timeout)
    action = decision["action"]
    if action == "no_inbox":
        return {**state, "needs_approval": False, "run": False, "note": NO_INBOX_NOTE}
    if action == "reject":
        return {**state, "needs_approval": False, "run": False, "note": REJECT_NOTE}
    if action == "timeout":
        return {**state, "needs_approval": False, "run": False, "note": TIMEOUT_NOTE}
    if action == "merge_edit":
        return {**state, "needs_approval": False, "run": False, "note": MERGE_EDIT_NOTE}
    if action == "approve":
        return {**state, "needs_approval": False, "run": True, "note": ""}
    edited = begin_git_call(
        decision["command"], "", 0,
        root=root, head_branch=head, default_branch=default,
    )
    edited["mode"] = state.get("mode") or "edit"
    if edited["note"] and not edited["run"] and not edited["needs_approval"]:
        return edited
    if (state.get("level") == "merge") or level_is_worse(state.get("level") or "", edited.get("level") or ""):
        edited["needs_approval"] = True
        edited["run"] = False
        edited["note"] = ""
        return edited
    edited["needs_approval"] = False
    edited["run"] = True
    edited["note"] = ""
    return edited


def _apply_merge_preview(state: dict, user_id: str, repo: dict | None, root: str) -> dict:
    if state.get("action") != "pr_merge" and "pr merge" not in (state.get("command") or ""):
        return state
    if state.get("note"):
        return state
    number = state.get("target") or ""
    if not str(number).isdigit():
        return state
    owner, name = _github_coords(root, repo)
    if not owner or not name:
        return {**state, "run": False, "needs_approval": False, "note": "This folder is not linked to a GitHub repository."}
    reason, summary = pr_merge_preview(user_id, owner, name, int(number))
    if reason:
        return {**state, "run": False, "needs_approval": False, "note": reason}
    if summary:
        state = {**state, "summary": summary}
    return state


async def resolve_git_call(
    command: str,
    mode: str,
    calls_so_far: int,
    client_inbox,
    user_id: str = "",
    dlog=None,
    session_id: str = "",
    timeout: float = 180,
):
    """Yield SSE payloads, then one ``git_done`` dict.

    The caller yields every event except ``git_done``. Read and local
    commands do not wait. Ask and Plan never open an approval card.
    """
    repo, repo_err = _repo_for(session_id, user_id)
    root = (repo or {}).get("root_path") or ""
    if repo_err and not root:
        yield {
            "type": "git_done", "run": False, "counted": False, "note": repo_err,
            "command": command, "level": "", "title": "", "target": "", "summary": "",
        }
        return
    head = _head_branch(root)
    default = _default_branch(root, repo)
    state = begin_git_call(
        command, mode, calls_so_far,
        root=root, head_branch=head, default_branch=default,
    )
    state = _apply_merge_preview(state, user_id, repo, root)
    label = _repo_label(root, repo)
    if state.get("action") == "push" and state.get("title"):
        state["title"] = f"{state['title']} to {label}"
    elif state.get("action") == "pr_create":
        state["title"] = f"Open a pull request on {label}"
    if state["needs_approval"] and client_inbox is None:
        yield {
            "type": "git_done", "run": False, "counted": False, "note": NO_INBOX_NOTE,
            "command": state["command"], "level": state["level"], "title": state["title"],
            "target": state["target"], "summary": state.get("summary") or "",
        }
        return
    cards = 0
    while state.get("needs_approval"):
        cards += 1
        if cards > _MAX_APPROVAL_CARDS:
            yield {
                "type": "git_done", "run": False, "counted": False, "note": REJECT_NOTE,
                "command": state["command"], "level": state["level"], "title": state["title"],
                "target": state["target"], "summary": state.get("summary") or "",
            }
            return
        yield {
            "type": "git_approval_needed",
            "command": state["command"],
            "level": state["level"],
            "title": state["title"],
            "target": state["target"],
            "summary": state.get("summary") or "",
        }
        state = await _after_decision(state, client_inbox, timeout, root, head, default)
        state = _apply_merge_preview(state, user_id, repo, root)
    if not state["run"]:
        yield {
            "type": "git_done", "run": False, "counted": False, "note": state["note"],
            "command": state["command"], "level": state["level"], "title": state["title"],
            "target": state["target"], "summary": state.get("summary") or "",
        }
        return
    shown = " ".join((state["command"] or "").split())
    yield {"type": "progress", "content": f"Running {shown[:120] or 'git'}..."}
    started = time.perf_counter()
    out = await asyncio.to_thread(
        run_git_cli, state["command"],
        root=root, user_id=user_id, session_id=session_id or "", repo=repo,
        head_branch=head, default_branch=default,
    )
    _audit(
        dlog,
        session_id=session_id,
        command=(state["command"] or "")[:180],
        level=state["level"],
        mode=(mode or ""),
        exit=out.get("exit_code"),
        stdout_len=len(out.get("stdout") or ""),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    text = format_git_result(state["command"], out)
    if _ask_plan(mode) and calls_so_far + 1 >= 3:
        text = text + "\n" + ASK_PLAN_STOP_NOTE
    yield {
        "type": "git_cli",
        "command": state["command"],
        "stdout": out.get("stdout") or "",
        "stderr": out.get("stderr") or "",
        "exit_code": out.get("exit_code"),
        "level": state["level"],
        "title": state["title"],
        "target": state["target"],
        "summary": state.get("summary") or "",
    }
    yield {
        "type": "git_done",
        "run": True,
        "counted": True,
        "note": text,
        "command": state["command"],
        "level": state["level"],
        "title": state["title"],
        "target": state["target"],
        "summary": state.get("summary") or "",
        "out": out,
    }
