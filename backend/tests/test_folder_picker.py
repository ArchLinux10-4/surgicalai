import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import folder_picker as fp


def _load_helper():
    path = Path(__file__).resolve().parents[2] / "scripts" / "macos-folder-helper.py"
    spec = importlib.util.spec_from_file_location("macos_folder_helper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*completed):
    with mock.patch.object(fp.platform, "system", return_value="Darwin"), \
            mock.patch.object(fp.subprocess, "run", side_effect=list(completed)) as run:
        result: dict = {}
        fp.open_directory_dialog(result, 'Pick "this"')
        return result, run


def _cp(rc, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def test_mac_returns_path_without_trailing_slash():
    result, run = _run(_cp(0, "/Users/me/app/\n"))
    assert result == {"path": "/Users/me/app"}
    args = run.call_args[0][0]
    assert args[-1] == 'Pick "this"'  # prompt is argv, not interpolated
    assert "activate" in " ".join(args)


def test_mac_cancel_is_empty_path_not_error():
    result, _ = _run(_cp(1, "", "execution error: User canceled. (-128)"))
    assert result == {"path": ""}


def test_mac_real_error_is_reported():
    result, _ = _run(_cp(1, "", "boom"))
    assert result == {"error": "boom"}


def test_mac_falls_back_when_automation_denied():
    result, run = _run(_cp(1, "", "Not authorized (-1743)"), _cp(0, "/a/b/\n"))
    assert result == {"path": "/a/b"}
    assert run.call_count == 2


def test_linux_without_helper_or_display_has_no_picker():
    with mock.patch.object(fp.platform, "system", return_value="Linux"), \
            mock.patch.object(fp, "helper_reachable", return_value=False), \
            mock.patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}, clear=False):
        assert fp.native_picker_available() is False


def test_linux_uses_helper_when_it_is_reachable():
    def fill(result, prompt, default_path=""):
        assert prompt == "Hi"
        assert default_path == ""
        result["path"] = "/from/helper"

    with mock.patch.object(fp.platform, "system", return_value="Linux"), \
            mock.patch.object(fp, "helper_reachable", return_value=True), \
            mock.patch.object(fp, "_open_via_helper", side_effect=fill):
        result: dict = {}
        fp.open_directory_dialog(result, "Hi")
        assert result == {"path": "/from/helper"}
        assert fp.native_picker_available() is True


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_helper_down_is_an_error():
    port = _free_port()
    with mock.patch.dict(os.environ, {
        "SURGICALAI_PICKER_URL": f"http://127.0.0.1:{port}",
        "SURGICALAI_PICKER_TOKEN": "tok",
    }):
        result: dict = {}
        fp._open_via_helper(result, "Pick")
        assert "not reachable" in result["error"]


def _start_helper(helper):
    server = helper.ThreadingHTTPServer(("127.0.0.1", 0), helper.PickerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_helper_pick_and_bad_token():
    helper = _load_helper()
    with tempfile.TemporaryDirectory() as tmp:
        token_file = os.path.join(tmp, "token")
        with open(token_file, "w", encoding="utf-8") as handle:
            handle.write("correct-token")
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        args_file = os.path.join(tmp, "args")
        osa = os.path.join(bin_dir, "osascript")
        with open(osa, "w", encoding="utf-8") as handle:
            quoted = "'" + args_file.replace("'", "'\\''") + "'"
            handle.write("#!/bin/sh\nprintf '%s\\n' \"$*\" > " + quoted + "\necho /Users/me/proj/\n")
        os.chmod(osa, 0o755)
        with mock.patch.dict(os.environ, {
            "SURGICALAI_PICKER_TOKEN_FILE": token_file,
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
        }):
            server = _start_helper(helper)
            port = server.server_address[1]
            try:
                bad = urllib.request.Request(
                    f"http://127.0.0.1:{port}/pick",
                    data=b"{}",
                    method="POST",
                    headers={"Content-Type": "application/json", "X-SurgicalAI-Picker-Token": "nope"},
                )
                try:
                    urllib.request.urlopen(bad, timeout=2)
                    raise AssertionError("bad token should be rejected")
                except urllib.error.HTTPError as exc:
                    assert exc.code == 401
                assert not os.path.exists(args_file)

                with mock.patch.dict(os.environ, {
                    "SURGICALAI_PICKER_URL": f"http://127.0.0.1:{port}",
                    "SURGICALAI_PICKER_TOKEN": "correct-token",
                }):
                    result: dict = {}
                    fp._open_via_helper(result, 'Pick "this"')
                    assert result == {"path": "/Users/me/proj"}
                logged = open(args_file, encoding="utf-8").read()
                assert "activate" in logged
                assert 'Pick "this"' in logged

                cancel = os.path.join(bin_dir, "osascript")
                with open(cancel, "w", encoding="utf-8") as handle:
                    handle.write("#!/bin/sh\necho 'execution error: User canceled. (-128)' >&2\nexit 1\n")
                os.chmod(cancel, 0o755)
                with mock.patch.dict(os.environ, {
                    "SURGICALAI_PICKER_URL": f"http://127.0.0.1:{port}",
                    "SURGICALAI_PICKER_TOKEN": "correct-token",
                }):
                    result = {}
                    fp._open_via_helper(result, "Pick")
                    assert result == {"path": ""}
            finally:
                server.shutdown()
                server.server_close()


def test_helper_script_falls_back_when_automation_denied():
    helper = _load_helper()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 1, "", "Not authorized (-1743)")
        return subprocess.CompletedProcess(args, 0, "/a/b/\n", "")

    with mock.patch.object(helper.subprocess, "run", side_effect=run):
        assert helper.choose_folder("Hi") == {"path": "/a/b"}
    assert len(calls) == 2
    assert "activate" not in " ".join(calls[1])
