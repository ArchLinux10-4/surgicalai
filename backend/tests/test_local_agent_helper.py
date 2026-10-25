"""Host helper exec/read/write/list. No network and no real LaunchAgent."""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import local_agent_core as core  # noqa: E402


def _load_helper():
    import importlib.util
    path = SCRIPTS / "macos-folder-helper.py"
    spec = importlib.util.spec_from_file_location("macos_folder_helper_local", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _start(helper):
    server = helper.ThreadingHTTPServer(("127.0.0.1", 0), helper.PickerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _req(port, path, body=None, token="secret", headers=None, method="POST"):
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "X-SurgicalAI-Local-Token": token}
    if headers:
        hdrs.update(headers)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers=hdrs,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload


def test_deny_sudo_and_protected_write(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert core.deny_reason(["sudo", "ls"]) == "sudo is not allowed"
    assert core.deny_reason(["/usr/bin/su", "-"]) == "sudo is not allowed"
    secret = tmp_path / ".ssh"
    secret.mkdir()
    assert core.path_is_protected(str(secret / "id_rsa"))
    assert core.deny_reason(["rm", str(secret / "id_rsa")]) == "that path is protected"
    assert core.deny_reason(["bash", "-lc", "echo hi > ~/.ssh/authorized_keys"]) == "that path is protected"
    assert core.deny_reason(["/bin/echo", "hi"]) is None


def test_exec_read_write_list_and_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ran = core.run_exec(["/bin/echo", "hello"], cwd=str(tmp_path), timeout_s=5)
    assert ran["exit_code"] == 0
    assert "hello" in ran["stdout"]
    assert ran["timed_out"] is False

    slow = core.run_exec(["/bin/sleep", "30"], cwd=str(tmp_path), timeout_s=1)
    assert slow["timed_out"] is True
    assert slow["exit_code"] is None

    target = tmp_path / "note.txt"
    wrote = core.write_file(str(target), "one\n")
    assert wrote["ok"] is True
    assert wrote["created"] is True
    assert not list(tmp_path.glob(".sai-write-*"))
    conflict = core.write_file(str(target), "two\n", expected_sha="not-the-sha")
    assert conflict["status"] == 409
    assert conflict["conflict"] is True
    assert target.read_text(encoding="utf-8") == "one\n"
    again = core.write_file(str(target), "two\n", expected_sha=wrote["sha"])
    assert again["ok"] is True
    read = core.read_file(str(target), offset=1, limit=1)
    assert read["content"].startswith("two")
    assert read["binary"] is False
    listed = core.list_dir(str(tmp_path))
    assert any(item["name"] == "note.txt" for item in listed["entries"])

    blocked = core.write_file(str(tmp_path / ".ssh" / "id_rsa"), "nope")
    (tmp_path / ".ssh").mkdir()
    blocked = core.write_file(str(tmp_path / ".ssh" / "id_rsa"), "nope")
    assert blocked["status"] == 403


def test_truncation_and_binary(tmp_path):
    huge = core.run_exec([sys.executable, "-c", "print('x' * 40000)"], cwd=str(tmp_path), timeout_s=5)
    assert huge["truncated"] is True
    assert "truncated" in huge["stdout"]
    blob = tmp_path / "blob"
    blob.write_bytes(b"abc\x00def")
    read = core.read_file(str(blob))
    assert read["binary"] is True
    assert read["content"] == ""


def test_audit_has_no_content(tmp_path):
    log = tmp_path / "local-agent.log"
    core.audit(str(log), {
        "endpoint": "/write",
        "path": "note.txt",
        "exit_code": None,
        "duration_ms": 3,
        "status": 200,
    })
    text = log.read_text(encoding="utf-8")
    assert "note.txt" in text
    assert "secret-body" not in text


def test_http_gates(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    log = tmp_path / "agent.log"
    monkeypatch.setenv("SURGICALAI_LOCAL_AGENT_TOKEN_FILE", str(token))
    monkeypatch.setenv("SURGICALAI_LOCAL_AGENT_LOG", str(log))
    monkeypatch.setenv("HOME", str(tmp_path))
    helper = _load_helper()
    server = _start(helper)
    port = server.server_address[1]
    try:
        code, _ = _req(port, "/exec", {"argv": ["/bin/echo", "hi"], "cwd": str(tmp_path)}, token="")
        assert code == 401
        code, _ = _req(port, "/exec", {"argv": ["/bin/echo", "hi"]}, token="nope")
        assert code == 401
        code, payload = _req(
            port, "/exec", {"argv": ["/bin/echo", "hi"], "cwd": str(tmp_path)},
            headers={"Host": "evil.example"},
        )
        assert code == 403
        assert payload["error"] == "forbidden host"
        code, _ = _req(
            port, "/exec", {"argv": ["/bin/echo", "hi"], "cwd": str(tmp_path)},
            headers={"Origin": "https://evil.example"},
        )
        assert code == 403
        code, _ = _req(
            port, "/exec", {"argv": ["/bin/echo", "hi"], "cwd": str(tmp_path)},
            headers={"Content-Type": "text/plain"},
        )
        assert code == 415
        code, payload = _req(port, "/exec", {"argv": ["sudo", "ls"], "cwd": str(tmp_path)})
        assert code == 403
        assert "sudo" in payload["error"]
        code, payload = _req(port, "/exec", {"argv": ["/bin/echo", "ok"], "cwd": str(tmp_path)})
        assert code == 200
        assert "ok" in payload["stdout"]
        health = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(health, timeout=2) as resp:
            assert resp.status == 200
        logged = log.read_text(encoding="utf-8")
        assert "/exec" in logged
        assert "ok" not in logged.split("echo")[-1] or "stdout" not in logged
        assert "stdout" not in logged
    finally:
        server.shutdown()
        server.server_close()


def test_host_allowlist_includes_docker_host():
    assert core.host_allowed("host.docker.internal:18765", 18765)
    assert core.host_allowed("127.0.0.1:18765", 18765)
    assert core.host_allowed("[::1]:18765", 18765)
    assert not core.host_allowed("evil.example:18765", 18765)
    assert not core.host_allowed("127.0.0.1:9", 18765)
    assert core.origin_allowed("")
    assert core.origin_allowed("http://127.0.0.1:8000")
    assert not core.origin_allowed("https://evil.example")
