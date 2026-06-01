"""
DataLab sandboxed transform executor (DuckDB SQL only).

Security model (defense in depth):
  1. Engine lockdown: `enable_external_access=false` — DuckDB cannot read/write
     files, URLs, or attach databases. `max_memory` caps RAM. Extension
     install/load is blocked.
  2. Statement allow-list: exactly ONE statement, must begin with SELECT or WITH.
     A keyword deny-list rejects ATTACH/COPY/INSTALL/LOAD/PRAGMA/read_*/etc.
  3. Process isolation: the query runs in a separate process with an OS address-
     space rlimit and a hard wall-clock timeout; overruns are killed.

Arbitrary Python execution is intentionally NOT supported — SQL covers the
spreadsheet workload with far less risk.

Tables are built from the loaded workbook with every column typed as VARCHAR
(fidelity-first). Claude's SQL uses TRY_CAST for numeric/date math.
"""
from __future__ import annotations

import multiprocessing as mp
import re
from typing import Dict, List, Tuple

from .config import TRANSFORM_TIMEOUT_SEC, TRANSFORM_MAX_MEM_MB

# Table payload: (columns, rows)
TablePayload = Tuple[List[str], List[List[str]]]

_BANNED = re.compile(
    r"\b("
    r"attach|detach|copy|install|load|export|import|pragma|set\s|reset|"
    r"read_csv|read_parquet|read_json|read_csv_auto|read_text|read_blob|"
    r"glob|sniff_csv|parquet_scan|csv_scan|delta_scan|iceberg_scan|"
    r"system|getenv|shell|call|create_secret|httpfs"
    r")\b",
    re.IGNORECASE,
)

# Allow a leading line comment / whitespace, then SELECT or WITH.
_STARTS_OK = re.compile(r"^\s*(--[^\n]*\n\s*)*(with|select)\b", re.IGNORECASE)


class SandboxError(RuntimeError):
    pass


class SqlValidationError(SandboxError):
    pass


def validate_sql(sql: str) -> str:
    """Static validation. Returns the cleaned single statement or raises."""
    if not sql or not sql.strip():
        raise SqlValidationError("empty SQL")
    cleaned = sql.strip().rstrip(";").strip()
    # exactly one statement: no internal ';'
    if ";" in cleaned:
        raise SqlValidationError("only a single statement is allowed")
    if not _STARTS_OK.match(cleaned):
        raise SqlValidationError("transform must be a single SELECT/WITH query")
    if _BANNED.search(cleaned):
        raise SqlValidationError("query uses a forbidden operation")
    return cleaned


def _safe_ident(name: str, fallback: str) -> str:
    ident = re.sub(r"[^0-9a-zA-Z_]", "_", (name or "").strip())
    if not ident or not re.match(r"^[a-zA-Z_]", ident):
        ident = fallback
    return ident.lower()


def table_names_for(sheet_names: List[str]) -> Dict[str, str]:
    """Map sheet display names → safe SQL identifiers (dedup-safe)."""
    out: Dict[str, str] = {}
    used = set()
    for i, sn in enumerate(sheet_names):
        ident = _safe_ident(sn, f"sheet{i+1}")
        base = ident
        k = 2
        while ident in used:
            ident = f"{base}_{k}"
            k += 1
        used.add(ident)
        out[sn] = ident
    return out


def _child(conn_q, tables: Dict[str, TablePayload], sql: str, max_mem_mb: int):
    """Runs in a separate process. Builds tables, runs SQL, returns result."""
    try:
        try:
            import resource
            soft = max_mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (soft, soft))
        except Exception:
            pass  # rlimit best-effort; duckdb max_memory still applies

        import duckdb
        con = duckdb.connect(database=":memory:")
        con.execute("SET enable_external_access=false")
        con.execute(f"SET max_memory='{max_mem_mb}MB'")
        con.execute("SET threads=2")

        for ident, (cols, rows) in tables.items():
            safe_cols = [f'"{c}"' for c in cols]
            col_defs = ", ".join(f"{c} VARCHAR" for c in safe_cols)
            con.execute(f'CREATE TABLE "{ident}" ({col_defs})')
            if rows:
                placeholders = ", ".join(["?"] * len(cols))
                con.executemany(
                    f'INSERT INTO "{ident}" VALUES ({placeholders})',
                    [list(r[:len(cols)]) + [""] * (len(cols) - len(r)) for r in rows],
                )

        cur = con.execute(sql)
        out_cols = [d[0] for d in cur.description] if cur.description else []
        out_rows = [[("" if v is None else str(v)) for v in row]
                    for row in cur.fetchall()]
        con.close()
        conn_q.put(("ok", out_cols, out_rows))
    except MemoryError:
        conn_q.put(("err", "transform exceeded memory limit", []))
    except Exception as e:  # noqa: BLE001
        conn_q.put(("err", f"{type(e).__name__}: {e}", []))


def run_sql(
    tables: Dict[str, TablePayload],
    sql: str,
    timeout_sec: int = TRANSFORM_TIMEOUT_SEC,
    max_mem_mb: int = TRANSFORM_MAX_MEM_MB,
) -> Tuple[List[str], List[List[str]]]:
    """
    Validate + execute SQL against the given tables in an isolated process.
    Returns (columns, rows) or raises SandboxError.
    """
    cleaned = validate_sql(sql)
    # Prefer fork on Linux (Railway): no re-import of the entry module, no
    # __main__ guard needed, and still a real isolated process so the rlimit +
    # hard kill apply. Fall back to the default context elsewhere.
    try:
        ctx = mp.get_context("fork")
    except ValueError:
        ctx = mp.get_context()
    q = ctx.Queue()
    p = ctx.Process(target=_child, args=(q, tables, cleaned, max_mem_mb))
    p.start()
    p.join(timeout_sec)
    if p.is_alive():
        p.terminate()
        p.join(2)
        if p.is_alive():
            p.kill()
        raise SandboxError(f"transform timed out after {timeout_sec}s")
    try:
        status, a, b = q.get_nowait()
    except Exception:
        raise SandboxError("transform produced no result (worker crashed)")
    if status == "ok":
        return a, b
    raise SandboxError(a)
