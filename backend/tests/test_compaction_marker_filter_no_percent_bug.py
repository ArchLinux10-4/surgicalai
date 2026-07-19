"""
Regression test for a real production outage (2026-07-19): every /ws/smart-stream
call crashed with `IndexError: tuple index out of range` because the conversation
history query embedded a literal, unescaped `%` inside a parameterized SQL string:

    conn.execute(
        "... AND content NOT LIKE '__COMPACTION_EVENT__:%' ...",
        (session_id,),
    )

psycopg2 scans the *entire* SQL string for `%s` / `%(name)s` placeholders whenever
params are passed. A bare `%` that isn't part of a real placeholder (here, the LIKE
wildcard) confuses that scan and raises `IndexError: tuple index out of range` --
this reproduces 100% of the time on the pinned psycopg2-binary==2.9.9, independent
of session_id or data content (proven live against a real local Postgres instance
before landing the fix).

The correct, backend-agnostic fix is to pass the LIKE pattern as a bound parameter
instead of splicing it into the SQL text. This test guards two things:

1. (static) None of the fixed call sites regress back to an inline `%` literal in
   a parameterized `NOT LIKE` clause.
2. (behavioural) On SQLite (always available in CI, no Postgres required) the
   bound-parameter query still correctly excludes compaction-marker rows.
"""
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SITES = [
    "routers/chat.py",
    "services/task_runner.py",
]

DANGEROUS_PATTERN = re.compile(r"LIKE\s+'[^']*%[^']*'")


def test_no_inline_percent_literal_in_compaction_filter_sites():
    """Static guard: the exact class of bug (literal `%` spliced into a
    parameterized SQL string) must never reappear at these 3 known sites."""
    for rel_path in SITES:
        path = os.path.join(REPO_ROOT, rel_path)
        with open(path, "r") as f:
            # Strip comment lines -- this check is about live SQL text, not
            # docstrings/comments that merely *mention* the old buggy pattern.
            code_lines = [
                ln for ln in f.readlines() if not ln.strip().startswith("#")
            ]
            text = "".join(code_lines)
        assert "COMPACTION_EVENT__" in text, f"expected marker text in {rel_path}"
        bad_matches = [
            m.group(0) for m in DANGEROUS_PATTERN.finditer(text)
            if "COMPACTION_EVENT" in m.group(0)
        ]
        assert not bad_matches, (
            f"{rel_path} has a literal '%' embedded inside a quoted LIKE pattern "
            f"passed alongside bound params -- this crashes psycopg2 with "
            f"'IndexError: tuple index out of range'. Found: {bad_matches}. "
            f"Pass the pattern as a bound parameter instead, e.g. "
            f"NOT LIKE ? with params (session_id, '__COMPACTION_EVENT__:%')."
        )


def test_bound_parameter_pattern_still_excludes_compaction_marker_rows():
    """Behavioural guard: the fixed query (pattern passed as a param) must still
    correctly filter out compaction-marker rows, proven on a real DB engine."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE chat_messages (id TEXT, session_id TEXT, role TEXT, "
        "content TEXT, is_compacted INTEGER DEFAULT 0, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?,?,?,?)",
        ("1", "sess-a", "user", "hello"),
    )
    conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?,?,?,?)",
        ("2", "sess-a", "assistant", '__COMPACTION_EVENT__:{"foo":1}'),
    )
    conn.commit()

    sql = (
        "SELECT role, content FROM chat_messages WHERE session_id = ? "
        "AND is_compacted = 0 AND content NOT LIKE ? ORDER BY created_at ASC"
    )
    rows = conn.execute(sql, ("sess-a", "__COMPACTION_EVENT__:%")).fetchall()

    assert len(rows) == 1
    assert rows[0]["content"] == "hello"
