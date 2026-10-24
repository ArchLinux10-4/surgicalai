"""AWS CLI runner and access-key check.

The terminal and every agent call run_aws_cli. The process is always the
aws binary with shell=False. Secrets stay in the environment of that
process only, and only the variables listed in ``_subprocess_env``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time

from crypto_utils import decrypt_api_key
from database import get_user_api_key

AWS_REGIONS = (
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-central-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
)
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_KEY_RE_PREFIXES = ("AKIA", "ASIA")
_FORBIDDEN = set(";|&`$<>")
_OUTPUT_CAP = 32_000
_TRUNC_NOTE = "[output truncated at 32000 characters, use --query]"

ASK_PLAN_STOP_NOTE = "Do not call AWS again this turn. Answer the user now."
ASK_PLAN_MUTATION_NOTE = (
    "AWS commands that change resources or read secrets are not available in Ask or Plan mode. "
    "Describe the command to the user, or tell them to switch to Edit or Agent mode."
)
REJECT_NOTE = "The user rejected this command."
TIMEOUT_NOTE = "The approval request timed out. Ask the user what to do."
NO_INBOX_NOTE = (
    "AWS command was not run because this connection cannot ask the user to approve it."
)

_BANNED_FLAGS = {
    "--endpoint-url",
    "--ca-bundle",
    "--no-verify-ssl",
    "--debug",
    "--profile",
    "--cli-input-json",
    "--cli-input-yaml",
    "--generate-cli-skeleton",
}
_URL_PREFIXES = ("file://", "fileb://", "http://", "https://")
_BOOLEAN_FLAGS = {
    "--no-paginate",
    "--force",
    "--recursive",
    "--dry-run",
    "--debug",
    "--no-verify-ssl",
    "--no-sign-request",
    "--no-cli-pager",
    "--delete",
}
_TARGET_FLAGS = {
    "--function-name",
    "--bucket",
    "--repository-name",
    "--stack-name",
    "--table-name",
}
_READ_PREFIXES = ("get-", "describe-", "list-", "search-", "batch-get-")
_DELETE_PREFIXES = (
    "delete-",
    "terminate-",
    "deregister-",
    "remove-",
    "purge-",
    "drop-",
    "revoke-",
    "detach-",
    "disassociate-",
)
_S3_DELETE_OPS = {"rm", "rb", "mv", "sync"}
_LEVEL_RANK = {"on_task": 0, "side": 0, "change": 1, "sensitive": 2, "deletes": 3}
_INTENT_RE = re.compile(r"^\s*intent\s*:\s*(on_task|side)\s*$")
_IDENT_RE = re.compile(r"^[a-z0-9-]+$")
_MAX_APPROVAL_CARDS = 4


def validate_access_key_id(value: str) -> str:
    key = (value or "").strip()
    if len(key) != 20 or not key.startswith(_KEY_RE_PREFIXES) or not key[4:].isalnum() or not key.isupper():
        raise ValueError("Access key ID must start with AKIA or ASIA and be 20 characters.")
    if not all(c.isalnum() for c in key):
        raise ValueError("Access key ID must start with AKIA or ASIA and be 20 characters.")
    return key


def aws_connected(user_id: str) -> bool:
    if not user_id:
        return False
    try:
        return bool(get_user_api_key(user_id, "aws"))
    except Exception:
        return False


def _load_creds(user_id: str) -> dict | None:
    enc = get_user_api_key(user_id, "aws")
    if not enc:
        return None
    data = json.loads(decrypt_api_key(enc))
    if not isinstance(data, dict):
        return None
    return data


def check_connect_fields(access_key_id: str, secret: str, region: str, session_token: str) -> dict:
    key_id = validate_access_key_id(access_key_id)
    secret_s = (secret or "").strip()
    if len(secret_s) < 16:
        raise ValueError("Secret access key is too short.")
    region_s = (region or "us-east-1").strip() or "us-east-1"
    if not _REGION_RE.fullmatch(region_s):
        raise ValueError("Region must look like us-east-1.")
    token = (session_token or "").strip()
    if key_id.startswith("ASIA") and not token:
        raise ValueError("Session token is required for temporary keys.")
    return {
        "access_key_id": key_id,
        "secret_access_key": secret_s,
        "region": region_s,
        "session_token": token,
    }


_CLI_VERSION: str | None = None


def aws_cli_version() -> str:
    """First line of ``aws --version``, or empty when the CLI is not installed.

    The result is cached for the process. ``aws --version`` writes to stderr.
    """
    global _CLI_VERSION
    if _CLI_VERSION is not None:
        return _CLI_VERSION
    aws_bin = shutil.which("aws")
    if not aws_bin:
        _CLI_VERSION = ""
        return ""
    try:
        proc = subprocess.run(
            [aws_bin, "--version"],
            shell=False,
            timeout=5,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "AWS_PAGER": ""},
        )
    except Exception:
        _CLI_VERSION = ""
        return ""
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    _CLI_VERSION = line[:160]
    return _CLI_VERSION


def sts_identity(access_key_id: str, secret: str, region: str, session_token: str) -> dict:
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client(
        "sts",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret,
        aws_session_token=session_token or None,
        region_name=region,
        endpoint_url=f"https://sts.{region}.amazonaws.com",
    )
    try:
        ident = client.get_caller_identity()
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code in ("InvalidClientTokenId", "SignatureDoesNotMatch", "ExpiredToken"):
            raise ValueError("Access key was rejected by AWS.") from exc
        raise
    return {
        "account": ident.get("Account") or "",
        "arn": ident.get("Arn") or "",
        "user_id": ident.get("UserId") or "",
    }


def normalize_aws_command(command: str) -> str:
    """Join shell line continuations into one command.

    A pasted multi-line command keeps the backslash and the newline becomes
    a space, so shlex turns each ``\\`` into a blank argument. AWS CLI then
    reports ``Unknown options: , , ,``. Continuations outside quotes are
    removed. A real newline that is not a continuation stays, and
    ``_reject_command`` still refuses it.
    """
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


def _reject_command(command: str) -> str | None:
    raw = command or ""
    if not raw.strip():
        return "Command is empty."
    if "\n" in raw or "\r" in raw or any(ch in raw for ch in _FORBIDDEN):
        return "Only a single aws command is allowed."
    try:
        argv = shlex.split(raw)
    except ValueError:
        return "Command could not be parsed."
    if not argv or argv[0] != "aws":
        return "Command must start with aws."
    if "configure" in argv:
        return "aws configure is not allowed."
    for tok in argv[1:]:
        flag = tok.split("=", 1)[0]
        if flag in _BANNED_FLAGS:
            return f"{flag} is not allowed."
        if tok.lower().startswith(_URL_PREFIXES):
            return "file://, fileb://, and http(s) URLs are not allowed in the command."
    return None


def _skip_flag(argv: list[str], index: int) -> int:
    """Advance past one flag. A following token is its value unless the flag is boolean."""
    tok = argv[index]
    name = tok.split("=", 1)[0]
    if "=" in tok or name in _BOOLEAN_FLAGS:
        return index + 1
    if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
        return index + 2
    return index + 1


def _parse_service_operation(argv: list[str]) -> tuple[str, str, int] | None:
    """Return (service, operation, operation_index) after leading global flags.

    ``None`` when the command is not ``aws <service> <operation>`` with
    lowercase names. Callers treat that as a change, never as a read.
    """
    if not argv or argv[0] != "aws":
        return None
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i = _skip_flag(argv, i)
    if i >= len(argv):
        return None
    service = argv[i]
    j = i + 1
    while j < len(argv) and argv[j].startswith("-"):
        j = _skip_flag(argv, j)
    if j >= len(argv) or argv[j].startswith("-"):
        return None
    operation = argv[j]
    if not _IDENT_RE.fullmatch(service) or not _IDENT_RE.fullmatch(operation):
        return None
    return service, operation, j


def _query_value(argv: list[str]) -> str:
    for i, tok in enumerate(argv):
        if tok == "--query" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--query="):
            return tok.split("=", 1)[1]
    return ""


def _amplify_list_apps_exposes_env(argv: list[str]) -> bool:
    """list-apps returns environmentVariables on every app unless --query drops them.

    A multi-select hash ({name:name,repository:repository}) or a single field
    such as apps[].repository is safe. No query, a bare apps list, or any
    query that names environment variables is not.
    """
    query = _query_value(argv)
    if not query or "environment" in query.lower():
        return True
    if "{" in query:
        return False
    compact = re.sub(r"\s+", "", query)
    return re.fullmatch(r"apps\[\]\.(name|repository|appId|defaultDomain|platform)", compact) is None


def _is_sensitive(service: str, operation: str, argv: list[str] | None = None) -> bool:
    if service == "secretsmanager" and operation == "get-secret-value":
        return True
    if service == "ssm" and operation in ("get-parameter", "get-parameters", "get-parameters-by-path"):
        return True
    if service == "sts" and (
        operation.startswith("assume-role")
        or operation in ("get-session-token", "get-federation-token")
    ):
        return True
    if service == "iam" and operation in ("create-access-key", "create-login-profile"):
        return True
    if service == "kms" and (operation == "decrypt" or operation.startswith("generate-data-key")):
        return True
    if service == "lambda" and operation in ("get-function", "get-function-configuration"):
        return True
    if service == "ecs" and operation == "describe-task-definition":
        return True
    if service == "codebuild" and operation == "batch-get-projects":
        return True
    if service == "rds" and operation == "generate-db-auth-token":
        return True
    if service == "ecr" and operation in ("get-login-password", "get-authorization-token"):
        return True
    if service == "s3" and operation == "presign":
        return True
    if service == "amplify" and operation in ("get-app", "get-branch", "list-branches"):
        return True
    if service == "amplify" and operation == "list-apps":
        return _amplify_list_apps_exposes_env(argv or [])
    return False


def _has_exact_flag(argv: list[str], flag: str) -> bool:
    for tok in argv:
        if tok.split("=", 1)[0] == flag:
            return True
    return False


def _is_delete(service: str, operation: str, argv: list[str]) -> bool:
    if _has_exact_flag(argv, "--delete") or _has_exact_flag(argv, "--force"):
        return True
    if service == "s3" and operation in _S3_DELETE_OPS:
        return True
    return operation.startswith(_DELETE_PREFIXES)


def _operation_is_read(operation: str) -> bool:
    return operation == "ls" or operation.startswith(_READ_PREFIXES)


def aws_command_level(command: str, intent: str = "") -> str:
    """Server-side level. The model's intent cannot upgrade a delete, a secret, or a change."""
    argv = _argv(normalize_aws_command(command))
    parsed = _parse_service_operation(argv)
    if parsed is None:
        return "change"
    service, operation, _idx = parsed
    if _is_sensitive(service, operation, argv):
        return "sensitive"
    if _is_delete(service, operation, argv):
        return "deletes"
    if _operation_is_read(operation):
        return "on_task" if intent == "on_task" else "side"
    return "change"


