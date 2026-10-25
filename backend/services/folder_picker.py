"""Native folder picker shared by Settings and Open Folder.

Runs off the request thread. The dialog blocks until the user closes it.

- macOS (backend on the host): osascript. The dialog is shown by Finder.
  System Events is faceless, so a dialog opened there never comes forward.
- Linux container on macOS: there is no display and no osascript. Call the
  host helper (scripts/macos-folder-helper.py), which runs in the GUI session.
- Linux with a display and no helper: tkinter.

The prompt is passed as an argv item, never interpolated into the script.
Cancel (AppleScript error -128) is a normal outcome: empty path, no error.
"""
import json
import os
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

PICKER_PORT = 18765

NO_NATIVE_PICKER_MESSAGE = (
    "A native folder picker isn't available. On macOS with Docker, run ./docker-rebuild-local.sh "
    "so the folder helper is installed, then try again. You can use the folder list below, or type the path."
)

# Hard cap for the dialog; keep below the routers' 120s join timeout so the
# subprocess is always reaped before the request thread gives up.
_DIALOG_TIMEOUT_S = 110

_probe_cache = {"until": 0.0, "ok": False}

# Finder owns the dialog. Keep this in sync with scripts/macos-folder-helper.py.
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

# Plain version, used if macOS Automation permission for Finder was denied
# (AppleScript error -1743). The dialog may open behind other windows.
_MAC_SCRIPT_PLAIN = [
    "on run argv",
    "  set promptText to item 1 of argv",
    "  set chosen to choose folder with prompt promptText",
    "  return POSIX path of chosen",
    "end run",
]


def _helper_url() -> str:
    configured = (os.environ.get("SURGICALAI_PICKER_URL") or "").strip()
    base = configured or f"http://host.docker.internal:{PICKER_PORT}"
    return base.rstrip("/")


def _token_file() -> Path:
    override = (os.environ.get("SURGICALAI_PICKER_TOKEN_FILE") or "").strip()
    if override:
        return Path(override)
    browse = (os.environ.get("SURGICALAI_BROWSE_ROOT") or "").strip()
    base = Path(browse) if browse else Path.home()
    return base / ".surgicalai" / "folder-helper-token"


def picker_token() -> str:
    env = (os.environ.get("SURGICALAI_PICKER_TOKEN") or "").strip()
    if env:
        return env
    try:
        return _token_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _remember_helper(ok: bool) -> None:
    _probe_cache["ok"] = ok
    _probe_cache["until"] = time.monotonic() + (5.0 if ok else 2.0)


def helper_reachable(force: bool = False) -> bool:
    """True when the macOS folder helper answers /health. Cached briefly."""
    now = time.monotonic()
    if not force and now < _probe_cache["until"]:
        return bool(_probe_cache["ok"])
    ok = False
    try:
        req = urllib.request.Request(_helper_url() + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=0.5) as resp:
            ok = resp.status == 200
    except Exception:
        ok = False
    _remember_helper(ok)
    return ok


def native_picker_available() -> bool:
    """False when this process cannot open a dialog and no host helper is up."""
    if platform.system() == "Darwin":
        return shutil.which("osascript") is not None
    if helper_reachable():
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _mac_args(prompt: str, script: list[str] | None = None, default_path: str = "") -> list[str]:
    args = ["osascript"]
    for line in (script or _MAC_SCRIPT_FRONT):
        args += ["-e", line]
    args.append(prompt)
    if default_path:
        args.append(default_path)
    return args


def _run_mac(prompt: str, script: list[str], default_path: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        _mac_args(prompt, script, default_path),
        capture_output=True,
        text=True,
        timeout=_DIALOG_TIMEOUT_S,
    )


def _open_mac(result: dict, prompt: str, default_path: str = "") -> None:
    try:
        proc = _run_mac(prompt, _MAC_SCRIPT_FRONT, default_path)
        if proc.returncode != 0 and "(-1743)" in (proc.stderr or ""):
            proc = _run_mac(prompt, _MAC_SCRIPT_PLAIN, default_path)
    except subprocess.TimeoutExpired:
        result["error"] = "Folder picker timed out"
        return
    except FileNotFoundError:
        result["error"] = "osascript not found"
        return

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        if "(-128)" in stderr or "user canceled" in stderr.lower() or "user cancelled" in stderr.lower():
            result["path"] = ""
        else:
            result["error"] = stderr or f"osascript failed ({proc.returncode})"
        return
    # Keep "/" intact; strip the trailing slash AppleScript adds to folders.
    result["path"] = stdout.rstrip("/") or ("/" if stdout else "")


def _open_via_helper(result: dict, prompt: str, default_path: str = "") -> None:
    token = picker_token()
    if not token:
        result["error"] = "Folder picker helper token is missing"
        return
    payload_out = {"prompt": prompt}
    if default_path:
        payload_out["default_path"] = default_path
    body = json.dumps(payload_out).encode("utf-8")
    req = urllib.request.Request(
        _helper_url() + "/pick",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-SurgicalAI-Picker-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_DIALOG_TIMEOUT_S + 5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code == 401:
            result["error"] = "Folder picker helper rejected the token"
        else:
            result["error"] = f"Folder picker helper failed ({exc.code}): {detail}"
        return
    except Exception as exc:
        _remember_helper(False)
        result["error"] = f"Folder picker helper is not reachable ({exc})"
        return
    if not isinstance(payload, dict):
        result["error"] = "Folder picker helper returned an unexpected response"
        return
    if payload.get("error"):
        result["error"] = str(payload["error"])
        return
    result["path"] = payload.get("path") or ""


def open_directory_dialog(result: dict, prompt: str = "Select a folder", default_path: str = "") -> None:
    """Fill result['path'] or result['error']. Never raises to the caller thread."""
    try:
        if platform.system() == "Darwin":
            _open_mac(result, prompt, default_path)
            return
        if helper_reachable():
            _open_via_helper(result, prompt, default_path)
            return

        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        result["root"] = root
        path = filedialog.askdirectory(title=prompt)
        root.destroy()
        result["path"] = path or ""
    except Exception as exc:
        result["error"] = str(exc)
