#!/usr/bin/env python3
"""Host-side folder picker for SurgicalAI running in Docker.

The backend container is Linux and cannot open a Mac dialog. This process
runs in the logged-in GUI session (see scripts/install-folder-helper.sh) and
listens on 127.0.0.1 only. The backend calls POST /pick; the browser never
talks to this port.

Python 3.9 compatible: macOS still ships 3.9, and Homebrew 3.x is optional.
Keep the AppleScript in sync with backend/services/folder_picker.py.
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import local_agent_core as local_agent

PICKER_PORT = 18765
_DIALOG_TIMEOUT_S = 110
_dialog_lock = threading.Lock()

# Finder owns the dialog. System Events is a faceless background process, so a
# dialog opened there never comes to the front and Browse looks broken.
_MAC_SCRIPT_FRONT = [
    "on run argv",
    "  set promptText to item 1 of argv",
    "  set defaultPath to \"\"",
    "  if (count of argv) > 1 then set defaultPath to item 2 of argv",
    '  tell application "Finder"',
    "    activate",
    "    if defaultPath is \"\" then",
    "      set chosen to choose folder with prompt promptText",
    "    else",
    "      set chosen to choose folder with prompt promptText default location (POSIX file defaultPath)",
    "    end if",
    "  end tell",
    "  return POSIX path of chosen",
    "end run",
]

_MAC_SCRIPT_PLAIN = [
    "on run argv",
    "  set promptText to item 1 of argv",
    "  set chosen to choose folder with prompt promptText",
    "  return POSIX path of chosen",
    "end run",
]


def token_path():
    override = (os.environ.get("SURGICALAI_PICKER_TOKEN_FILE") or "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".surgicalai", "folder-helper-token")


def read_token():
    try:
        with open(token_path(), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def local_token_path():
    override = (os.environ.get("SURGICALAI_LOCAL_AGENT_TOKEN_FILE") or "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".surgicalai", "local-agent-token")


def read_local_token():
    try:
        with open(local_token_path(), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def local_log_path():
    override = (os.environ.get("SURGICALAI_LOCAL_AGENT_LOG") or "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".surgicalai", "local-agent.log")


def _authorized(presented, expected):
    if not presented or not expected:
        return False
    try:
        return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


def _run_osascript(prompt, script, default_path=""):
    args = ["osascript"]
    for line in script:
        args.extend(["-e", line])
    args.append(prompt)
    if default_path:
        args.append(default_path)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=_DIALOG_TIMEOUT_S,
    )


def choose_folder(prompt, default_path=""):
    """Return {"path": str} or {"error": str}. Cancel is {"path": ""}."""
    if not _dialog_lock.acquire(timeout=1):
        return {"error": "A folder dialog is already open", "status": 409}
    try:
        try:
            proc = _run_osascript(prompt, _MAC_SCRIPT_FRONT, default_path)
            if proc.returncode != 0 and "(-1743)" in (proc.stderr or ""):
                proc = _run_osascript(prompt, _MAC_SCRIPT_PLAIN, default_path)
        except subprocess.TimeoutExpired:
            return {"error": "Folder picker timed out"}
        except FileNotFoundError:
            return {"error": "osascript not found"}
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            lowered = stderr.lower()
            if "(-128)" in stderr or "user canceled" in lowered or "user cancelled" in lowered:
                return {"path": ""}
            return {"error": stderr or "osascript failed ({0})".format(proc.returncode)}
        stdout = (proc.stdout or "").strip()
        return {"path": stdout.rstrip("/") or ("/" if stdout else "")}
    finally:
        _dialog_lock.release()


def _clean_prompt(raw):
    text = " ".join(str(raw or "").split())
    return (text[:200] or "Select a folder")


class PickerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _bound_port(self):
        return int(self.server.server_address[1])

    def _gate(self, require_json):
        port = self._bound_port()
        if not local_agent.host_allowed(self.headers.get("Host", ""), port):
            return 403, {"error": "forbidden host"}
        if not local_agent.origin_allowed(self.headers.get("Origin", "")):
            return 403, {"error": "forbidden origin"}
        if require_json and not local_agent.content_type_ok(self.headers.get("Content-Type", "")):
            return 415, {"error": "content type must be application/json"}
        return 0, None

    def _read_json(self, limit):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length < 0 or length > limit:
            return None, 413, {"error": "body too large"}
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (ValueError, UnicodeError):
            return None, 400, {"error": "invalid json"}
        return body, 0, None

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/health":
            self._send(404, {"error": "not found"})
            return
        status, payload = self._gate(False)
        if status:
            self._send(status, payload)
            return
        self._send(200, {"ok": True})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/pick":
            self._post_pick()
            return
        if path in ("/exec", "/read", "/write", "/list"):
            self._post_local(path)
            return
        self._send(404, {"error": "not found"})

    def _post_pick(self):
        status, payload = self._gate(True)
        if status:
            self._send(status, payload)
            return
        if not _authorized(self.headers.get("X-SurgicalAI-Picker-Token", ""), read_token()):
            self._send(401, {"error": "unauthorized"})
            return
        body, status, payload = self._read_json(4096)
        if status:
            self._send(status, payload)
            return
        default_path = str(body.get("default_path") or "")
        if "\x00" in default_path or not os.path.isdir(default_path):
            default_path = ""
        else:
            default_path = os.path.realpath(default_path)
        result = choose_folder(_clean_prompt(body.get("prompt")), default_path)
        status = int(result.pop("status", 200))
        self._send(status, result)

    def _post_local(self, path):
        status, payload = self._gate(True)
        if status:
            self._send(status, payload)
            return
        if not _authorized(self.headers.get("X-SurgicalAI-Local-Token", ""), read_local_token()):
            self._send(401, {"error": "unauthorized"})
            return
        body, status, payload = self._read_json(local_agent.MAX_BODY_BYTES)
        if status:
            self._send(status, payload)
            return
        if path == "/exec":
            result = local_agent.run_exec(
                body.get("argv"),
                str(body.get("cwd") or ""),
                body.get("timeout_s"),
            )
            audit_path = str(body.get("cwd") or "")
        elif path == "/read":
            result = local_agent.read_file(body.get("path"), body.get("offset"), body.get("limit"))
            audit_path = str(body.get("path") or "")
        elif path == "/write":
            result = local_agent.write_file(
                body.get("path"),
                body.get("content"),
                str(body.get("expected_sha") or ""),
            )
            audit_path = str(body.get("path") or "")
        else:
            result = local_agent.list_dir(body.get("path"))
            audit_path = str(body.get("path") or "")
        status = int(result.pop("status", 200))
        local_agent.audit(local_log_path(), {
            "endpoint": path,
            "path": audit_path[:180],
            "exit_code": result.get("exit_code"),
            "duration_ms": result.get("duration_ms"),
            "status": status,
        })
        self._send(status, result)


def serve(port=None):
    chosen = int(port if port is not None else os.environ.get("SURGICALAI_PICKER_PORT", PICKER_PORT))
    server = ThreadingHTTPServer(("127.0.0.1", chosen), PickerHandler)
    print("SurgicalAI folder helper listening on 127.0.0.1:%s" % server.server_address[1], flush=True)
    server.serve_forever()


def main():
    serve()


if __name__ == "__main__":
    main()
