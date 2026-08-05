"""
Tests for the v1.7 "research checkbox" — Claude web-search citations
surviving a reload in Edit/Agent mode, not just Ask/Plan.

Scope: `routers.chat._save_task_message` (used by both /execute-task and
services/task_runner.py) must persist `web_search_sources` into
chat_messages.metadata exactly like the pre-existing Ask/Plan branch does,
and `get_messages` must read it back out into `_sources` on the returned
message dict — so a page reload shows the same citations that streamed live.

Does NOT touch the already-working OpenAI/Sonnet-5 QA path or any Ask/Plan
code — this is additive coverage for the new Edit/Agent wiring only.
"""
import os
import sys
import asyncio
import tempfile
import importlib
import json as _json
from pathlib import Path

import pytest

_TMP_HOME = tempfile.mkdtemp(prefix="sai_test_home_ws_")
os.environ["HOME"] = _TMP_HOME
os.environ.pop("DATABASE_URL", None)  # force SQLite path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import database  # noqa: E402
importlib.reload(database)
database.init_db()

from routers import chat as chat_router  # noqa: E402


def _make_session():
    with database.get_db_ctx() as db:
        import uuid
        sid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO chat_sessions (id, title) VALUES (?, ?)", (sid, "t")
        )
        db.commit()
    return sid


def test_save_task_message_without_sources_unchanged():
    """Existing call sites that omit web_search_sources keep saving the
    exact same metadata shape as before this param existed (no regression)."""
    sid = _make_session()
    chat_router._save_task_message(sid, "hello world", None, model="claude-x")
    msgs = chat_router.get_messages(sid)
    saved = [m for m in msgs if m["role"] == "assistant"][-1]
    # get_messages should NOT attach _sources when none were saved
    assert "_sources" not in saved or not saved.get("_sources")
    assert saved["content"] == "hello world"


def test_save_task_message_persists_and_reloads_sources():
    """The new param: sources saved by a task must reload as `_sources`,
    matching the Ask/Plan branch's existing `_sources` field shape."""
    sid = _make_session()
    sources = [
        {"url": "https://example.com/a", "title": "A", "domain": "example.com", "page_age": None},
        {"url": "https://example.com/b", "title": "B", "domain": "example.com", "page_age": "2024"},
    ]
    chat_router._save_task_message(sid, "researched answer", None, model="claude-x",
                                    web_search_sources=sources)
    msgs = chat_router.get_messages(sid)
    saved = [m for m in msgs if m["role"] == "assistant"][-1]
    assert saved.get("_sources") == sources
    assert saved["content"] == "researched answer"


def test_save_task_message_empty_sources_list_is_noop():
    """An empty list (research ran but found nothing / wasn't enabled) must
    not add a `web_search_sources` key at all — mirrors the falsy-check
    used at every call site (execute-task, task_runner, single-pass)."""
    sid = _make_session()
    chat_router._save_task_message(sid, "no research needed", None, model="claude-x",
                                    web_search_sources=[])
    msgs = chat_router.get_messages(sid)
    saved = [m for m in msgs if m["role"] == "assistant"][-1]
    assert not saved.get("_sources")
