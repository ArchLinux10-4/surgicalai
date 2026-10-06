"""AWS CLI runner: key shape, shell rejection, env vs argv. No live AWS calls."""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import HTTPException

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from routers.aws import ConnectBody, aws_connect  # noqa: E402
from services import aws_cli  # noqa: E402


class _Req:
    def __init__(self):
        self.state = type("S", (), {"user_id": "user-a"})()


def test_bad_access_key_id_does_not_call_sts(monkeypatch):
    called = []
    monkeypatch.setattr(aws_cli, "sts_identity", lambda *a, **k: called.append(1))
    with pytest.raises(HTTPException) as ei:
        aws_connect(ConnectBody(access_key_id="not-a-key", secret_access_key="s" * 20), _Req())
    assert ei.value.status_code == 400
    assert called == []


def test_shell_metacharacters_do_not_start_a_process(monkeypatch):
    monkeypatch.setattr(aws_cli, "_load_creds", lambda uid: {
        "access_key_id": "AKIAaaaaaaaaaaaaaaaa",
        "secret_access_key": "secretvalue123456",
        "region": "us-east-1",
    })
    monkeypatch.setattr(aws_cli.shutil, "which", lambda name: "/usr/bin/aws")
    started = []
    monkeypatch.setattr(aws_cli.subprocess, "run", lambda *a, **k: started.append(1))
    out = aws_cli.run_aws_cli("user-a", "aws s3 ls; rm -rf /")
    assert out["exit_code"] == 1
    assert started == []
    out_ls = aws_cli.run_aws_cli("user-a", "ls")
    assert out_ls["exit_code"] == 1
    assert started == []


def test_run_passes_key_in_env_not_argv(monkeypatch):
    secret = "secretvalue123456"
    monkeypatch.setattr(aws_cli, "_load_creds", lambda uid: {
        "access_key_id": "AKIAaaaaaaaaaaaaaaaa",
        "secret_access_key": secret,
        "region": "us-east-1",
    })
    monkeypatch.setattr(aws_cli.shutil, "which", lambda name: "/usr/bin/aws")
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env") or {}
        captured["shell"] = kwargs.get("shell")
        return _Proc()

    monkeypatch.setattr(aws_cli.subprocess, "run", _run)
    out = aws_cli.run_aws_cli("user-a", "aws sts get-caller-identity")
    assert out["stdout"] == "ok"
    assert captured["shell"] is False
    assert captured["env"]["AWS_ACCESS_KEY_ID"] == "AKIAaaaaaaaaaaaaaaaa"
    assert secret not in captured["argv"]
