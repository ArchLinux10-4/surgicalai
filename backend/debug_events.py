"""
Canonical debug-event emitter — the ONE place that writes to the persistent
`debug_events` table (the exact table the user downloads via
`GET /api/debug/pipeline-log[/download]`).

WHY THIS MODULE EXISTS
----------------------
Before this, there were two separate `_dlog` functions:
  • services/pipeline.py::_dlog  → writes to the debug_events table (persistent)
  • database.py::_dlog           → writes to the Python logger + /tmp file ONLY

Everything that imported `_dlog` from `database` (session_files.py, database.py's
own connection-lifecycle logging, debug.py) therefore NEVER reached the
exportable `debug_events` table and was lost on deploy (/tmp is wiped). This
module is the single canonical emitter so those call sites can become
exportable without duplicating the DB-write logic.

CONTRACT
--------
  • emit(event, **kwargs) NEVER raises.
  • Writes a row to debug_events with the SAME shape as pipeline.py::_dlog
    (id, event, session_id, user_id, data, created_at; data = full JSON record
    including `ts` and `event`) AND appends the same JSON line to a /tmp file.
  • Imports nothing heavy at module load. `database.get_db_ctx` is imported
    lazily inside emit() (NEVER services.pipeline).
  • Reentrancy guard: database._dlog forwards into emit(); emit() writes via
    get_db_ctx(), whose open/close fires database._dlog again. A threading.local
    in-progress flag prevents that inner _dlog from recursing into a second DB
    write — the inner call degrades to the /tmp file line only.
"""
import os as _os
import json as _json
import uuid as _uuid
import random as _rnd
import threading as _threading
import datetime as _dt

_DEBUG_EVENTS_DLOG_PATH = "/tmp/surgical_debug.jsonl"
_DLOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB cap — rotate by truncating oldest half
_DLOG_EXPIRE_DAYS = 7               # auto-expire DB entries older than this (matches pipeline.py)

# Per-thread reentrancy flag. When emit() is mid-DB-write, get_db_ctx()'s own
# lifecycle logging (database._dlog → emit) must NOT open a second connection.
_state = _threading.local()


def _emitting() -> bool:
    return getattr(_state, "in_progress", False)


def _write_tmp_line(line: str) -> None:
    """Append one JSON line to the /tmp fallback file. Best-effort, never raises."""
    try:
        if _os.path.exists(_DEBUG_EVENTS_DLOG_PATH) and _os.path.getsize(_DEBUG_EVENTS_DLOG_PATH) > _DLOG_MAX_BYTES:
            with open(_DEBUG_EVENTS_DLOG_PATH, "r") as _f:
                _lines = _f.readlines()
            with open(_DEBUG_EVENTS_DLOG_PATH, "w") as _f:
                _f.writelines(_lines[len(_lines) // 2:])
    except Exception:
        pass
    try:
        with open(_DEBUG_EVENTS_DLOG_PATH, "a") as _f:
            _f.write(line)
    except Exception:
        pass


def emit(event: str, **kwargs) -> None:
    """Write one debug event to the persistent debug_events table + /tmp file.

    NEVER raises. If already emitting on this thread (reentrancy), skip the DB
    write entirely and only append the /tmp line, then return.
    """
    try:
        ts = _dt.datetime.utcnow().isoformat() + "Z"
        record = {"ts": ts, "event": event, **kwargs}
        line = _json.dumps(record, default=str) + "\n"

        # ── Reentrancy guard ──────────────────────────────────────────────
        # If we're already inside an emit() on this thread, a DB-lifecycle
        # _dlog fired during our own get_db_ctx() open/close. Do NOT recurse
        # into another DB write; the /tmp line is enough to preserve it.
        if _emitting():
            _write_tmp_line(line)
            return

        _state.in_progress = True
        try:
            # ── Primary: persistent DB row (survives deploys/restarts) ──
            try:
                from database import get_db_ctx  # lazy — nothing heavy at import time
                with get_db_ctx() as conn:
                    session_id = str(kwargs.get("session_id", "") or "")
                    user_id = str(kwargs.get("user_id", "") or "")
                    data_json = _json.dumps(record, default=str)
                    conn.execute(
                        "INSERT INTO debug_events (id, event, session_id, user_id, data, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (str(_uuid.uuid4()), event, session_id, user_id, data_json, ts),
                    )
                    # Probabilistic cleanup: ~1 in 50 calls, expire old rows so
                    # the table can't grow unbounded (matches pipeline.py::_dlog).
                    if _rnd.random() < 0.02:
                        cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=_DLOG_EXPIRE_DAYS)).isoformat()
                        conn.execute("DELETE FROM debug_events WHERE created_at < ?", (cutoff,))
                    conn.commit()
            except Exception:
                pass  # DB write failed — /tmp fallback below still preserves it
        finally:
            _state.in_progress = False

        # ── Secondary: /tmp file (fast grep + backward compat) ──
        _write_tmp_line(line)
    except Exception:
        pass  # logging must NEVER crash a caller
