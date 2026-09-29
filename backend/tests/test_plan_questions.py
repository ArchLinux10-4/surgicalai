"""Plan-mode clarifying questions: parser, gate, prompt block, smart_stream flow."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services.plan_questions import (  # noqa: E402
    MAX_OPTIONS,
    MAX_QUESTIONS,
    parse_plan_questions,
    plan_questions_block,
    questions_allowed,
    strip_plan_questions_fence,
)


def _fence(payload) -> str:
    return "One quick thing.\n\n```plan_questions\n" + json.dumps(payload) + "\n```"


# ── parser ────────────────────────────────────────────────────────────────────

def test_parse_valid_object_form():
    text = _fence({"questions": [{
        "question": "Extend /items or add /search?",
        "options": [
            {"label": "Extend /items", "description": "routes/items.py", "recommended": True},
            {"label": "New /search"},
        ],
    }]})
    qs = parse_plan_questions(text)
    assert qs == [{
        "id": "q1",
        "question": "Extend /items or add /search?",
        "options": [
            {"label": "Extend /items", "description": "routes/items.py", "recommended": True},
            {"label": "New /search", "description": "", "recommended": False},
        ],
    }]


def test_parse_bare_array_form():
    qs = parse_plan_questions(_fence([{"question": "A?", "options": [{"label": "x"}]}]))
    assert qs and qs[0]["question"] == "A?"


def test_parse_caps_questions_and_options_and_reassigns_ids():
    payload = {"questions": [
        {"id": "zzz", "question": f"Q{i}?",
         "options": [{"label": f"o{j}"} for j in range(5)]}
        for i in range(6)
    ]}
    qs = parse_plan_questions(_fence(payload))
    assert len(qs) == MAX_QUESTIONS == 3
    assert [q["id"] for q in qs] == ["q1", "q2", "q3"]
    assert all(len(q["options"]) == MAX_OPTIONS == 2 for q in qs)


def test_parse_only_first_recommended_kept():
    qs = parse_plan_questions(_fence({"questions": [{
        "question": "Pick?",
        "options": [
            {"label": "a", "recommended": True},
            {"label": "b", "recommended": True},
        ],
    }]}))
    assert [o["recommended"] for o in qs[0]["options"]] == [True, False]


def test_parse_truncates_long_strings_and_collapses_whitespace():
    qs = parse_plan_questions(_fence({"questions": [{
        "question": "x " * 400,
        "options": [{"label": "L" * 300, "description": "d" * 500}],
    }]}))
    q = qs[0]
    assert len(q["question"]) <= 200
    assert len(q["options"][0]["label"]) <= 60
    assert len(q["options"][0]["description"]) <= 140


def test_parse_drops_empty_duplicate_and_non_dict_items():
    qs = parse_plan_questions(_fence({"questions": [
        {"question": "  "},
        "not a dict",
        {"question": "Same?"},
        {"question": "same?"},
        {"question": "Other?", "options": [{"label": ""}, {"label": "k"}, {"label": "K"}]},
    ]}))
    assert [q["question"] for q in qs] == ["Same?", "Other?"]
    assert [o["label"] for o in qs[1]["options"]] == ["k"]


def test_parse_recommended_must_be_literal_true():
    qs = parse_plan_questions(_fence({"questions": [{
        "question": "Q?", "options": [{"label": "a", "recommended": "yes"}],
    }]}))
    assert qs[0]["options"][0]["recommended"] is False


def test_parse_question_without_options_is_open_text():
    qs = parse_plan_questions(_fence({"questions": [{"question": "Which DB?"}]}))
    assert qs[0]["options"] == []


@pytest.mark.parametrize("text", [
    "", "no fence at all", "```plan_questions\n```",
    "```plan_questions\nnot json\n```",
    "```plan_questions\n{\"questions\": []}\n```",
    "```plan_questions\n{\"questions\": \"nope\"}\n```",
    "```plan_questions\n42\n```",
    "```plan_questions\n{\"questions\": [{\"question\": \"\"}]}\n```",
])
def test_parse_unusable_returns_none(text):
    assert parse_plan_questions(text) is None


def test_strip_fence():
    text = _fence({"questions": [{"question": "Q?"}]})
    assert strip_plan_questions_fence(text) == "One quick thing."


# ── prompt block ──────────────────────────────────────────────────────────────

def test_block_allowed_has_gate_caps_and_exclusivity():
    b = plan_questions_block(True)
    assert "Default: do NOT ask" in b
    assert "ALL of these are true" in b
    assert "at most 3 questions" in b
    assert "1 or 2" in b
    assert "```plan_questions" in b
    assert "Never both" in b


def test_block_closed_forbids_asking():
    b = plan_questions_block(False)
    assert "CLOSED" in b
    assert "Do not ask any more questions" in b
    assert "```plan_questions" not in b


def test_ask_directive_untouched():
    from routers import chat as chat_router
    ask = chat_router._mode_directive("ask")
    assert "plan_questions" not in ask
    assert "implementation_plan" not in ask


# ── gate (DB) ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def qdb(monkeypatch, tmp_path):
    import database as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "plan_questions.db"))
    db.init_db()
    sid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (sid, "t", "user-a"),
        )
        conn.commit()
    return {"db": db, "sid": sid}


def _add_msg(db, sid, role, content, metadata=None, ts="2026-01-01 00:00:00"):
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, metadata, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), sid, role, content,
             json.dumps(metadata) if metadata is not None else None, ts),
        )
        conn.commit()


def test_allowed_on_fresh_session(qdb):
    assert questions_allowed(qdb["sid"]) is True


def test_closed_when_latest_assistant_was_question_turn(qdb):
    db, sid = qdb["db"], qdb["sid"]
    _add_msg(db, sid, "assistant", "plan", {"mode": "plan"}, "2026-01-01 00:00:01")
    _add_msg(db, sid, "assistant", "q", {"mode": "plan", "plan_questions": [{"id": "q1"}]},
             "2026-01-01 00:00:02")
    _add_msg(db, sid, "user", "answers", None, "2026-01-01 00:00:03")
    assert questions_allowed(sid) is False


def test_reopens_after_answer_turn_was_answered_with_a_plan(qdb):
    db, sid = qdb["db"], qdb["sid"]
    _add_msg(db, sid, "assistant", "q", {"plan_questions": [{"id": "q1"}]}, "2026-01-01 00:00:01")
    _add_msg(db, sid, "assistant", "plan", {"mode": "plan"}, "2026-01-01 00:00:02")
    assert questions_allowed(sid) is True


def test_malformed_metadata_treated_as_allowed(qdb):
    db, sid = qdb["db"], qdb["sid"]
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, metadata) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), sid, "assistant", "x", "{not json"),
        )
        conn.commit()
    assert questions_allowed(sid) is True


def test_closed_while_plan_implementing(qdb, monkeypatch):
    monkeypatch.setattr("services.plan_artifact.latest_plan_run",
                        lambda sid: {"phase": "implementing", "tasks": [], "run_id": "r"})
    assert questions_allowed(qdb["sid"]) is False


def test_fails_closed_on_db_error(monkeypatch):
    import database as db

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_db_ctx", _boom)
    assert questions_allowed("whatever") is False


# ── smart_stream integration ──────────────────────────────────────────────────

PLAN_TEXT = """## Overview
Add a banner.

