"""AWS CLI runner and access-key check.

The terminal and every agent call run_aws_cli. The process is always the
aws binary with shell=False. Secrets stay in the environment.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile

from crypto_utils import decrypt_api_key
from database import get_user_api_key

AWS_REGIONS = (
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-central-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
)
_KEY_RE_PREFIXES = ("AKIA", "ASIA")
_FORBIDDEN = set(";|&`$<>")
_OUTPUT_CAP = 32_000


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
    if region_s not in AWS_REGIONS:
        raise ValueError("Choose a supported AWS region.")
    token = (session_token or "").strip()
    if key_id.startswith("ASIA") and not token:
        raise ValueError("Session token is required for temporary keys.")
    return {
        "access_key_id": key_id,
        "secret_access_key": secret_s,
        "region": region_s,
        "session_token": token,
    }


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
    return {"account": ident.get("Account") or "", "arn": ident.get("Arn") or ""}


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
    if len(argv) > 1 and argv[1] == "configure":
        return "aws configure is not allowed."
    return None


def run_aws_cli(user_id: str, command: str) -> dict:
    reason = _reject_command(command)
    if reason:
        return {"exit_code": 1, "stdout": "", "stderr": reason}
    creds = None
    try:
        creds = _load_creds(user_id)
    except Exception:
        creds = None
    if not creds:
        return {"exit_code": 1, "stdout": "", "stderr": "AWS is not connected."}
    argv = shlex.split(command)
    aws_bin = shutil.which("aws")
    if not aws_bin:
        return {"exit_code": 1, "stdout": "", "stderr": "AWS CLI is not installed on the server."}
    argv[0] = aws_bin
    tmp = tempfile.mkdtemp(prefix="awscli-")
    try:
        cred_path = os.path.join(tmp, "credentials")
        with open(cred_path, "w", encoding="utf-8") as fh:
            fh.write("")
        env = os.environ.copy()
        env["HOME"] = tmp
        env["AWS_SHARED_CREDENTIALS_FILE"] = cred_path
        env["AWS_ACCESS_KEY_ID"] = creds.get("access_key_id") or ""
        env["AWS_SECRET_ACCESS_KEY"] = creds.get("secret_access_key") or ""
        env["AWS_DEFAULT_REGION"] = creds.get("region") or "us-east-1"
        token = creds.get("session_token") or ""
        if token:
            env["AWS_SESSION_TOKEN"] = token
        else:
            env.pop("AWS_SESSION_TOKEN", None)
        try:
            proc = subprocess.run(
                argv, shell=False, timeout=30, capture_output=True,
                cwd=tmp, env=env, text=True,
            )
        except subprocess.TimeoutExpired:
            return {"exit_code": 124, "stdout": "", "stderr": "AWS CLI timed out."}
        return {
            "exit_code": int(proc.returncode),
            "stdout": (proc.stdout or "")[:_OUTPUT_CAP],
            "stderr": (proc.stderr or "")[:_OUTPUT_CAP],
        }
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def mask_access_key_id(key_id: str) -> str:
    key = key_id or ""
    if len(key) < 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
