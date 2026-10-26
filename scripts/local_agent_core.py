"""Local Mac actions for the SurgicalAI host helper.

Plain functions so tests can call them without a socket. Python 3.9 compatible:
the LaunchAgent runs the system interpreter.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from urllib.parse import urlparse

MAX_BODY_BYTES = 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_CHARS = 32_000
MAX_LIST_ENTRIES = 500
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
TRUNC_NOTE = "[output truncated at 32000 characters]"

_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1", "host.docker.internal")
_LOOPBACK_ORIGINS = ("127.0.0.1", "localhost", "::1", "host.docker.internal")
_DENY_EXE = {"sudo", "su", "doas"}
_SHELLS = {"bash", "zsh", "sh"}
_WRITERS = {
    "rm", "mv", "cp", "tee", "install", "chmod", "chown", "ln", "touch",
    "mkdir", "rmdir", "unlink", "trash",
}
_PROTECTED_MARKERS = ("/.ssh", "/.gnupg", "/.aws", "/Library/Keychains")
_PATH_CACHE = {"value": ""}


def content_sha(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def protected_roots():
    home = os.path.expanduser("~")
    names = (".ssh", ".gnupg", ".aws", os.path.join("Library", "Keychains"))
    roots = []
    for name in names:
        roots.append(os.path.realpath(os.path.join(home, name)))
    return roots


def path_is_protected(path):
    if not path:
        return False
    raw = os.path.expanduser(str(path))
    if not os.path.isabs(raw):
        raw = os.path.abspath(raw)
    resolved = os.path.realpath(raw)
    for root in protected_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    return False


def deny_reason(argv):
    """Second line of defense. Returns a short reason, or None."""
    if not argv:
        return "empty command"
    exe = os.path.basename(str(argv[0]))
    if exe in _DENY_EXE:
        return "sudo is not allowed"
    script = " ".join(str(part) for part in argv)
    if exe in _SHELLS and any(flag in argv for flag in ("-lc", "-c", "-l")):
        for marker in _PROTECTED_MARKERS:
            if marker in script:
                return "that path is protected"
    if exe in _WRITERS:
        for arg in argv[1:]:
            text = str(arg)
            if text.startswith("-"):
                continue
            if path_is_protected(text):
                return "that path is protected"
    return None


def host_allowed(host_header, port):
    if not host_header:
        return False
    host = str(host_header).strip().lower()
    name, got_port = _split_host(host)
    if got_port and str(got_port) != str(port):
        return False
    return name in _ALLOWED_HOSTS


def origin_allowed(origin):
    if not origin:
        return True
    try:
        parsed = urlparse(str(origin))
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return host in _LOOPBACK_ORIGINS


def content_type_ok(value):
    if not value:
        return False
    media = str(value).split(";", 1)[0].strip().lower()
    return media == "application/json"


def _split_host(host):
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return host, ""
        name = host[: end + 1]
        rest = host[end + 1:]
        if rest.startswith(":"):
            return name, rest[1:]
        return name, ""
    if host.count(":") == 1:
        name, port = host.split(":", 1)
        return name, port
    return host, ""


def login_path():
    if _PATH_CACHE["value"]:
        return _PATH_CACHE["value"]
    try:
        proc = subprocess.run(
            ["/bin/zsh", "-lc", 'printf %s "$PATH"'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        found = (proc.stdout or "").strip()
    except Exception:
        found = ""
    found = found or os.environ.get("PATH") or "/usr/bin:/bin"
    parts = [part for part in found.split(":") if part]
    home = os.path.expanduser("~")
    for extra in ("/opt/homebrew/bin", "/usr/local/bin", os.path.join(home, "bin")):
        if extra not in parts and os.path.isdir(extra):
            parts.insert(0, extra)
    _PATH_CACHE["value"] = ":".join(parts)
    return _PATH_CACHE["value"]


def child_env():
    env = {}
    for key in ("HOME", "USER", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["LANG"] = os.environ.get("LANG") or "C.UTF-8"
    env["PATH"] = login_path()
    return env


def _cap(text):
    text = text or ""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS] + "\n" + TRUNC_NOTE, True


def _clamp_timeout(timeout_s):
    try:
        value = float(timeout_s if timeout_s is not None else DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_S
    if value < 1:
        value = 1
    if value > MAX_TIMEOUT_S:
        value = MAX_TIMEOUT_S
    return value


def run_exec(argv, cwd="", timeout_s=None):
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
        return {"error": "argv must be a list of strings", "status": 400}
    reason = deny_reason(argv)
    if reason:
        return {"error": reason, "status": 403}
    work = cwd or os.getcwd()
    if not os.path.isdir(work):
        return {"error": "cwd is not a directory", "status": 400}
    timeout = _clamp_timeout(timeout_s)
    started = time.time()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=work,
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        name = os.path.basename(argv[0]) or argv[0]
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": name + ": command not found",
            "timed_out": False,
            "truncated": False,
            "duration_ms": int((time.time() - started) * 1000),
        }
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        stdout, stderr = proc.communicate()
        code = None
    out, out_cut = _cap(stdout)
    err, err_cut = _cap(stderr)
    return {
        "exit_code": code,
        "stdout": out,
        "stderr": err,
        "timed_out": timed_out,
        "truncated": out_cut or err_cut,
        "duration_ms": int((time.time() - started) * 1000),
    }


def _read_capped(path):
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        return None, size, "file is larger than 5 MiB"
    with open(path, "rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if b"\x00" in data:
        return None, size, "binary"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, size, "binary"
    return text, size, ""


def read_file(path, offset=None, limit=None):
    if not path or not isinstance(path, str):
        return {"error": "path is required", "status": 400}
    if not os.path.isfile(path):
        return {"error": "not a file", "status": 404}
    text, size, problem = _read_capped(path)
    if problem == "binary":
        return {"content": "", "size": size, "sha": "", "truncated": False, "binary": True}
    if problem:
        return {"error": problem, "status": 413}
    lines = text.splitlines(keepends=True)
    start = 0
    end = len(lines)
    if offset:
        try:
            start = max(0, int(offset) - 1)
        except (TypeError, ValueError):
            start = 0
    if limit:
        try:
            end = start + max(0, int(limit))
        except (TypeError, ValueError):
            end = len(lines)
    chosen = "".join(lines[start:end])
    capped, truncated = _cap(chosen)
    return {
        "content": capped,
        "size": size,
        "sha": content_sha(text),
        "truncated": truncated or end < len(lines),
        "binary": False,
    }


def write_file(path, content, expected_sha=""):
    if not path or not isinstance(path, str):
        return {"error": "path is required", "status": 400}
    if not isinstance(content, str):
        return {"error": "content must be text", "status": 400}
    if path_is_protected(path):
        return {"error": "that path is protected", "status": 403}
    parent = os.path.dirname(path) or "."
    if not os.path.isdir(parent):
        return {"error": "parent directory does not exist", "status": 400}
    existed = os.path.isfile(path)
    current = ""
    if existed:
        text, _size, problem = _read_capped(path)
        if problem == "binary":
            return {"error": "refusing to overwrite a binary file", "status": 409}
        if problem:
            return {"error": problem, "status": 413}
        current = text or ""
    if expected_sha:
        if content_sha(current) != expected_sha:
            return {
                "conflict": True,
                "current_sha": content_sha(current) if existed else "",
                "status": 409,
            }
    tmp_name = ""
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent, delete=False, prefix=".sai-write-"
        )
        tmp_name = handle.name
        with handle:
            handle.write(content)
        os.replace(tmp_name, path)
        tmp_name = ""
    except Exception:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
        return {"error": "write failed", "status": 500}
    return {"ok": True, "sha": content_sha(content), "created": not existed}


def list_dir(path):
    if not path or not isinstance(path, str):
        return {"error": "path is required", "status": 400}
    if not os.path.isdir(path):
        return {"error": "not a directory", "status": 404}
    names = sorted(os.listdir(path))
    truncated = len(names) > MAX_LIST_ENTRIES
    entries = []
    for name in names[:MAX_LIST_ENTRIES]:
        full = os.path.join(path, name)
        try:
            st = os.lstat(full)
            kind = "dir" if os.path.isdir(full) else "file"
            size = int(st.st_size)
        except OSError:
            kind = "file"
            size = 0
        entries.append({"name": name, "type": kind, "size": size})
    return {"entries": entries, "truncated": truncated}


def audit(log_path, record):
    if not log_path:
        return
    try:
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        line = json.dumps(record, sort_keys=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return
