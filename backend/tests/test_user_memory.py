"""Per-user memory: ownership, recall ranking, and secret clipping. No model calls."""
from __future__ import annotations

import os
import sys
import uuid

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services.user_memory import (  # noqa: E402
    clip_memory_text,
    load_user_memory_block,
    recall_lines,
)


@pytest.fixture()
def mem_db(monkeypatch, tmp_path):
    import database as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "user_memory.db"))
    db.init_db()
    return db


def test_memory_block_excludes_other_users_and_null_owner(mem_db):
    current = str(uuid.uuid4())
    with mem_db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id, session_summary) VALUES (?, ?, ?, ?)",
            (current, "Current", "A", "current session summary"),
        )
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id, session_summary) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "Auth work", "A", "added login"),
        )
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id, session_summary) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "Billing", "B", "invoices"),
        )
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id, session_summary) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "Legacy", None, "shared legacy note"),
        )
        conn.execute(
            "INSERT INTO user_memory (user_id, content, enabled) VALUES (?, ?, 1)",
            ("A", "- uses postgres"),
        )
        conn.commit()
    block = load_user_memory_block("A", current, "hello")
    assert "Auth work" in block
    assert "uses postgres" in block
    assert "Billing" not in block
    assert "Legacy" not in block
    assert "current session summary" not in block


def test_recall_prefers_overlapping_summary_over_newer_unrelated_session():
    rows = [
        {"title": "New", "session_summary": "shipped the footer"},
        {"title": "Old", "session_summary": "postgres pooling"},
    ]
    lines = recall_lines(rows, "postgres pooling")
    assert lines[0].startswith("Old")


def test_extract_clips_to_25_bullets_and_drops_secret_lines():
    bullets = [f"- fact {i}" for i in range(30)]
    bullets.insert(3, "- key sk-abc")
    text = clip_memory_text("\n".join(bullets))
    assert len(text.splitlines()) == 25
    assert "sk-abc" not in text
