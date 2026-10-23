"""AWS CLI runner and access-key check.

The terminal and every agent call run_aws_cli. The process is always the
aws binary with shell=False. Secrets stay in the environment.
"""
from __future__ import annotations

import asyncio
import json
import os
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


def aws_agent_guidance() -> str:
    """Prompt text for every mode that can run the connected AWS CLI.

    Matches ``_reject_command`` and ``normalize_aws_command``. Callers must
    not paraphrase a shorter version.
    """
    return (
        "How to call AWS. Claude, GPT, Ask, Plan, Edit, and Agent emit exactly "
        "one tag and then stop. The body is the command, not JSON:\n"
        "<aws_cli>aws sts get-caller-identity --output json</aws_cli>\n"
        "Grok does not emit that tag. Call the aws_cli function with "
        "{\"command\": \"aws sts get-caller-identity --output json\"}. "
        "One call per turn. A second call in the same turn is dropped.\n"
        "Then stop. The user approves, edits, or rejects that command before "
        "it runs. Do not also write the answer in that message. If they reject "
        "it, the next message says they rejected it. Do not retry a rephrased "
        "version unless they ask. If they approve after editing, the approved "
        "string is what ran. Do not assume your original text ran.\n"
        "How to read the result. The next turn is plain text:\n"
        "AWS CLI exit <code>.\nstdout:\n<stdout>\nstderr:\n<stderr>\n"
        "Trust the exit code, then read stderr before inventing a cause. "
        "stdout with --output json is one JSON document. Do not treat a "
        "non-zero exit plus empty stdout as an empty account. A truncated "
        "stdout is the start of the real response: narrow it with --query or "
        "--max-items and call again. Do not guess missing rows. Empty stdout "
        "and empty stderr with exit 0 is a real empty result. Say that and do "
        "not repeat the command. The next command may only use an id, name, "
        "or ARN that appeared in stdout.\n"
        "What this CLI accepts. One command. It must start with aws. The "
        "process splits it with shlex and runs shell=False. Rejected: a raw "
        "newline, and any of ; | & ` $ < >. No &&, pipes, redirects, or $(). "
        "A backslash at the end of a line is a continuation and is joined into "
        "one command. A newline that is not a continuation is rejected. "
        "aws configure is not allowed. Do not use --profile or aws sso login. "
        "The process home is an empty temp directory, so there is no profile "
        "and no saved config. Credentials and region come from the connected "
        "account. Never put keys in the command. The user sees the command on "
        "the approval card. --region only when overriding that connected "
        "region. The pager and auto-prompt are off. The run times out at 30 "
        "seconds. Output is capped. The working directory is empty, so "
        "aws s3 cp ./file s3://bucket has no file to read and a download does "
        "not land on the user's machine.\n"
        "How to use any AWS service. Every service is aws <service> <operation> "
        "[options]. Examples: aws s3 ls, aws ec2 describe-instances, "
        "aws iam list-users, aws lambda list-functions. Use the CLI operation "
        "name, not a boto3 method name. Prefer a read first "
        "(sts get-caller-identity, describe-*, list-*, get-*) so the next "
        "command is grounded in this account. Do not pipe to jq or grep. Use "
        "--output json and --query. Quote values that contain spaces. Do not "
        "rely on an interactive prompt. A mutating command (delete-*, "
        "terminate-*, put-*, create-*) is allowed only when the user asked "
        "for that change. They still approve the exact string. If stderr says "
        "the command was rejected, send one corrected command. Do not wrap it "
        "in a shell.\n"
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
        "30 seconds. One describe or get is enough.\n"
        "Prefer one API call. aws s3api is one S3 call. aws s3 cp, sync, and "
        "rm are multi-step transfers that need local files this directory does "
        "not have, and they can return exit 1 or 2 after a partial copy. "
        "aws s3 ls is fine. Do not pass file:// or fileb://. Put a short JSON "
        "document inline. aws <service> <operation> --generate-cli-skeleton "
        "does not call the service.\n"
        "A create that supports a client token should send one. A timeout can "
        "mean the call succeeded and the response was killed. Do not fire a "
        "second create for the same resource.\n"
        "Do not request secret material (secretsmanager get-secret-value, "
        "ssm get-parameter --with-decryption, iam create-access-key, "
        "sts assume-role) unless the user asked for that value. Never copy "
        "those credentials into the next command.\n"
        "Global services (IAM, CloudFront, Route 53) do not need --region. "
        "EC2 --dry-run is a preview, not a change. Most other services have "
        "no dry run. Do not invent one.\n"
        "Ask and Plan stay read-only for the repo. They may still run AWS "
        "commands. Edit and Agent use the same AWS rules and may follow a "
        "read with a mutation the user asked for."
    )


def run_aws_cli(user_id: str, command: str) -> dict:
    command = normalize_aws_command(command)
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
        # v2 pages through less and can auto-prompt. Either one hangs this
        # captured 30s run. Empty pager and auto-prompt off keep stdout here.
        env["AWS_PAGER"] = ""
        env["AWS_CLI_AUTO_PROMPT"] = "off"
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


async def wait_aws_decision(command: str, client_inbox, timeout: float = 180) -> str | None:
    """Wait for the user to approve, edit, or reject an agent AWS command.

    Returns the command to run, "" if rejected or timed out, or None when
    there is no back-channel (plain HTTP) and the command must not run.
    """
    if client_inbox is None:
        return None
    try:
        while True:
            client_inbox.get_nowait()
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = await asyncio.wait_for(client_inbox.get(), timeout=5)
        except asyncio.TimeoutError:
            continue
        if not isinstance(msg, dict) or msg.get("type") != "aws_cli_response":
            continue
        action = str(msg.get("action") or "").lower()
        if action == "reject":
            return ""
        if action in ("approve", "edit"):
            edited = str(msg.get("command") or command).strip()
            return edited or command
    return ""


def mask_access_key_id(key_id: str) -> str:
    key = key_id or ""
    if len(key) < 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