```implementation_plan
{"steps": [{"filename": "app.py", "symbol": "main", "description": "Add banner"}]}
```
"""

QUESTION = {"questions": [{
    "question": "Extend /items or add /search?",
    "options": [{"label": "Extend /items", "recommended": True}, {"label": "New /search"}],
}]}


@pytest.fixture()
def stream_env(monkeypatch, tmp_path):
    import database as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "pq_stream.db"))
    db.init_db()

    from routers import chat as chat_router

    calls = {"chat_stream": 0, "pipeline": 0, "messages": []}
    replies = {"texts": [PLAN_TEXT]}

    async def _fake_chat_stream(messages, **kwargs):
        calls["messages"].append(messages)
        text = replies["texts"][min(calls["chat_stream"], len(replies["texts"]) - 1)]
        calls["chat_stream"] += 1
        yield 'data: {"type": "token", "content": ' + json.dumps(text) + "}\n\n"
        yield 'data: {"type": "done", "content": "", "model": "grok-4.6"}\n\n'

    async def _fake_pipeline(*a, **k):
        calls["pipeline"] += 1
        yield 'data: {"type": "done", "content": ""}\n\n'

    monkeypatch.setattr(chat_router, "_resolve_chat_key", lambda uid, provider: "k")
    monkeypatch.setattr(chat_router, "run_chat_stream", _fake_chat_stream)
    monkeypatch.setattr("services.pipeline.run_natural_pipeline_stream", _fake_pipeline)
    monkeypatch.setattr("services.pipeline.run_smart_pipeline_stream", _fake_pipeline)
    monkeypatch.setattr("database.get_setting",
                        lambda k, d=None: "grok-4.6" if k == "architect_model" else (d if d is not None else ""))

    class _Req:
        def __init__(self):
            self.state = type("S", (), {"user_id": "user-a"})()

    sid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute("INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
                     (sid, "t", "user-a"))
        conn.commit()
    return {"chat": chat_router, "sid": sid, "req": _Req(), "calls": calls,
            "replies": replies, "db": db}


def _run_turn(env, message="plan search", mode="plan"):
    chat_router = env["chat"]

    async def _drain(resp):
        parts = []
        async for p in resp.body_iterator:
            parts.append(p.decode() if isinstance(p, (bytes, bytearray)) else p)
        return "".join(parts)

    resp = asyncio.run(chat_router.smart_stream(
        {"session_id": env["sid"], "message": message, "mode": mode}, env["req"]))
    return asyncio.run(_drain(resp))


def _events(body: str) -> list[dict]:
    out = []
    for line in body.split("\n\n"):
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except Exception:
                pass
    return out


def _assistant_rows(env):
    with env["db"].get_db_ctx() as conn:
        return conn.execute(
            "SELECT content, metadata FROM chat_messages "
            "WHERE session_id = ? AND role = 'assistant' ORDER BY created_at ASC",
            (env["sid"],),
        ).fetchall()


def test_question_turn_emits_event_before_done_no_retry(stream_env):
    stream_env["replies"]["texts"] = [_fence(QUESTION)]
    body = _run_turn(stream_env)
    evs = _events(body)
    types = [e["type"] for e in evs]
    assert "plan_questions" in types
    assert types.index("plan_questions") < types.index("done")
    assert "plan_ready" not in types and "plan_missing" not in types
    assert stream_env["calls"]["chat_stream"] == 1
    q_ev = next(e for e in evs if e["type"] == "plan_questions")
    assert q_ev["questions"][0]["id"] == "q1"

    rows = _assistant_rows(stream_env)
    assert len(rows) == 1
    meta = json.loads(rows[0]["metadata"])
    assert meta["mode"] == "plan"
    assert meta["plan_questions"][0]["question"].startswith("Extend")
    assert "plan_run_id" not in meta


def test_get_messages_surfaces_plan_questions(stream_env):
    stream_env["replies"]["texts"] = [_fence(QUESTION)]
    _run_turn(stream_env)
    from routers import chat as chat_router

    class _R:
        state = type("S", (), {"user_id": "user-a"})()

    msgs = chat_router.get_messages(stream_env["sid"], _R())
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert asst and asst[0]["_plan_questions"][0]["id"] == "q1"


def test_directive_includes_allowed_block_on_first_turn(stream_env):
    _run_turn(stream_env)
    sent = stream_env["calls"]["messages"][0][-1]["content"]
    assert "[PLAN QUESTIONS]" in sent
    assert "CLOSED" not in sent


def test_answer_turn_is_closed_and_ignores_question_fence(stream_env):
    stream_env["replies"]["texts"] = [_fence(QUESTION), _fence(QUESTION), PLAN_TEXT]
    _run_turn(stream_env)                       # question turn
    calls = stream_env["calls"]
    calls["messages"].clear()
    body = _run_turn(stream_env, message="Answers to your clarifying questions:\n1. x -> y")
    types = [e["type"] for e in _events(body)]
    assert "plan_questions" not in types        # fence ignored, not forwarded as questions
    assert "plan_ready" in types                # retry recovered a real plan
    assert calls["chat_stream"] == 3            # 1 question turn + answer turn + retry
    first_answer_call = calls["messages"][0][-1]["content"]
    assert "[PLAN QUESTIONS — CLOSED]" in first_answer_call
    assert "[PLAN QUESTIONS]" not in first_answer_call


def test_plan_fence_beats_question_fence(stream_env):
    stream_env["replies"]["texts"] = [_fence(QUESTION) + "\n\n" + PLAN_TEXT]
    body = _run_turn(stream_env)
    types = [e["type"] for e in _events(body)]
    assert "plan_ready" in types
    assert "plan_questions" not in types
    assert stream_env["calls"]["chat_stream"] == 1
    meta = json.loads(_assistant_rows(stream_env)[0]["metadata"])
    assert "plan_questions" not in meta


def test_invalid_question_fence_falls_back_to_retry(stream_env):
    stream_env["replies"]["texts"] = ["hm\n```plan_questions\nnot json\n```", PLAN_TEXT]
    body = _run_turn(stream_env)
    types = [e["type"] for e in _events(body)]
    assert "plan_questions" not in types
    assert "plan_ready" in types
    assert stream_env["calls"]["chat_stream"] == 2


def test_fenceless_reply_still_retries_then_plan_missing(stream_env):
    stream_env["replies"]["texts"] = ["just prose"]
    body = _run_turn(stream_env)
    types = [e["type"] for e in _events(body)]
    assert "plan_missing" in types and "plan_questions" not in types
    assert stream_env["calls"]["chat_stream"] == 2


def test_implementing_phase_never_asks(stream_env, monkeypatch):
    from services.plan_artifact import persist_plan_from_assistant_text, mark_plan_implementing
    ev = persist_plan_from_assistant_text(stream_env["sid"], PLAN_TEXT)
    mark_plan_implementing(stream_env["sid"], ev["run_id"])
    stream_env["replies"]["texts"] = [_fence(QUESTION)]
    body = _run_turn(stream_env)
    types = [e["type"] for e in _events(body)]
    assert "plan_locked" in types
    assert "plan_questions" not in types
    sent = stream_env["calls"]["messages"][0][-1]["content"]
    assert "CLOSED" in sent


def test_ask_mode_never_gets_questions_block(stream_env):
    stream_env["replies"]["texts"] = [_fence(QUESTION)]
    body = _run_turn(stream_env, mode="ask")
    assert "plan_questions" not in [e["type"] for e in _events(body)]
    sent = stream_env["calls"]["messages"][0][-1]["content"]
    assert "PLAN QUESTIONS" not in sent


def test_questions_with_existing_ready_plan_keep_plan_untouched(stream_env):
    from services.plan_artifact import latest_plan_run
    _run_turn(stream_env)                       # creates a ready plan
    before = latest_plan_run(stream_env["sid"])["run_id"]
    stream_env["replies"]["texts"] = [_fence(QUESTION)]
    stream_env["calls"]["chat_stream"] = 0
    body = _run_turn(stream_env, message="change the approach")
    types = [e["type"] for e in _events(body)]
    assert "plan_questions" in types and "plan_unchanged" not in types
    assert latest_plan_run(stream_env["sid"])["run_id"] == before
