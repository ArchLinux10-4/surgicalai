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
import queue as _queue
import time as _time
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


# One writer for pipeline._dlog, database._dlog, and emit(). Callers enqueue.
# A daemon thread batch-inserts. A full queue drops the new event.
_QUEUE_MAX = 8000
_BATCH_MAX = 100
_BATCH_WAIT_S = 0.25
_log_queue: _queue.Queue = _queue.Queue(maxsize=_QUEUE_MAX)


def _write_batch(records: list) -> None:
    """Insert one batch in a single transaction and append the same lines to /tmp."""
    if not records:
        return
    lines = []
    for record in records:
        try:
            lines.append(_json.dumps(record, default=str) + "\n")
        except Exception:
            lines.append("")
    _state.in_progress = True
    try:
        try:
            from database import get_db_ctx
            with get_db_ctx() as conn:
                for record, line in zip(records, lines):
                    if not line:
                        continue
                    conn.execute(
                        "INSERT INTO debug_events (id, event, session_id, user_id, data, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            str(_uuid.uuid4()),
                            str(record.get("event") or ""),
                            str(record.get("session_id") or ""),
                            str(record.get("user_id") or ""),
                            line.strip(),
                            str(record.get("ts") or ""),
                        ),
                    )
                if _rnd.random() < 0.02:
                    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=_DLOG_EXPIRE_DAYS)).isoformat()
                    conn.execute("DELETE FROM debug_events WHERE created_at < ?", (cutoff,))
                conn.commit()
        except Exception:
            pass
    finally:
        _state.in_progress = False
    for line in lines:
        if line:
            _write_tmp_line(line)


def _log_worker() -> None:
    while True:
        first = _log_queue.get()
        batch = [first]
        deadline = _time.monotonic() + _BATCH_WAIT_S
        while len(batch) < _BATCH_MAX:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(_log_queue.get(timeout=remaining))
            except _queue.Empty:
                break
        try:
            _write_batch(batch)
        except Exception:
            pass
        finally:
            for _ in batch:
                _log_queue.task_done()


_worker = _threading.Thread(target=_log_worker, name="debug-events", daemon=True)
_worker.start()


def flush(timeout: float = 5.0) -> None:
    """Wait until queued events have been written. Tests call this."""
    deadline = _time.monotonic() + max(0.1, float(timeout))
    while _time.monotonic() < deadline:
        if _log_queue.unfinished_tasks == 0:
            return
        _time.sleep(0.01)


def emit(event: str, **kwargs) -> None:
    """Queue one debug event. NEVER raises.

    If this thread is already writing a batch, skip the queue and append the
    /tmp line only. That is what stops database._dlog, fired by get_db_ctx
    during the batch write, from queueing another connection forever.
    """
    try:
        ts = _dt.datetime.utcnow().isoformat() + "Z"
        record = {"ts": ts, "event": event, "session_id": str(kwargs.get("session_id", "") or ""),
                  "user_id": str(kwargs.get("user_id", "") or ""), **kwargs}
        line = _json.dumps(record, default=str) + "\n"
        if _emitting():
            _write_tmp_line(line)
            return
        try:
            _log_queue.put_nowait(record)
        except _queue.Full:
            _write_tmp_line(line)
    except Exception:
        pass
