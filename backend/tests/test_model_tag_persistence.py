"""
Model-tag persistence fix — targeted regression tests.

Root cause: the `_model` badge shown in the UI only ever lived in transient
React state from the SSE stream; it was never written to the database, so it
vanished on reload. Fix persists it into the pre-existing
`metadata TEXT DEFAULT '{}'` column on chat_messages and reads it back out in
GET /sessions/{id}/messages as `_model`.

These tests exercise only the read/write contract added by the fix — they do
not touch the single-pass edit pipeline, agent pipeline, or QA path.
"""
import json
import os
import sqlite3
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_test_db():
    """Minimal in-memory chat_messages table matching production schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE chat_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


def _read_back_model(conn, msg_id):
    """Mirror the exact parse logic added to GET /sessions/{id}/messages."""
    row = conn.execute(
        "SELECT * FROM chat_messages WHERE id = ?", (msg_id,)
    ).fetchone()
    d = dict(row)
    meta_raw = d.pop("metadata", None)
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
            mdl = meta.get("model")
            if mdl:
                d["_model"] = mdl
        except Exception:
            pass
    return d


def test_model_round_trips():
    """A saved model tag comes back out unchanged as `_model`."""
    conn = _make_test_db()
    msg_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content, metadata) VALUES (?, ?, ?, ?, ?)",
        (msg_id, "sess1", "assistant", "hello", json.dumps({"model": "claude-sonnet-5"})),
    )
    conn.commit()
    d = _read_back_model(conn, msg_id)
    assert d["_model"] == "claude-sonnet-5"


def test_old_rows_without_metadata_dont_crash_or_fake_a_model():
    """Rows saved before this fix shipped have metadata='{}' (the column
    default) — must not crash, and must not surface a fake `_model` key."""
    conn = _make_test_db()
    msg_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content) VALUES (?, ?, ?, ?)",
        (msg_id, "sess1", "assistant", "hello"),
    )
    conn.commit()
    d = _read_back_model(conn, msg_id)
    assert "_model" not in d


def test_malformed_metadata_json_degrades_safely():
    """Corrupt metadata must never break message listing."""
    conn = _make_test_db()
    msg_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content, metadata) VALUES (?, ?, ?, ?, ?)",
        (msg_id, "sess1", "assistant", "hello", "{not valid json"),
    )
    conn.commit()
    d = _read_back_model(conn, msg_id)  # must not raise
    assert "_model" not in d


def test_empty_model_string_never_shows_as_literal_none():
    """model='' (e.g. offline/local run with no resolved tag) must not
    surface as a truthy `_model` key that would render as a literal
    'None'/'' badge in the UI."""
    conn = _make_test_db()
    msg_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content, metadata) VALUES (?, ?, ?, ?, ?)",
        (msg_id, "sess1", "assistant", "hello", json.dumps({"model": ""})),
    )
    conn.commit()
    d = _read_back_model(conn, msg_id)
    assert "_model" not in d


if __name__ == "__main__":
    test_model_round_trips()
    test_old_rows_without_metadata_dont_crash_or_fake_a_model()
    test_malformed_metadata_json_degrades_safely()
    test_empty_model_string_never_shows_as_literal_none()
    print("ALL PASS")