def is_readonly_aws_command(command: str) -> bool:
    return aws_command_level(command, "") in ("on_task", "side")


def aws_command_target(command: str) -> str:
    argv = _argv(normalize_aws_command(command))
    for i, tok in enumerate(argv):
        name, _, value = tok.partition("=")
        if name in _TARGET_FLAGS:
            if value:
                return value
            if i + 1 < len(argv):
                return argv[i + 1]
            return ""
    parsed = _parse_service_operation(argv)
    if parsed is None:
        return ""
    _service, _operation, op_index = parsed
    i = op_index + 1
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("-"):
            nxt = _skip_flag(argv, i)
            if nxt == i:
                break
            i = nxt
            continue
        return tok
    return ""


def approval_title(level: str, target: str, recursive: bool = False) -> str:
    if level == "deletes":
        base = f"This deletes {target}" if target else "This deletes data"
        if recursive:
            return f"{base} (recursive)"
        return base
    if level == "sensitive":
        return "This reads a secret"
    return "AWS command needs approval"


def level_is_worse(old: str, new: str) -> bool:
    return _LEVEL_RANK.get(new, 1) > _LEVEL_RANK.get(old, 1)


def split_intent(body: str) -> tuple[str, str]:
    """Split an optional ``intent: on_task|side`` first line from the command.

    The ``<aws_cli>`` open tag stays exactly ``<aws_cli>``. Intent is not an
    attribute, because the Edit/Agent loop matches that open tag as a string.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    matched = _INTENT_RE.match(lines[0]) if lines else None
    if matched:
        return matched.group(1), "\n".join(lines[1:]).strip()
    return "", text.strip()


def context_tag_looks_real(tag_name: str, body: str) -> bool:
    """False when a streaming tag body is prose, not a tool call.

    Search, file, GitHub, and history bodies start with JSON. An AWS command
    starts with ``aws`` or with the ``intent:`` line. Checking for ``[`` or
    ``{`` would drop every real ``<aws_cli>`` tag as it streams.
    """
    stripped = (body or "").lstrip()
    if not stripped:
        return True
    if tag_name == "aws":
        return stripped[0] in "aAiI"
    return stripped[0] in "[{"


def _ask_plan(mode: str) -> bool:
    return (mode or "").strip().lower() in ("ask", "plan")


def begin_aws_call(command: str, intent: str, mode: str, calls_so_far: int) -> dict:
    """Decide whether a command runs, is refused, or needs a card. No I/O."""
    command = normalize_aws_command(command)
    clean_intent = intent if intent in ("on_task", "side") else ""
    reason = _reject_command(command)
    level = aws_command_level(command, clean_intent)
    target = aws_command_target(command)
    recursive = _has_exact_flag(_argv(command), "--recursive")
    state = {
        "command": command,
        "intent": clean_intent,
        "mode": (mode or "").strip().lower(),
        "level": level,
        "title": approval_title(level, target, recursive=recursive and level == "deletes"),
        "target": target,
        "needs_approval": False,
        "run": False,
        "note": "",
    }
    if reason:
        state["note"] = reason
        return state
    if _ask_plan(mode):
        if calls_so_far >= 3:
            state["note"] = ASK_PLAN_STOP_NOTE
            return state
        if level not in ("on_task", "side"):
            state["note"] = ASK_PLAN_MUTATION_NOTE
            return state
        state["run"] = True
        return state
    if level in ("on_task", "side"):
        state["run"] = True
        return state
    state["needs_approval"] = True
    return state


def _cap_text(text: str) -> str:
    if len(text) <= _OUTPUT_CAP:
        return text
    return text[:_OUTPUT_CAP] + "\n" + _TRUNC_NOTE


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _collect_cli_files(tmp: str) -> list[dict]:
    """Read text files the CLI wrote. Skip the empty credentials file.

    Combined text stops at ``_OUTPUT_CAP``. A non-UTF-8 file is reported by
    name and size and its bytes are not returned.
    """
    files: list[dict] = []
    used = 0
    root = os.path.realpath(tmp)
    try:
        names = sorted(os.listdir(tmp))
    except OSError:
        return files
    for name in names:
        if name == "credentials":
            continue
        path = os.path.join(tmp, name)
        real = os.path.realpath(path)
        if real != root and not real.startswith(root + os.sep):
            continue
        if not os.path.isfile(real):
            continue
        try:
            with open(real, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if used >= _OUTPUT_CAP:
            break
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = f"binary, {len(raw)} bytes"
        room = _OUTPUT_CAP - used
        if len(content) > room:
            content = content[:room] + "\n" + _TRUNC_NOTE
        used += len(content)
        files.append({"name": name, "content": content})
        if used >= _OUTPUT_CAP:
            break
    return files


def format_aws_result(out: dict) -> str:
    """The only model-facing AWS result string."""
    text = (
        f"AWS CLI exit {out.get('exit_code')}.\n"
        f"stdout:\n{out.get('stdout') or ''}\n"
        f"stderr:\n{out.get('stderr') or ''}"
    )
    files = out.get("files") or []
    if not files:
        return text
    chunks = [text, "files:"]
    for item in files:
        chunks.append(f"{item.get('name') or 'file'}:\n{item.get('content') or ''}")
    return "\n".join(chunks)


def _subprocess_env(tmp: str, creds: dict) -> dict:
    """Environment for the aws child. Nothing from the server process is copied
    except PATH and LANG. Railway secrets must not reach this process.
    """
    env: dict[str, str] = {}
    if os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    env["LANG"] = os.environ.get("LANG") or "C.UTF-8"
    env["HOME"] = tmp
    env["AWS_ACCESS_KEY_ID"] = creds.get("access_key_id") or ""
    env["AWS_SECRET_ACCESS_KEY"] = creds.get("secret_access_key") or ""
    env["AWS_DEFAULT_REGION"] = creds.get("region") or "us-east-1"
    env["AWS_PAGER"] = ""
    env["AWS_CLI_AUTO_PROMPT"] = "off"
    env["AWS_RETRY_MODE"] = "standard"
    env["AWS_MAX_ATTEMPTS"] = "3"
    token = creds.get("session_token") or ""
    if token:
        env["AWS_SESSION_TOKEN"] = token
    return env


def run_aws_cli(user_id: str, command: str) -> dict:
    command = normalize_aws_command(command)
    reason = _reject_command(command)
    if reason:
        return {"exit_code": 1, "stdout": "", "stderr": reason, "files": []}
    creds = None
    try:
        creds = _load_creds(user_id)
    except Exception:
        creds = None
    if not creds:
        return {"exit_code": 1, "stdout": "", "stderr": "AWS is not connected.", "files": []}
    argv = shlex.split(command)
    aws_bin = shutil.which("aws")
    if not aws_bin:
        return {"exit_code": 1, "stdout": "", "stderr": "AWS CLI is not installed on the server.", "files": []}
    argv[0] = aws_bin
    tmp = tempfile.mkdtemp(prefix="awscli-")
    try:
        cred_path = os.path.join(tmp, "credentials")
        with open(cred_path, "w", encoding="utf-8") as fh:
            fh.write("")
        env = _subprocess_env(tmp, creds)
        try:
            proc = subprocess.run(
                argv, shell=False, timeout=30, capture_output=True,
                cwd=tmp, env=env, text=True,
            )
        except subprocess.TimeoutExpired as exc:
            files = _collect_cli_files(tmp)
            err = _as_text(getattr(exc, "stderr", None)).strip()
            stderr = "AWS CLI timed out." if not err else "AWS CLI timed out.\n" + err
            return {
                "exit_code": 124,
                "stdout": _cap_text(_as_text(getattr(exc, "stdout", None))),
                "stderr": _cap_text(stderr),
                "files": files,
            }
        return {
            "exit_code": int(proc.returncode),
            "stdout": _cap_text(proc.stdout or ""),
            "stderr": _cap_text(proc.stderr or ""),
            "files": _collect_cli_files(tmp),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _running_label(command: str) -> str:
    shown = " ".join((command or "").split())
    if len(shown) > 120:
        shown = shown[:117] + "..."
    return shown or "aws"


async def wait_aws_decision(command: str, client_inbox, timeout: float = 180) -> dict:
    """Wait for approve, edit, or reject.

    ``approve`` returns the server-stored command. The string on the client
    message is ignored, so a tampered approve cannot swap in a different
    command. ``edit`` returns the edited text for a new classification.
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
        if not isinstance(msg, dict) or msg.get("type") != "aws_cli_response":
            continue
        action = str(msg.get("action") or "").lower()
        if action == "reject":
            return {"action": "reject", "command": ""}
        if action == "approve":
            return {"action": "approve", "command": command}
        if action == "edit":
            edited = str(msg.get("command") or "").strip()
            return {"action": "edit", "command": edited or command}
    return {"action": "timeout", "command": ""}


async def _after_decision(state: dict, client_inbox, timeout: float) -> dict:
    decision = await wait_aws_decision(state["command"], client_inbox, timeout=timeout)
    action = decision["action"]
    if action == "no_inbox":
        return {**state, "needs_approval": False, "run": False, "note": NO_INBOX_NOTE}
    if action == "reject":
        return {**state, "needs_approval": False, "run": False, "note": REJECT_NOTE}
    if action == "timeout":
        return {**state, "needs_approval": False, "run": False, "note": TIMEOUT_NOTE}
    if action == "approve":
        return {**state, "needs_approval": False, "run": True, "note": ""}
    edited = begin_aws_call(decision["command"], "", state.get("mode") or "edit", 0)
    edited["mode"] = state.get("mode") or "edit"
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


def _audit(dlog, **kwargs) -> None:
    if dlog is None:
        return
    try:
        dlog("aws_cli_run", **kwargs)
    except Exception:
        return


async def resolve_aws_call(
    command: str,
    intent: str,
    mode: str,
    calls_so_far: int,
    client_inbox,
    user_id: str = "",
    dlog=None,
    session_id: str = "",
    timeout: float = 180,
):
    """Yield SSE payloads, then one ``aws_done`` dict.

    ``aws_done`` carries ``run``, ``command``, ``level``, ``title``,
    ``target``, ``note``, and ``counted``. The caller yields every other
    event and does not run the CLI itself. Read-only commands do not wait.
    Ask and Plan never open an approval card.
    """
    state = begin_aws_call(command, intent, mode, calls_so_far)
    if state["needs_approval"] and client_inbox is None:
        yield {
            "type": "aws_done",
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
    while state.get("needs_approval"):
        cards += 1
        if cards > _MAX_APPROVAL_CARDS:
            yield {
                "type": "aws_done",
                "run": False,
                "counted": False,
                "note": REJECT_NOTE,
                "command": state["command"],
                "level": state["level"],
                "title": state["title"],
                "target": state["target"],
            }
            return
        yield {
            "type": "aws_approval_needed",
            "command": state["command"],
            "level": state["level"],
            "title": state["title"],
            "target": state["target"],
        }
        state = await _after_decision(state, client_inbox, timeout)
    if not state["run"]:
        yield {
            "type": "aws_done",
            "run": False,
            "counted": False,
            "note": state["note"],
            "command": state["command"],
            "level": state["level"],
            "title": state["title"],
            "target": state["target"],
        }
        return
    yield {"type": "progress", "content": f"Running {_running_label(state['command'])}..."}
    started = time.perf_counter()
    out = await asyncio.to_thread(run_aws_cli, user_id, state["command"])
    _audit(
        dlog,
        session_id=session_id,
        command=(state["command"] or "")[:180],
        level=state["level"],
        intent=state.get("intent") or "",
        mode=(mode or ""),
        exit=out.get("exit_code"),
        stdout_len=len(out.get("stdout") or ""),
        files=len(out.get("files") or []),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    text = format_aws_result(out)
    if _ask_plan(mode) and calls_so_far + 1 >= 3:
        text = text + "\n" + ASK_PLAN_STOP_NOTE
    yield {
        "type": "aws_cli",
        "command": state["command"],
        "stdout": out.get("stdout") or "",
        "stderr": out.get("stderr") or "",
        "exit_code": out.get("exit_code"),
        "level": state["level"],
        "title": state["title"],
        "target": state["target"],
        "files": out.get("files") or [],
    }
    yield {
        "type": "aws_done",
        "run": True,
        "counted": True,
        "note": text,
        "command": state["command"],
        "level": state["level"],
        "title": state["title"],
        "target": state["target"],
        "out": out,
    }


def aws_agent_guidance() -> str:
    """Prompt text for every mode that can run the connected AWS CLI.

    Matches ``_reject_command`` and ``normalize_aws_command``. Callers must
    not paraphrase a shorter version.
    """
    return (
        "How to call AWS. Claude, GPT, Ask, Plan, Edit, and Agent emit exactly "
        "one tag and then stop. The open tag is exactly <aws_cli> with no "
        "attributes. The body is an optional intent line, then the command, "
        "not JSON:\n"
        "<aws_cli>\nintent: on_task\naws sts get-caller-identity --output json\n</aws_cli>\n"
        "intent is on_task only for the next command in the locate order below, "
        "and side for any other read you still believe is worth making. Omit "
        "the line and a read is treated as side. You cannot mark a change, a "
        "delete, or a secret read as on_task. The server overrules that.\n"
        "Grok does not emit that tag. Call the aws_cli function with "
        "{\"command\": \"aws sts get-caller-identity --output json\", "
        "\"intent\": \"on_task\"}. One call per turn. A second call in the "
        "same turn is dropped.\n"
        "Read-only commands run immediately. A read is ls, or an operation "
        "that starts with get-, describe-, list-, search-, or batch-get-, "
        "except the secret reads listed below. You do not wait for approval "
        "on those. Ask and Plan can only run read-only AWS commands. A command "
        "that changes a resource, deletes data, or reads a secret is refused "
        "in Ask and Plan. Tell the user the exact command and that they should "
        "switch to Edit or Agent mode. Do not retry it in Ask or Plan.\n"
        "Edit and Agent: a change, a delete, or a secret read waits on the "
        "approval card. Then stop. Do not also write the answer in that "
        "message. If they reject it, the next message says they rejected it. "
        "Do not retry a rephrased version unless they ask. If the approval "
        "times out, the next message says the approval timed out. Ask the user "
        "what to do. If they approve after editing, the approved string is "
        "what ran. Do not assume your original text ran. A read still runs "
        "without a card in Edit and Agent.\n"
        "How to read the result. The next turn is plain text:\n"
        "AWS CLI exit <code>.\nstdout:\n<stdout>\nstderr:\n<stderr>\n"
        "files:\nresponse.json:\n<file text>\n"
        "Trust the exit code, then read stderr before inventing a cause. "
        "stdout with --output json is one JSON document. Do not treat a "
        "non-zero exit plus empty stdout as an empty account. When you see "
        "[output truncated at 32000 characters, use --query] the stdout is "
        "partial: narrow it with --query or --max-items and call again. Do "
        "not guess missing rows. Empty stdout and empty stderr with exit 0 "
        "is a real empty result. Say that and do not repeat the command. "
        "files: is present only when the command wrote a file. Read that file "
        "body. It is not in stdout. The next command may only use an id, "
        "name, or ARN that appeared in stdout or in a returned file.\n"
        "What this CLI accepts. One command. It must start with aws. The "
        "process splits it with shlex and runs shell=False. Rejected: a raw "
        "newline, and any of ; | & ` $ < >. No &&, pipes, redirects, or $(). "
        "A backslash at the end of a line is a continuation and is joined into "
        "one command. A newline that is not a continuation is rejected. "
        "aws configure is not allowed. Do not use --profile or aws sso login. "
        "Do not pass --endpoint-url, --ca-bundle, --no-verify-ssl, --debug, "
        "--cli-input-json, --cli-input-yaml, or --generate-cli-skeleton. "
        "The process home is an empty temp directory, so there is no profile "
        "and no saved config. Credentials and region come from the connected "
        "account. Never put keys in the command. The user sees the command on "
        "the approval card when a card is required. --region only when "
        "overriding that connected region. The pager and auto-prompt are off. "
        "The run times out at 30 seconds. Output is capped. The working "
        "directory is empty and is deleted after the run. A file the command "
        "writes there, such as response.json, is returned to you in files: "
        "and then deleted. A download does not land on the user's machine. "
        "aws s3 cp ./file s3://bucket has no local file to read.\n"
        "How to use any AWS service. Every service is aws <service> <operation> "
        "[options]. Examples: aws s3 ls, aws ec2 describe-instances, "
        "aws iam list-users, aws lambda list-functions. Use the CLI operation "
        "name, not a boto3 method name. Prefer a read first "
        "(sts get-caller-identity, describe-*, list-*, get-*) so the next "
        "command is grounded in this account. Do not pipe to jq or grep. Use "
        "--output json and --query. Quote values that contain spaces. Do not "
        "rely on an interactive prompt. A mutating command (delete-*, "
        "terminate-*, put-*, create-*) is allowed only in Edit or Agent, and "
        "only when the user asked for that change. They still approve the "
        "exact string. If stderr says the command was rejected, send one "
        "corrected command. Do not wrap it in a shell.\n"
        "Exit codes are not all the same failure. 0 is success. 1 means one "
        "or more high-level S3 transfers failed, or this runner refused the "
        "command before AWS saw it. 2 means the command did not parse, or for "
        "aws s3 some files were skipped. 252 is bad syntax or a bad parameter. "
        "253 is bad config or credentials. 254 means the service returned an "
        "error. 255 is a catch-all. 124 means this runner killed the process "
        "at 30 seconds. Read stderr to tell those apart.\n"
        "The CLI returns every page by default. A wide list or describe will "
        "hit the 30 second kill or the output cap. Start with --no-paginate "
        "or --max-items. --page-size does not shrink the result. Filter on "
        "the server first (--filters, --bucket, --names), then --query. "
        "--query is client-side and runs after the download. Use --output json "
        "with --query. Do not use --output text with --query.\n"
        "The CLI already retries throttling and HTTP 500, 502, 503, and 504. "
        "Do not immediately send the same command again on Throttling or "
        "RequestLimitExceeded. Do not pass --debug.\n"
        "Do not use aws <service> wait. Waiters poll and will be killed at "
        "30 seconds. One describe or get is enough. Do not use aws lambda wait.\n"
        "Prefer one API call. aws s3api is one S3 call. aws s3 cp, sync, and "
        "rm are multi-step transfers. aws s3 ls is fine. aws s3 rm, rb, mv, "
        "and sync delete or move data and require approval in Edit or Agent. "
        "Do not pass file:// or fileb://. Put a short JSON document inline. "
        "aws <service> <operation> --generate-cli-skeleton does not call the "
        "service and is rejected here.\n"
        "Lambda. List names with aws lambda list-functions --no-paginate "
        "--query 'Functions[].FunctionName'. Invoke with aws lambda invoke "
        "--function-name NAME --cli-binary-format raw-in-base64-out "
        "--payload '<json>' --cli-read-timeout 20 response.json. CLI v2 "
        "base64-encodes a payload blob unless raw-in-base64-out is set. The "
        "payload must be inline JSON. file:// is not available here. Stdout "
        "StatusCode 200 means Lambda accepted a synchronous call. FunctionError "
        "on that same stdout means the function failed. The error object is in "
        "response.json, not in stdout. Do not treat StatusCode 200 as the "
        "function succeeding when FunctionError is present. Event invocation "
        "returns StatusCode 202 and does not wait for the function result. Use "
        "the default RequestResponse when the user wants the return value. "
        "--cli-read-timeout defaults to 60 seconds, which is longer than this "
        "runner's 30 second kill. Set it to 20 so a slow function returns a "
        "CLI error instead of exit 124 and an empty file. Invoke is not a "
        "read. Ask and Plan cannot invoke.\n"
        "API Gateway. This runner cannot curl a deployed URL. There is no HTTP "
        "client. A REST API call is aws apigateway test-invoke-method "
        "--rest-api-id ID --resource-id ID --http-method GET "
        "--path-with-query-string '/'. The JSON stdout has status, body, "
        "headers, and log. Read body for the integration response. Pass --body "
        "as an inline string, not file://. Find ids first: aws apigateway "
        "get-rest-apis and get-resources. HTTP and WebSocket APIs are aws "
        "apigatewayv2, not aws apigateway. test-invoke-method simulates the "
        "method. It is not a call to the stage URL. It is not a read. Ask and "
        "Plan cannot call it.\n"
        "Where source code lives. A where-is-this-codebase question is one "
        "lookup, not a tour. The first command is Amplify, because list-apps "
        "returns the git URL on each app in the repository field. Filter to "
        "the name the user gave and do not select environmentVariables, which "
        "list-apps also returns:\n"
        "aws amplify list-apps --no-paginate --output json --query "
        "'apps[?contains(name, `NAME`)].{name:name,repository:repository,"
        "defaultDomain:defaultDomain,platform:platform}'\n"
        "If repository is set, stop. Answer with that git URL, the Amplify "
        "app name, and the region. Do not call CodeCommit or CodePipeline "
        "after a match. A list-apps with no --query is refused in Ask and "
        "Plan because the full app includes environmentVariables.\n"
        "Only if that list is empty, check CodeCommit list-repositories with "
        "--no-paginate and a --query on the name. CodeCommit is AWS-hosted "
        "git. It is not where a GitHub repo lives.\n"
        "Do not call codepipeline list-pipelines to find a codebase. That "
        "list returns name, version, created, and updated. It does not return "
        "the git URL. The source action is on get-pipeline, a second call, "
        "and ListPipelines is often missing from the key. If stderr says "
        "AccessDenied or UnauthorizedOperation, this key cannot see that "
        "action. Do not retry it and do not treat the denial as the "
        "codebase being missing. Skip that service and answer from what you "
        "already have.\n"
        "S3 is object storage and Lambda is a deployed function. List those "
        "only after Amplify and CodeCommit miss, and only with --no-paginate "
        "plus a --query that filters on the name the user gave. Do not dump "
        "every bucket or every function. Set intent on_task only for the "
        "next command in that order. Set side for anything else. In Ask and "
        "Plan, after three AWS commands in one user question, stop calling "
        "the tool and answer from what you have, including not found in "
        "Amplify or CodeCommit. The server then returns: Do not call AWS "
        "again this turn. Answer the user now. Edit and Agent are not capped "
        "at three, but they still start with Amplify and stop when a "
        "repository matches.\n"
        "A create that supports a client token should send one. A timeout can "
        "mean the call succeeded and the response was killed. Do not fire a "
        "second create for the same resource.\n"
        "Do not request secret material (secretsmanager get-secret-value, "
        "ssm get-parameter, ssm get-parameters, ssm get-parameters-by-path, "
        "iam create-access-key, iam create-login-profile, kms decrypt, "
        "kms generate-data-key, lambda get-function, "
        "lambda get-function-configuration, ecs describe-task-definition, "
        "codebuild batch-get-projects, rds generate-db-auth-token, "
        "ecr get-login-password, ecr get-authorization-token, s3 presign, "
        "sts assume-role, sts get-session-token, sts get-federation-token) "
        "unless the user asked for that value. Those are not auto-run. Ask "
        "and Plan refuse them. Edit and Agent show a card titled as a secret "
        "read. Never copy those credentials into the next command.\n"
        "Global services (IAM, CloudFront, Route 53) do not need --region. "
        "EC2 --dry-run is a preview, not a change. Most other services have "
        "no dry run. Do not invent one."
    )

