"""
Static linter validation: pyflakes (Python) + tsc (TypeScript/TSX).

Drop this file into backend/services/ alongside syntax_validator.py.

Design mirrors syntax_validator.py:
  - validate_linters(code, filename) → list of error dicts
  - count_linter_errors(code, filename) → int (for delta comparisons)
  - format_feedback_block(errors, tool) → str injected into Surgeon retry prompt

Error dict shape (identical to syntax_validator.py):
    {"line": int, "column": int, "message": str, "detail": str}

Supported file types:
    .py        → pyflakes  (pip install pyflakes>=3.2.0)
    .ts .tsx   → tsc --noEmit
    .js .jsx   → tsc --noEmit --allowJs --checkJs (best-effort)

Both tools degrade gracefully: missing deps return [] and log a warning.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def validate_linters(code: str, filename: str) -> List[Dict]:
    """
    Run the appropriate linter for *filename* against *code*.
    Returns a (possibly empty) list of error dicts.
    Returns [] for unsupported file types.
    """
    ext = _ext(filename)
    if ext == ".py":
        return _run_pyflakes(code, filename)
    if ext in (".ts", ".tsx"):
        return _run_tsc(code, filename)
    if ext in (".js", ".jsx"):
        return _run_tsc(code, filename, allow_js=True)
    return []


def count_linter_errors(code: str, filename: str) -> int:
    """Quick error count — used for before/after delta comparisons."""
    return len(validate_linters(code, filename))


def linter_tool_name(filename: str) -> str:
    """Return the human-readable tool name for a given filename."""
    return "pyflakes" if _ext(filename) == ".py" else "tsc"


def linter_available(filename: str) -> bool:
    """True if the linter for this file type can actually run.

    pyflakes ships as a backend dependency; tsc must be located on disk/PATH.
    PR #81: the pipeline uses this to report an honest "skipped" instead of a
    misleading "clean" when tsc is absent (a false "tsc clean" is what hid
    BIG10's broken ship)."""
    ext = _ext(filename)
    if ext == ".py":
        try:
            import pyflakes  # noqa: F401
            return True
        except Exception:
            return False
    if ext in (".ts", ".tsx", ".js", ".jsx"):
        # tsc can run via the remote Vercel service (Option #2) OR a local binary.
        # The remote path only counts as "available" when the feature is enabled
        # AND a service URL is configured — otherwise we honestly self-skip.
        if _tsc_service_enabled() and _tsc_service_url():
            return True
        return _find_tsc() is not None
    return False


def format_feedback_block(errors: List[Dict], tool: str) -> str:
    """
    Format linter errors into a prompt block for the Surgeon retry.

    Example output:
        COMPILE ERRORS TO FIX (tsc — 2 error(s)):
          - line 42, col 5: Type 'string' is not assignable to type 'number'
          - line 57, col 12: Cannot find name 'useRef'
        Your replacement MUST resolve ALL of these errors.
        Do NOT introduce any new compile errors.
    """
    if not errors:
        return ""
    lines = [f"COMPILE ERRORS TO FIX ({tool} — {len(errors)} error(s)):"]
    for err in errors:
        lines.append(f"  - line {err['line']}, col {err['column']}: {err['message']}")
    lines.append("Your replacement MUST resolve ALL of these errors.")
    lines.append("Do NOT introduce any new compile errors.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# pyflakes — Python static analysis
# ─────────────────────────────────────────────────────────────────────────────

def _run_pyflakes(code: str, filename: str) -> List[Dict]:
    """
    Run pyflakes programmatically (pure-Python, no subprocess).

    Catches: undefined names, missing imports, unused imports,
             redefined-while-unused, syntax errors.
    Does NOT enforce style — avoids noise from pre-existing code choices.
    """
    try:
        from pyflakes import api as pf_api
        from pyflakes import reporter as pf_reporter
    except ImportError:
        print("[LINTER_VALIDATOR] pyflakes not installed — skipping Python lint. "
              "Fix: pip install pyflakes>=3.2.0")
        return []

    errors: List[Dict] = []

    class _Collector(pf_reporter.Reporter):
        def unexpectedError(self, filename, msg):
            errors.append(_make_err(0, 0, f"pyflakes internal error: {msg}", str(filename)))

        def syntaxError(self, filename, msg, lineno, offset, text):
            col = offset or 0
            errors.append(_make_err(
                lineno or 0, col,
                f"SyntaxError: {msg}",
                f"line {lineno}: {(text or '').strip()}",
            ))

        def flake(self, message):
            line = getattr(message, "lineno", 0) or 0
            col  = getattr(message, "col", 0) or 0
            # str(message) → "filename:line:col: MessageText"
            clean = re.sub(r"^[^:]+:\d+:\d*\s*", "", str(message)).strip()
            errors.append(_make_err(line, col, clean, f"line {line}: {clean}"))

    collector = _Collector(warningStream=io.StringIO())
    try:
        pf_api.check(code, filename=filename, reporter=collector)
    except Exception as exc:
        print(f"[LINTER_VALIDATOR] pyflakes raised unexpectedly: {exc}")

    return errors[:8]


# ─────────────────────────────────────────────────────────────────────────────
# tsc — TypeScript type-checker
# ─────────────────────────────────────────────────────────────────────────────

_TSC_CONFIG_BASE: Dict = {
    "compilerOptions": {
        "target": "ES2020",
        "module": "ESNext",
        "moduleResolution": "bundler",
        "jsx": "react-jsx",
        "strict": True,
        "skipLibCheck": True,
        "noEmit": True,
        "isolatedModules": True,
        "allowImportingTsExtensions": True,
    },
    "include": ["*.ts", "*.tsx", "*.js", "*.jsx"],
}

_TSC_TIMEOUT_SECS = 25

# ── Option #2: tsc via Vercel serverless function ────────────────────────────
# Feature flag + endpoint + shared secret. ALL read from the environment so the
# gate can be toggled without a code change (the kill switch we lacked before).
#   TSC_ENABLED         "1"/"true" to route tsc to the remote service
#   TSC_SERVICE_URL     full https URL of the Vercel /api/tsc function
#   TSC_SERVICE_SECRET  shared secret; sent as the x-tsc-secret header
_TSC_SERVICE_TIMEOUT_SECS = 25


def _tsc_service_enabled() -> bool:
    """True if the remote-tsc feature flag is on (default OFF / fail-safe)."""
    return os.environ.get("TSC_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _tsc_service_url() -> Optional[str]:
    url = os.environ.get("TSC_SERVICE_URL", "").strip()
    return url or None


def _tsc_service_secret() -> str:
    return os.environ.get("TSC_SERVICE_SECRET", "").strip()


def _run_tsc_via_service(code: str, filename: str, allow_js: bool = False) -> List[Dict]:
    """
    POST {filename, content} to the Vercel tsc function and parse its JSON reply.

    Expected response shape (produced by frontend/api/tsc.ts):
        {"errors": [{line, column, message, detail}, ...], "tool": "tsc"}

    Degrades to a SAFE SKIP (returns []) on any error — unconfigured secret,
    network failure, non-200, malformed JSON, or timeout — so the remote path
    can never false-block an edit (same contract as the local binary path).
    """
    url = _tsc_service_url()
    if not url:
        return []

    payload = json.dumps({"filename": filename, "content": code}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    secret = _tsc_service_secret()
    if secret:
        req.add_header("x-tsc-secret", secret)

    try:
        with urllib.request.urlopen(req, timeout=_TSC_SERVICE_TIMEOUT_SECS) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        print(f"[LINTER_VALIDATOR] tsc service HTTP {exc.code} — skipping TypeScript lint")
        return []
    except Exception as exc:
        print(f"[LINTER_VALIDATOR] tsc service unreachable ({exc}) — skipping TypeScript lint")
        return []

    try:
        data = json.loads(raw)
    except Exception:
        print("[LINTER_VALIDATOR] tsc service returned malformed JSON — skipping")
        return []

    errors_in = data.get("errors") if isinstance(data, dict) else None
    if not isinstance(errors_in, list):
        return []

    errors: List[Dict] = []
    for e in errors_in:
        if not isinstance(e, dict):
            continue
        line = int(e.get("line", 0) or 0)
        col = int(e.get("column", 0) or 0)
        msg = str(e.get("message", "")).strip()
        detail = str(e.get("detail") or f"line {line}, col {col}: {msg}")
        errors.append(_make_err(line, col, msg, detail))
    return errors[:8]


def _run_tsc(code: str, filename: str, allow_js: bool = False) -> List[Dict]:
    """
    Write *code* to a temp dir with a tsconfig.json and run tsc --noEmit.
    Parses stdout and returns structured error dicts.
    Returns [] gracefully if tsc is not installed.

    Option #2 (Vercel sidecar): when TSC_ENABLED is set and TSC_SERVICE_URL is
    configured, type-checking is delegated to the remote Vercel function instead
    of a local binary. The remote path degrades to a safe skip (returns []) on
    any network/HTTP error, so it can never false-block an edit.
    """
    if _tsc_service_enabled() and _tsc_service_url():
        return _run_tsc_via_service(code, filename, allow_js=allow_js)

    tsc_bin = _find_tsc()
    if not tsc_bin:
        print("[LINTER_VALIDATOR] tsc not found — skipping TypeScript lint. "
              "Fix: cd frontend && npm install --save-dev typescript")
        return []

    cfg = dict(_TSC_CONFIG_BASE)
    if allow_js:
        cfg["compilerOptions"] = {**cfg["compilerOptions"], "allowJs": True, "checkJs": True}

    with tempfile.TemporaryDirectory(prefix="surgicalai_tsc_") as tmpdir:
        src_path = os.path.join(tmpdir, os.path.basename(filename))
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(code)

        tsconfig_path = os.path.join(tmpdir, "tsconfig.json")
        with open(tsconfig_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

        try:
            result = subprocess.run(
                [tsc_bin, "--noEmit", "--project", tsconfig_path],
                capture_output=True,
                text=True,
                timeout=_TSC_TIMEOUT_SECS,
                cwd=tmpdir,
            )
        except subprocess.TimeoutExpired:
            print("[LINTER_VALIDATOR] tsc timed out — skipping")
            return []
        except Exception as exc:
            print(f"[LINTER_VALIDATOR] tsc subprocess error: {exc}")
            return []

    return _parse_tsc_output(result.stdout + result.stderr, os.path.basename(filename))


def _parse_tsc_output(output: str, basename: str) -> List[Dict]:
    """
    Parse tsc diagnostic lines.
    Format: <file>(<line>,<col>): error TS<code>: <message>
    Only keeps errors for our source file (not lib.d.ts / node_modules noise).
    """
    errors: List[Dict] = []
    pattern = re.compile(
        r"^(?P<file>[^(]+)\((?P<line>\d+),(?P<col>\d+)\):\s+"
        r"(?P<severity>error|warning)\s+TS\d+:\s+(?P<msg>.+)$",
        re.MULTILINE,
    )
    for m in pattern.finditer(output):
        if m.group("severity") != "error":
            continue
        file_part = m.group("file")
        if ".d.ts" in file_part or "node_modules" in file_part:
            continue
        if basename and os.path.basename(file_part) != basename:
            continue
        line = int(m.group("line"))
        col  = int(m.group("col"))
        msg  = m.group("msg").strip()
        errors.append(_make_err(line, col, msg, f"line {line}, col {col}: {msg}"))

    return errors[:8]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ext(filename: str) -> str:
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""


def _make_err(line: int, col: int, message: str, detail: str) -> Dict:
    return {"line": line, "column": col, "message": message, "detail": detail}


def _find_tsc() -> Optional[str]:
    """
    Locate tsc: checks local node_modules first (version-pinned),
    then falls back to global PATH.

    ┌─ FUTURE WORK / "how to actually turn tsc on" (handoff, 2026-05-29) ──────┐
    │ tsc is currently NOT installed on the backend, so this returns None and  │
    │ the whole lint gate honestly self-skips. To enable it, the binary must   │
    │ land in ONE of the probe paths below — anywhere else and nothing changes.│
    │ Easiest: `npm install typescript` inside frontend/ (matches the first    │
    │ two candidates), or put `tsc` on PATH (shutil.which fallback).            │
    │                                                                          │
    │ The moment a binary is found here, linter_available() flips to True and  │
    │ the gate goes LIVE immediately for ALL edits — there is NO feature flag / │
    │ kill switch today. If you want a gradual rollout or an off-switch, add a  │
    │ flag check (env var) BEFORE shipping the install. See the matching note  │
    │ in pipeline.py at the "⏭ tsc skipped (not installed)" branch.            │
    │                                                                          │
    │ PERFORMANCE: _run_tsc() is invoked on BOTH the original and edited file  │
    │ (delta gate), i.e. tsc runs ~2x per edit in the request path. On a large │
    │ file (6k+ lines, --checkJs) that can add several seconds. It degrades to │
    │ a safe skip on timeout (returns []), so it can't break an edit, but it   │
    │ can make edits feel slower — watch latency after enabling.               │
    │                                                                          │
    │ SAFE because PR #82 made the lint gate DELTA-based: checking a file in    │
    │ isolation produces many "Cannot find module" errors (no node_modules in  │
    │ the temp dir), but those exist in BOTH original and edited versions and  │
    │ cancel out in the delta — so they do NOT false-block. Do not enable tsc  │
    │ if that delta gate is ever reverted.                                     │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    candidates = [
        # Preferred: inside the frontend package
        os.path.join(os.getcwd(), "frontend", "node_modules", ".bin", "tsc"),
        os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "node_modules", ".bin", "tsc"),
        # Project root node_modules
        os.path.join(os.getcwd(), "node_modules", ".bin", "tsc"),
        # Relative (when CWD is backend/)
        "../frontend/node_modules/.bin/tsc",
        "./node_modules/.bin/tsc",
    ]
    for c in candidates:
        resolved = os.path.abspath(c)
        if os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return resolved

    import shutil
    return shutil.which("tsc")
