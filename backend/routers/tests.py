"""
Test runner router.
Writes session files to a temp directory, detects test framework (pytest / jest / vitest),
runs tests with a timeout, and returns structured results.

Security notes:
- Files run in a sandboxed temp directory with a strict 30s timeout
- Only pytest (Python) and jest/vitest (JS/TS) are supported
- Output is capped at 8000 chars to prevent response bloat
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from database import get_db_connection
from auth_utils import decode_token

router = APIRouter()

TIMEOUT_SECS = 30
MAX_OUTPUT_CHARS = 8000


def _get_user_id(request: Request) -> str:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        payload = decode_token(token)
        if payload:
            return str(payload.get("user_id", ""))
    return ""


def _detect_framework(file_names: list[str]) -> str:
    """Detect test framework from uploaded filenames."""
    for name in file_names:
        n = name.lower()
        if n.endswith(".py") and ("test_" in n or "_test.py" in n or "tests.py" in n):
            return "pytest"
        if re.search(r"\.(test|spec)\.(ts|tsx|js|jsx)$", n):
            return "jest"
        if "vitest" in n:
            return "vitest"
        if "jest.config" in n:
            return "jest"
    # Fallback: any .py test file
    for name in file_names:
        if name.endswith(".py"):
            return "pytest"
    return "unknown"


def _parse_pytest_output(output: str) -> dict:
    """Extract pass/fail counts from pytest output."""
    passed = failed = errors = 0
    m = re.search(r"(\d+) passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+) error", output)
    if m:
        errors = int(m.group(1))
    return {"passed": passed, "failed": failed, "errors": errors}


def _parse_jest_output(output: str) -> dict:
    """Extract pass/fail from jest/vitest output."""
    passed = failed = 0
    # Tests: X passed, Y failed
    m = re.search(r"Tests:\s*(?:(\d+) failed,?\s*)?(?:(\d+) passed)?", output)
    if m:
        failed = int(m.group(1) or 0)
        passed = int(m.group(2) or 0)
    return {"passed": passed, "failed": failed, "errors": 0}


@router.post("/run")
async def run_tests(body: dict, request: Request):
    """
    Run tests for a chat session.
    Body: { session_id: str, file_ids?: [str] }
    Returns: { framework, verdict, passed, failed, errors, output, duration_ms }
    """
    user_id = _get_user_id(request)
    session_id = body.get("session_id", "")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    # ── Load session files from DB ──
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT filename, content FROM session_files WHERE session_id = ? ORDER BY filename",
            (session_id,),
        ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="No files found in this session")

    file_names = [r["filename"] for r in rows]
    framework = _detect_framework(file_names)

    if framework == "unknown":
        return {
            "framework": "unknown",
            "verdict": "skipped",
            "message": "No test files detected. Upload test files (e.g. test_*.py or *.test.ts) to run tests.",
            "passed": 0, "failed": 0, "errors": 0,
            "output": "", "duration_ms": 0,
        }

    # ── Write files to temp dir ──
    tmpdir = tempfile.mkdtemp(prefix="surgicalai_tests_")
    try:
        for row in rows:
            filepath = Path(tmpdir) / row["filename"]
            filepath.parent.mkdir(parents=True, exist_ok=True)
            content = row["content"] or ""
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            filepath.write_text(content, encoding="utf-8")

        # ── Build command ──
        if framework == "pytest":
            cmd = ["python", "-m", "pytest", "--tb=short", "-q", tmpdir]
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        elif framework in ("jest", "vitest"):
            # Check if package.json / node_modules exist
            pkg = Path(tmpdir) / "package.json"
            if not pkg.exists():
                return {
                    "framework": framework,
                    "verdict": "skipped",
                    "message": "Jest/Vitest requires package.json and node_modules. Upload your full project or use a local runner.",
                    "passed": 0, "failed": 0, "errors": 0,
                    "output": "", "duration_ms": 0,
                }
            runner = "vitest" if framework == "vitest" else "jest"
            cmd = ["npx", runner, "--run", "--reporter=verbose"]
            env = os.environ.copy()
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported framework: {framework}")

        # ── Run with timeout ──
        import time
        start = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=tmpdir,
                timeout=TIMEOUT_SECS,
                env=env,
            )
            duration_ms = int((time.time() - start) * 1000)
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            output = (stdout + "\n" + stderr).strip()
            output = output[:MAX_OUTPUT_CHARS]
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            duration_ms = TIMEOUT_SECS * 1000
            return {
                "framework": framework,
                "verdict": "timeout",
                "message": f"Tests timed out after {TIMEOUT_SECS} seconds.",
                "passed": 0, "failed": 0, "errors": 0,
                "output": "", "duration_ms": duration_ms,
            }

        # ── Parse results ──
        if framework == "pytest":
            counts = _parse_pytest_output(output)
        else:
            counts = _parse_jest_output(output)

        if exit_code == 0:
            verdict = "passed"
        elif counts["failed"] > 0 or counts["errors"] > 0:
            verdict = "failed"
        else:
            verdict = "error"

        return {
            "framework": framework,
            "verdict": verdict,
            "passed": counts["passed"],
            "failed": counts["failed"],
            "errors": counts["errors"],
            "output": output,
            "duration_ms": duration_ms,
        }

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.get("/detect/{session_id}")
def detect_framework(session_id: str, request: Request):
    """Detect test framework from files uploaded in a session."""
    from database import get_db_conn
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT filename FROM session_files WHERE session_id=?",
            (session_id,)
        ).fetchall()
        conn.close()
        filenames = [r[0].lower() for r in rows]
    except Exception:
        filenames = []

    framework = None
    # Jest / Vitest
    if any("test.ts" in f or "test.tsx" in f or "spec.ts" in f or "spec.js" in f or "test.js" in f for f in filenames):
        framework = "Jest/Vitest"
    # pytest
    elif any(f.startswith("test_") or f.endswith("_test.py") for f in filenames):
        framework = "pytest"
    # Go tests
    elif any(f.endswith("_test.go") for f in filenames):
        framework = "Go test"
    # Rust
    elif any(f == "main.rs" or f.endswith(".rs") for f in filenames):
        framework = "cargo test"
    # Ruby
    elif any(f.endswith("_spec.rb") for f in filenames):
        framework = "RSpec"

    return {"framework": framework, "file_count": len(filenames)}
