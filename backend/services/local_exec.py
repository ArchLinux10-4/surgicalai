"""run_local: one Mac action, classified, then approved like aws_cli.

The host helper runs the action. This module decides whether it may run.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import shlex
import time
import urllib.error
import urllib.request
from pathlib import Path

_FORBIDDEN = set(";|&`$<>")
_OUTPUT_CAP = 32_000
_MAX_APPROVAL_CARDS = 4
_LEVEL_RANK = {"read": 0, "change": 1, "network": 2, "delete": 3, "protected": 4}

ASK_PLAN_STOP_NOTE = "Do not call the local tool again this turn. Answer the user now."
ASK_PLAN_MUTATION_NOTE = (
    "Ask and Plan can only read. Switch to Edit or Agent to change files or run this command."
)
REJECT_NOTE = "The user rejected this command."
TIMEOUT_NOTE = "The approval request timed out. Ask the user what to do."
NO_INBOX_NOTE = (
    "This action needs approval, and this chat has no live connection to ask. "
    "Ask the user to retry from the app."
)
HELPER_DOWN_NOTE = "The local helper is not running. Run ./docker-rebuild-local.sh."
REDIRECT_NOTE = "Use git_cli for git and gh, and aws_cli for aws. Do not use run_local for those."

_READ_EXE = {
    "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "rg", "grep",
    "which", "whoami", "uname", "sw_vers", "date", "echo", "find",
}
_DELETE_EXE = {"rm", "rmdir", "unlink", "trash"}
_NETWORK_EXE = {"curl", "wget", "ssh", "scp", "sftp", "nc", "npx", "ping"}
_INSTALL_EXE = {"npm", "pnpm", "yarn", "pip", "pip3", "brew"}
_SHELLS = {"bash", "zsh", "sh"}
_REDIRECT = {"git", "gh", "aws"}


def local_agent_enabled() -> bool:
    """True when the user turned the tool on. Default off."""
    try:
        from database import get_setting
        raw = get_setting("local_agent_tools", "false")
    except Exception:
        raw = os.environ.get("LOCAL_AGENT_TOOLS", "")
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _token_path() -> Path:
    override = (os.environ.get("SURGICALAI_LOCAL_AGENT_TOKEN_FILE") or "").strip()
    if override:
        return Path(override)
    browse = (os.environ.get("SURGICALAI_BROWSE_ROOT") or "").strip()
    base = Path(browse) if browse else Path.home()
    return base / ".surgicalai" / "local-agent-token"


def _helper_base() -> str:
    from services.folder_picker import _helper_url
    return _helper_url()


def local_available(user_id: str = "") -> bool:
    """Tool is offered only on a local install with the helper up."""
    del user_id
    try:
        from database import USE_POSTGRES
        if USE_POSTGRES:
            return False
    except Exception:
        return False
    if not local_agent_enabled():
        return False
    if not _token_path().is_file():
        return False
    try:
        from services.folder_picker import helper_reachable
        return bool(helper_reachable())
    except Exception:
        return False


def _ask_plan(mode: str) -> bool:
    return (mode or "").strip().lower() in ("ask", "plan")


def level_is_worse(old: str, new: str) -> bool:
    return _LEVEL_RANK.get(new, 0) > _LEVEL_RANK.get(old, 0)


def _join_command(lines: list[str]) -> tuple[str, str]:
    """Join continuation lines. A bare newline is rejected."""
    if not lines:
        return "", "Command is empty."
    parts: list[str] = []
    for index, line in enumerate(lines):
        if line.endswith("\\"):
            parts.append(line[:-1])
            continue
        parts.append(line)
        if index != len(lines) - 1:
            return "", "Only a single command is allowed."
    command = " ".join(part.strip() for part in parts).strip()
    if not command:
        return "", "Command is empty."
    if any(ch in command for ch in _FORBIDDEN):
        return "", "Only a single command is allowed."
    return command, ""


def parse_local_body(body: str) -> dict:
    """Line format. Returns an error string the model can correct."""
    text = (body or "").replace("\r\n", "\n").strip()
    if not text:
        return {"error": "Say action: exec, read, write, or list."}
    if text.startswith("---"):
        return {"error": "Start with action: write, then a path, then ---."}
    lines = text.split("\n")
    headers: dict[str, str] = {}
    rest_at = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "---":
            rest_at = index + 1
            break
        if ":" in line and not headers.get("_command"):
            key, _, value = line.partition(":")
            name = key.strip().lower()
            if name in ("action", "cwd", "path", "lines", "expected_sha"):
                headers[name] = value.strip()
                rest_at = index + 1
                continue
        headers["_command"] = "1"
        rest_at = index
        break
    action = (headers.get("action") or "").lower()
    if action not in ("exec", "read", "write", "list"):
        return {"error": "action must be exec, read, write, or list."}
    if action == "write":
        content = "\n".join(lines[rest_at:])
        if "---" not in text:
            return {"error": "A write needs a line that is only --- before the file content."}
        path = headers.get("path") or ""
        if not path:
            return {"error": "A write needs path: the file to write."}
        return {
            "action": "write",
            "path": path,
            "expected_sha": headers.get("expected_sha") or "",
            "content": content,
            "command": path,
            "cwd": headers.get("cwd") or "",
        }
    if action in ("read", "list"):
        path = headers.get("path") or ""
        if not path:
            return {"error": f"A {action} needs path: the file or folder."}
        offset, limit = None, None
        span = headers.get("lines") or ""
        if "-" in span:
            left, _, right = span.partition("-")
            try:
                offset = int(left)
                limit = max(0, int(right) - offset + 1)
            except ValueError:
                return {"error": "lines must look like 1-200."}
        return {
            "action": action,
            "path": path,
            "offset": offset,
            "limit": limit,
            "command": path,
            "cwd": headers.get("cwd") or "",
        }
    command, err = _join_command(lines[rest_at:])
    if err:
        return {"error": err}
    return {
        "action": "exec",
        "command": command,
        "cwd": headers.get("cwd") or "",
        "path": "",
    }


def _basename(token: str) -> str:
    return os.path.basename(token or "")


def _shell_form(argv: list[str]) -> bool:
    if not argv:
        return False
    if _basename(argv[0]) not in _SHELLS:
        return False
    return any(flag in argv for flag in ("-lc", "-c"))


def _looks_like_path(token: str) -> bool:
    if token.startswith("-"):
        return False
    if token in (".", "..") or token.startswith("."):
        return True
    return token.startswith(("~", "/", "./", "../")) or "/" in token


def _inside(root: str, requested: str) -> bool:
    if not root:
        return False
    from fastapi import HTTPException
    from services.repositories import resolve_inside
    try:
        resolve_inside(root, requested)
    except HTTPException as exc:
        if exc.status_code == 403:
            return False
        raise
    return True


def _is_protected_path(path: str) -> bool:
    raw = (path or "").replace("\\", "/")
    if "/.git/config" in raw or raw.endswith("/.git/config") or raw == ".git/config":
        return True
    if "/.git/hooks" in raw or raw.startswith(".git/hooks"):
        return True
    if "/Library/" in raw or raw.startswith("~/Library") or raw.startswith("/Library/"):
        return True
    parts = [part for part in raw.split("/") if part not in ("", ".", "..")]
    for part in parts:
        if part.startswith("."):
            return True
    return False


def _argv_paths(argv: list[str]) -> list[str]:
    found = []
    for token in argv[1:]:
        if _looks_like_path(token):
            found.append(token)
    return found


def _is_delete(argv: list[str]) -> bool:
    exe = _basename(argv[0]) if argv else ""
    if exe in _DELETE_EXE:
        return True
    if exe == "find" and ("-delete" in argv or "-exec" in argv):
        return "-delete" in argv
    if exe in ("mv", "cp") and len(argv) >= 3:
        dest = argv[-1]
        if not dest.startswith("-") and os.path.exists(os.path.expanduser(dest)):
            return True
    return False


def _is_network(argv: list[str]) -> bool:
    exe = _basename(argv[0]) if argv else ""
    if exe in _NETWORK_EXE:
        return True
    if exe in _INSTALL_EXE and "install" in argv:
        return True
    return False


def _is_read_exe(argv: list[str]) -> bool:
    exe = _basename(argv[0]) if argv else ""
    if exe not in _READ_EXE:
        return False
    if exe == "find" and any(flag in argv for flag in ("-exec", "-delete", "-ok")):
        return False
    if exe in ("env", "printenv"):
        return False
    return True


def classify(action: str, args: dict, root: str = "") -> dict:
    """No approval decision. Levels: read, change, network, delete, protected."""
    action = action or ""
    command = args.get("command") or ""
    path = args.get("path") or ""
    cwd = args.get("cwd") or root or ""
    shell = False
    argv: list[str] = []
    if action == "exec":
        try:
            argv = shlex.split(command)
        except ValueError:
            return {"error": "Command could not be parsed."}
        if not argv:
            return {"error": "Command is empty."}
        exe = _basename(argv[0])
        if exe in ("sudo", "su", "doas"):
            return {"error": "sudo is not allowed."}
        if exe in _REDIRECT:
            return {"error": REDIRECT_NOTE}
        shell = _shell_form(argv)
    paths = []
    if path:
        paths.append(path)
    if action == "exec":
        paths.extend(_argv_paths(argv))
        if args.get("cwd"):
            paths.append(args["cwd"])
    protected = False
    outside = False
    for item in paths:
        if _is_protected_path(item):
            protected = True
        if root and not _inside(root, item):
            outside = True
        if not root and os.path.isabs(os.path.expanduser(item)):
            outside = True
    if not root and action == "exec" and not paths:
        outside = True
    level = "change"
    if action in ("read", "list"):
        level = "read"
    elif action == "exec" and _is_read_exe(argv) and not shell:
        level = "read"
    if action == "exec" and _is_network(argv):
        level = "network"
    if action == "exec" and _is_delete(argv):
        level = "delete"
    if shell and _LEVEL_RANK.get(level, 0) < _LEVEL_RANK["change"]:
        level = "change"
    if protected or outside:
        level = "protected"
    title = {
        "read": "Read",
        "change": "Change on this Mac",
        "network": "Network",
        "delete": "Delete",
        "protected": "Protected path",
    }[level]
    target = path or command or cwd
    if len(target) > 180:
        target = target[:177] + "..."
    return {
        "level": level,
        "title": title,
        "target": target,
        "argv": argv,
        "shell": shell,
        "outside": outside or not bool(root),
        "summary": "",
    }


def _allow_key(user_id: str) -> str:
    return f"local_allowlist:{(user_id or '').strip()}"


def load_allowlist(user_id: str) -> list:
    try:
        from database import get_setting
        raw = get_setting(_allow_key(user_id), "[]")
    except Exception:
        return []
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    rules = []
    for item in data:
        if isinstance(item, list) and all(isinstance(part, str) for part in item):
            rules.append(item)
    return rules


def save_allowlist(user_id: str, rules: list) -> None:
    from database import set_setting
    clean = [rule for rule in rules if isinstance(rule, list) and rule]
    set_setting(_allow_key(user_id), json.dumps(clean))


def _rule_matches(argv: list[str], rules: list) -> bool:
    for rule in rules:
        if argv[: len(rule)] == rule:
            return True
    return False


def _prefix_for(argv: list[str]) -> list[str]:
    if len(argv) >= 2 and not argv[1].startswith("-"):
        return argv[:2]
    return argv[:1]


def allowlist_can_skip(state: dict, rules: list) -> bool:
    if state.get("shell") or state.get("level") in ("delete", "protected", "read"):
        return False
    if state.get("outside"):
        return False
    if state.get("level") not in ("change", "network"):
        return False
    return _rule_matches(state.get("argv") or [], rules)


def begin_local_call(body: str, mode: str, calls_so_far: int, root: str = "", allow_rules=None) -> dict:
    """Decide run, refuse, or card. Does not call the helper."""
    parsed = parse_local_body(body)
    state = {
        "body": body or "",
        "action": parsed.get("action") or "",
        "command": parsed.get("command") or "",
        "path": parsed.get("path") or "",
        "cwd": parsed.get("cwd") or root or "",
        "content": parsed.get("content"),
        "expected_sha": parsed.get("expected_sha") or "",
        "offset": parsed.get("offset"),
        "limit": parsed.get("limit"),
        "mode": (mode or "").strip().lower(),
        "level": "",
        "title": "",
        "target": "",
        "summary": "",
        "argv": [],
        "shell": False,
        "outside": False,
        "needs_approval": False,
        "run": False,
        "note": "",
    }
    if parsed.get("error"):
        state["note"] = parsed["error"]
        return state
    try:
        classified = classify(parsed["action"], parsed, root)
    except Exception as exc:
        state["note"] = str(exc) or "Could not classify that action."
        return state
    if classified.get("error"):
        state["note"] = classified["error"]
        return state
    state.update({
        "level": classified["level"],
        "title": classified["title"],
        "target": classified["target"],
        "argv": classified["argv"],
        "shell": classified["shell"],
        "outside": classified["outside"],
        "summary": classified.get("summary") or "",
    })
    if _ask_plan(state["mode"]):
        if calls_so_far >= 3:
            state["note"] = ASK_PLAN_STOP_NOTE
            return state
        if state["level"] != "read":
            state["note"] = ASK_PLAN_MUTATION_NOTE
            return state
        state["run"] = True
        return state
    if state["level"] == "read":
        state["run"] = True
        return state
    if allowlist_can_skip(state, allow_rules or []):
        state["run"] = True
        return state
    state["needs_approval"] = True
    return state


def _diff_summary(path: str, old: str, new: str) -> str:
    lines = list(difflib.unified_diff(
        (old or "").splitlines(),
        (new or "").splitlines(),
        fromfile=path or "before",
        tofile=path or "after",
        lineterm="",
    ))
    if len(lines) > 80:
        lines = lines[:80] + ["... diff truncated"]
    return "\n".join(lines)


def _local_token() -> str:
    env = (os.environ.get("SURGICALAI_LOCAL_AGENT_TOKEN") or "").strip()
    if env:
        return env
    try:
        return _token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _call_helper(endpoint: str, payload: dict, timeout: float = 120) -> dict:
    token = _local_token()
    if not token:
        return {"error": HELPER_DOWN_NOTE, "status": 503}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _helper_base() + endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-SurgicalAI-Local-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict):
                data.setdefault("status", resp.status)
                return data
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"error": "The local helper rejected that action."}
        if isinstance(data, dict):
            data["status"] = exc.code
            return data
    except Exception:
        return {"error": HELPER_DOWN_NOTE, "status": 503}
    return {"error": HELPER_DOWN_NOTE, "status": 503}


def format_local_result(out: dict) -> str:
    if out.get("error") and "exit_code" not in out and "content" not in out and "entries" not in out and not out.get("ok"):
        return str(out.get("error"))
    if out.get("conflict"):
        return "The file changed since it was read. Read it again and retry the write."
    if "entries" in out:
        names = ", ".join(item.get("name", "") for item in out.get("entries") or [])
        note = "Output was cut at 500 entries." if out.get("truncated") else ""
        return f"exit 0\n{names}\n{note}".strip()
    if "content" in out and "exit_code" not in out:
        if out.get("binary"):
            return "That file is binary. It was not read."
        text = out.get("content") or ""
        extra = "\nOutput was cut at 32000 characters." if out.get("truncated") else ""
        return f"exit 0\n{text}{extra}"
    if out.get("ok") and "sha" in out:
        verb = "Created" if out.get("created") else "Wrote"
        return f"exit 0\n{verb} the file."
    code = out.get("exit_code")
    stdout = out.get("stdout") or ""
    stderr = out.get("stderr") or ""
    lines = [f"exit {code if code is not None else 'timeout'}", "stdout:", stdout, "stderr:", stderr]
    if out.get("timed_out"):
        lines.append("The command timed out and was stopped.")
    if out.get("truncated"):
        lines.append("Output was cut at 32000 characters.")
    return "\n".join(lines).strip()


def _perform(state: dict) -> dict:
    action = state.get("action")
    if action == "exec":
        return _call_helper("/exec", {
            "argv": state.get("argv") or [],
            "cwd": state.get("cwd") or "",
            "timeout_s": 120,
        }, timeout=130)
    if action == "read":
        return _call_helper("/read", {
            "path": state.get("path") or state.get("command") or "",
            "offset": state.get("offset"),
            "limit": state.get("limit"),
        })
    if action == "write":
        return _call_helper("/write", {
            "path": state.get("path") or state.get("command") or "",
            "content": state.get("content") if state.get("content") is not None else "",
            "expected_sha": state.get("expected_sha") or "",
        })
    if action == "list":
        return _call_helper("/list", {"path": state.get("path") or state.get("command") or ""})
    return {"error": "Unknown action.", "status": 400}


def _audit(dlog, **kwargs) -> None:
    if dlog is None:
        return
    try:
        dlog("local_cli_run", **kwargs)
    except Exception:
        return


def _running_label(state: dict) -> str:
    shown = state.get("command") or state.get("path") or state.get("action") or "local"
    shown = " ".join(str(shown).split())
    if len(shown) > 120:
        shown = shown[:117] + "..."
    return shown


async def wait_local_decision(command: str, client_inbox, timeout: float = 180) -> dict:
    """Approve returns the server command. The client string is ignored."""
    if client_inbox is None:
        return {"action": "no_inbox", "command": "", "always": False}
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
        if not isinstance(msg, dict) or msg.get("type") != "local_cli_response":
            continue
        action = str(msg.get("action") or "").lower()
        always = bool(msg.get("always"))
        if action == "reject":
            return {"action": "reject", "command": "", "always": False}
        if action == "approve":
            return {"action": "approve", "command": command, "always": always}
        if action == "edit":
            edited = str(msg.get("command") or "").strip()
            return {"action": "edit", "command": edited or command, "always": False}
    return {"action": "timeout", "command": "", "always": False}


def _rebuild_body(state: dict, edited: str) -> str:
    action = state.get("action") or "exec"
    if action == "exec":
        cwd = state.get("cwd") or ""
        cwd_line = f"cwd: {cwd}\n" if cwd else ""
        return f"action: exec\n{cwd_line}{edited}"
    if action == "write":
        content = state.get("content") if state.get("content") is not None else ""
        sha = state.get("expected_sha") or ""
        sha_line = f"expected_sha: {sha}\n" if sha else ""
        return f"action: write\npath: {edited}\n{sha_line}---\n{content}"
    return f"action: {action}\npath: {edited}"


async def _after_decision(state: dict, client_inbox, timeout: float, root: str, allow_rules) -> dict:
    decision = await wait_local_decision(state["command"], client_inbox, timeout=timeout)
    action = decision["action"]
    if action == "no_inbox":
        return {**state, "needs_approval": False, "run": False, "note": NO_INBOX_NOTE, "always": False}
    if action == "reject":
        return {**state, "needs_approval": False, "run": False, "note": REJECT_NOTE, "always": False}
    if action == "timeout":
        return {**state, "needs_approval": False, "run": False, "note": TIMEOUT_NOTE, "always": False}
    if action == "approve":
        return {**state, "needs_approval": False, "run": True, "note": "", "always": decision.get("always")}
    edited = begin_local_call(
        _rebuild_body(state, decision["command"]),
        state.get("mode") or "edit",
        0,
        root,
        allow_rules,
    )
    if edited["note"] and not edited["run"] and not edited["needs_approval"]:
        return edited
    if level_is_worse(state.get("level") or "", edited.get("level") or ""):
        edited["needs_approval"] = True
        edited["run"] = False
        edited["note"] = ""
        return edited
    edited["needs_approval"] = False
    edited["run"] = True
    edited["note"] = ""
    return edited


def _attach_write_summary(state: dict) -> dict:
    if state.get("action") != "write" or not state.get("needs_approval"):
        return state
    path = state.get("path") or state.get("command") or ""
    current = _call_helper("/read", {"path": path})
    old = "" if current.get("error") or current.get("binary") else (current.get("content") or "")
    state["summary"] = _diff_summary(path, old, state.get("content") or "")
    if current.get("sha"):
        state["expected_sha"] = current["sha"]
    return state


def _remember_always(state: dict, user_id: str, rules: list) -> None:
    if not state.get("always"):
        return
    if state.get("shell") or state.get("outside"):
        return
    if state.get("level") not in ("change", "network"):
        return
    prefix = _prefix_for(state.get("argv") or [])
    if not prefix or prefix in rules:
        return
    save_allowlist(user_id, rules + [prefix])


async def resolve_local_call(
    body: str,
    mode: str,
    calls_so_far: int,
    client_inbox,
    user_id: str = "",
    dlog=None,
    session_id: str = "",
    root: str = "",
    timeout: float = 180,
):
    """Yield SSE payloads, then one local_done dict."""
    try:
        from database import USE_POSTGRES
        hosted = bool(USE_POSTGRES)
    except Exception:
        hosted = False
    if hosted or not local_agent_enabled():
        yield {
            "type": "local_done",
            "run": False,
            "counted": False,
            "note": "The local tool is off.",
            "command": "",
            "level": "",
            "title": "",
            "target": "",
        }
        return
    rules = load_allowlist(user_id)
    state = begin_local_call(body, mode, calls_so_far, root, rules)
    if state["needs_approval"] and client_inbox is None:
        yield {
            "type": "local_done",
            "run": False,
            "counted": False,
            "note": NO_INBOX_NOTE,
            "command": state["command"],
            "level": state["level"],
            "title": state["title"],
            "target": state["target"],
        }
        return
    cards = 0
    waited = 0.0
    while state.get("needs_approval"):
        cards += 1
        if cards > _MAX_APPROVAL_CARDS:
            yield {
                "type": "local_done",
                "run": False,
                "counted": False,
                "note": REJECT_NOTE,
                "command": state["command"],
                "level": state["level"],
                "title": state["title"],
                "target": state["target"],
            }
            return
        state = _attach_write_summary(state)
        yield {
            "type": "local_approval_needed",
            "action": state.get("action") or "",
            "command": state.get("command") or "",
            "path": state.get("path") or "",
            "level": state.get("level") or "",
            "title": state.get("title") or "",
            "target": state.get("target") or "",
            "summary": state.get("summary") or "",
            "cwd": state.get("cwd") or "",
        }
        started_wait = time.perf_counter()
        state = await _after_decision(state, client_inbox, timeout, root, rules)
        waited += time.perf_counter() - started_wait
    if not state["run"]:
        yield {
            "type": "local_done",
            "run": False,
            "counted": False,
            "note": state["note"],
            "command": state.get("command") or "",
            "level": state.get("level") or "",
            "title": state.get("title") or "",
            "target": state.get("target") or "",
            "waited_s": round(waited, 3),
        }
        return
    if not local_available(user_id):
        yield {
            "type": "local_done",
            "run": False,
            "counted": False,
            "note": HELPER_DOWN_NOTE,
            "command": state.get("command") or "",
            "level": state.get("level") or "",
            "title": state.get("title") or "",
            "target": state.get("target") or "",
            "waited_s": round(waited, 3),
        }
        return
    yield {"type": "progress", "content": f"Running {_running_label(state)}..."}
    started = time.perf_counter()
    out = await asyncio.to_thread(_perform, state)
    _audit(
        dlog,
        session_id=session_id,
        command=(state.get("command") or state.get("path") or "")[:180],
        level=state.get("level") or "",
        mode=(mode or ""),
        exit=out.get("exit_code"),
        stdout_len=len(out.get("stdout") or out.get("content") or ""),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    if state.get("always"):
        _remember_always(state, user_id, rules)
    text = format_local_result(out)
    if _ask_plan(mode) and calls_so_far + 1 >= 3:
        text = text + "\n" + ASK_PLAN_STOP_NOTE
    yield {
        "type": "local_cli",
        "command": state.get("command") or "",
        "stdout": out.get("stdout") or out.get("content") or "",
        "stderr": out.get("stderr") or "",
        "exit_code": out.get("exit_code", 0 if not out.get("error") else 1),
        "level": state.get("level") or "",
        "title": state.get("title") or "",
        "target": state.get("target") or "",
    }
    yield {
        "type": "local_done",
        "run": True,
        "counted": True,
        "note": text,
        "command": state.get("command") or "",
        "level": state.get("level") or "",
        "title": state.get("title") or "",
        "target": state.get("target") or "",
        "out": out,
        "waited_s": round(waited, 3),
    }


def local_agent_guidance() -> str:
    """Prompt text for tag models. Callers must not paraphrase a shorter version."""
    from services.agent_tool_spec import local_text
    return local_text("tag")
