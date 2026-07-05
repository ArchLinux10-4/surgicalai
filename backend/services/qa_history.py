"""
QA history lookup — flag-gated via QA_HISTORY (default OFF).

Reads the qa_log table (which was previously write-only telemetry) to give
the QA agent memory: if a symbol was blocked or warned about in past runs,
QA gets a one-line heads-up in its prompt.

Advisory only — never gates, never raises. On any failure returns an empty
summary so the QA prompt is simply built without a history line.
"""


def get_symbol_history(filename, symbol_name, limit=20):
    """
    Look up past QA verdicts for (filename, symbol_name).

    Returns: {
        "summary": str,        # "" when no noteworthy history (or on error)
        "total": int,          # rows inspected
        "blocked": int,
        "warning": int,
        "last_verdict": str,   # most recent verdict, "" if none
        "error": str or None,
    }
    """
    empty = {"summary": "", "total": 0, "blocked": 0, "warning": 0,
             "last_verdict": "", "error": None}
    if not filename or not symbol_name:
        return empty

    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            # conn.execute() works for SQLite (native) AND PostgreSQL
            # (CompatConn.execute() auto-translates ? -> %s) — same pattern
            # as _log_qa_result, the proven writer for this table.
            cur = conn.execute(
                """SELECT verdict, qa_score FROM qa_log
                   WHERE filename = ? AND symbol_name = ?
                   ORDER BY id DESC LIMIT ?""",
                (filename, symbol_name, limit),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        empty["error"] = str(e)[:200]
        return empty

    if not rows:
        return empty

    blocked = 0
    warning = 0
    last_verdict = ""
    for i, row in enumerate(rows):
        # Rows may be tuples (SQLite) or dict-like (Postgres wrapper) — handle both.
        try:
            verdict = row["verdict"]
        except (TypeError, KeyError, IndexError):
            verdict = row[0] if row else ""
        verdict = (verdict or "").lower()
        if i == 0:
            last_verdict = verdict
        if verdict == "blocked":
            blocked += 1
        elif verdict == "warning":
            warning += 1

    result = {"summary": "", "total": len(rows), "blocked": blocked,
              "warning": warning, "last_verdict": last_verdict, "error": None}

    # Only inject a prompt line when there is something worth flagging —
    # a clean history adds tokens without adding signal.
    if blocked or warning:
        parts = []
        if blocked:
            parts.append(f"blocked {blocked}x")
        if warning:
            parts.append(f"warned {warning}x")
        result["summary"] = (
            f"QA HISTORY for this symbol (last {len(rows)} runs): "
            f"{', '.join(parts)} (most recent verdict: {last_verdict or 'unknown'}). "
            f"This symbol has a track record of issues — inspect extra carefully. "
            f"Judge THIS edit on its own merits; history is context, not a verdict."
        )
    return result
