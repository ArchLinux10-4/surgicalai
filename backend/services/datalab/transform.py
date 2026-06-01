"""
DataLab transform authoring + orchestration.

Flow (mirrors the code pipeline's "never ship below the gate" discipline):
  profile + request → Claude authors a single DuckDB SQL query →
  static SQL validation → sandboxed execution → data-QA gate →
  if it fails, the error is fed back to Claude and we retry (capped) →
  on success return the result; otherwise block with the failure trail.

The Claude call is injected (`caller`) so the loop is fully testable offline.
Architect model is a Claude model (claude-opus-4-6 default), per project rule.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import MAX_ROWS_PROFILE_SAMPLE
from .loader import LoadedWorkbook
from .profiler import build_profile
from .sandbox import run_sql, validate_sql, table_names_for, SandboxError, SqlValidationError
from .validate import validate_result, QAReport

ARCHITECT_FALLBACK = "claude-opus-4-6"
DEFAULT_MAX_ATTEMPTS = 3

# Caller signature: (system_prompt, user_message, user_id) -> raw text
Caller = Callable[[str, str, str], str]

SYSTEM = """You are a data transformation engine for spreadsheets/CSVs.
You are given a JSON profile of a workbook and a user's plain-language request.
Produce ONE DuckDB SQL query that performs the requested transformation.

HARD RULES:
- Output ONLY the SQL, inside a single ```sql code block. No prose.
- Exactly one statement. It MUST be a SELECT or a WITH...SELECT.
- Reference tables by the EXACT identifiers given in "tables".
- Every column is stored as VARCHAR. Use TRY_CAST(col AS DOUBLE/INTEGER/DATE)
  for any numeric or date logic. Never assume a column is already numeric.
- Preserve text fidelity: do NOT strip leading zeros or reformat codes/ids
  unless explicitly asked.
- Never use file, network, or system functions (no read_csv, COPY, ATTACH,
  INSTALL, LOAD, PRAGMA, etc.). Operate only on the provided tables.
- Return all columns the user would expect to see, with clear names.
"""


@dataclass
class TransformResult:
    ok: bool
    columns: Optional[List[str]] = None
    rows: Optional[List[List[str]]] = None
    sql: Optional[str] = None
    qa: Optional[QAReport] = None
    attempts: int = 0
    trail: List[str] = field(default_factory=list)
    error: str = ""


def extract_sql(raw: str) -> str:
    """Pull SQL out of a Claude reply (fenced block preferred)."""
    if not raw:
        return ""
    m = re.search(r"```(?:sql)?\s*(.+?)```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return raw.strip()


def _build_user_message(profile: dict, table_map: dict, user_request: str,
                        prior_error: Optional[str]) -> str:
    payload = {
        "tables": table_map,           # display name -> sql identifier
        "request": user_request,
        "profile": profile,
    }
    msg = (
        "Workbook profile and request below. Author the DuckDB SQL.\n\n"
        + json.dumps(payload, default=str)[:60000]
    )
    if prior_error:
        msg += (
            "\n\nYour previous query failed. Fix it and return corrected SQL.\n"
            f"FAILURE: {prior_error}"
        )
    return msg


def author_sql(caller: Caller, profile: dict, table_map: dict,
               user_request: str, prior_error: Optional[str], user_id: str) -> str:
    raw = caller(SYSTEM, _build_user_message(profile, table_map, user_request, prior_error), user_id)
    return extract_sql(raw)


def run_transform(
    wb: LoadedWorkbook,
    user_request: str,
    *,
    primary_sheet_index: int = 0,
    user_id: str = "",
    caller: Optional[Caller] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> TransformResult:
    """Author → execute → QA → retry loop. Never returns a failing result as ok."""
    caller = caller or _default_caller
    table_map = table_names_for([s.name for s in wb.sheets])
    tables = {table_map[s.name]: (s.columns, s.rows) for s in wb.sheets}
    profile = build_profile(wb, profile_filename(wb))

    trail: List[str] = []
    prior_error: Optional[str] = None
    last_sql: Optional[str] = None
    last_qa: Optional[QAReport] = None

    for attempt in range(1, max_attempts + 1):
        sql = author_sql(caller, profile, table_map, user_request, prior_error, user_id)
        last_sql = sql
        # Static validation first (cheap, blocks obviously bad SQL)
        try:
            validate_sql(sql)
        except SqlValidationError as e:
            prior_error = f"invalid SQL: {e}"
            trail.append(f"attempt {attempt}: {prior_error}")
            continue
        # Sandboxed execution
        try:
            cols, rows = run_sql(tables, sql)
        except SandboxError as e:
            prior_error = f"execution error: {e}"
            trail.append(f"attempt {attempt}: {prior_error}")
            continue
        # Data-QA gate
        qa = validate_result(wb, primary_sheet_index, cols, rows, user_request)
        last_qa = qa
        if qa.passed:
            trail.append(f"attempt {attempt}: QA pass (score {qa.score})")
            return TransformResult(ok=True, columns=cols, rows=rows, sql=sql,
                                   qa=qa, attempts=attempt, trail=trail)
        prior_error = f"data QA failed: {qa.summary}"
        trail.append(f"attempt {attempt}: {prior_error}")

    return TransformResult(
        ok=False, sql=last_sql, qa=last_qa, attempts=max_attempts,
        trail=trail,
        error="Transform did not pass the data-QA gate after "
              f"{max_attempts} attempts.",
    )


def profile_filename(wb: LoadedWorkbook) -> str:
    return wb.sheets[0].name if wb.sheets else "workbook"


# ---------------------------------------------------------------------------
# Real Claude caller (Anthropic SDK) — mirrors pipeline.py exactly.
# ---------------------------------------------------------------------------

def _resolve_anthropic_key(user_id: str) -> str:
    from database import get_setting, get_user_api_key
    if user_id:
        try:
            from crypto_utils import decrypt_api_key
            encrypted = get_user_api_key(user_id, "anthropic")
            if encrypted:
                return decrypt_api_key(encrypted)
        except Exception:
            pass
    return get_setting("anthropic_api_key", "")


def _default_caller(system: str, user_msg: str, user_id: str) -> str:
    from database import get_setting
    model = get_setting("architect_model", ARCHITECT_FALLBACK)
    # Data lane requires a Claude architect model; fall back if misconfigured.
    if not str(model).startswith("claude"):
        model = ARCHITECT_FALLBACK
    key = _resolve_anthropic_key(user_id)
    if not key:
        raise ValueError("Anthropic API key not configured. Settings → API Keys.")
    from anthropic import Anthropic
    client = Anthropic(api_key=key)
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return resp.content[0].text
