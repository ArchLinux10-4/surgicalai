"""Plan builds and QA-off turns must reach smart_result.

The diff-first block reads MAX_QA_RETRIES before the only assignment in
run_natural_pipeline_stream. Plan builds force qa_diff_first off, so every
plan build used to die there with UnboundLocalError after the edits had
already resolved, and the UI reported every step as not produced.

This runs the real pipeline. The model is the harness fake. QA's own model
call is stubbed so the flag combinations stay about the unbound name, not
about a second scripted turn.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services import pipeline  # noqa: E402
from services.plan_artifact import (  # noqa: E402
    forced_edit_plan_from_run,
    persist_plan_from_assistant_text,
)
from tests.loop_harness import _run, _text_events  # noqa: E402


A_SRC = 'def greet():\n    return "hi"\n'
A_NEW = 'def greet():\n    return "hello"\n'
B_SRC = 'def wave():\n    return "bye"\n'
B_NEW = 'def wave():\n    return "later"\n'

PLAN = """Update two functions.

```implementation_plan
{"steps": [
  {"filename": "a.py", "symbol": "greet", "description": "Update greet"},
  {"filename": "b.py", "symbol": "wave", "description": "Update wave"}
]}
```
"""


def _files():
    return [
        {"id": "f-a", "filename": "a.py", "content": A_SRC, "language": "python", "file_type": "code"},
        {"id": "f-b", "filename": "b.py", "content": B_SRC, "language": "python", "file_type": "code"},
    ]


def _edit(filename, symbol, new_code):
    raw = json.dumps({
        "filename": filename,
        "symbol": symbol,
        "new_code": new_code,
        "description": f"Update {symbol}",
    })
    return _text_events(f"<surgical_edit>{raw}</surgical_edit>")


@pytest.fixture()
def plan_db(monkeypatch, tmp_path):
    import database as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "qa_branch.db"))
    db.init_db()
    sid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (sid, "t", "user-a"),
        )
        conn.commit()
    return {"sid": sid}


def _safe_qa(**_kwargs):
    return {
        "verdict": "safe",
        "qa_score": 9,
        "summary": "ok",
        "import_issues": [],
        "downstream_risks": [],
        "type_errors": [],
        "plan_deviation": "",
        "risk_verdicts": [],
    }


async def _safe_qa_async(**kwargs):
    return _safe_qa(**kwargs)


@pytest.mark.parametrize("qa_diff_first", ["1", "0"])
@pytest.mark.parametrize("qa_enabled", ["1", "0"])
@pytest.mark.parametrize("kind", ["plan", "edit"])
def test_qa_flags_still_emit_smart_result(plan_db, monkeypatch, qa_diff_first, qa_enabled, kind):
    monkeypatch.setattr(pipeline, "run_qa_agent", _safe_qa_async)
    logs = []
    settings = {
        "qa_enabled": qa_enabled,
        "qa_diff_first": qa_diff_first,
        "qa_auto_fix_rounds": "0",
    }
    files = _files()
    if kind == "plan":
        ev = persist_plan_from_assistant_text(plan_db["sid"], PLAN)
        steps = forced_edit_plan_from_run(plan_db["sid"], ev["run_id"])
        assert len(steps) == 2
        _client, events = asyncio.run(_run(
            monkeypatch,
            "Implement the attached implementation_plan steps exactly.",
            [_edit("a.py", "greet", A_NEW), _edit("b.py", "wave", B_NEW)],
            logs=logs,
            session_files=files,
            session_id=plan_db["sid"],
            user_id="user-a",
            forced_edit_plan=steps,
            plan_run_id=ev["run_id"],
            settings=settings,
        ))
    else:
        _client, events = asyncio.run(_run(
            monkeypatch,
            "Change greet so it returns hello.",
            [_edit("a.py", "greet", A_NEW)],
            logs=logs,
            session_files=files[:1],
            settings=settings,
        ))

    types = [e.get("type") for e in events]
    assert "smart_result" in types, types
    names = [name for name, _kw in logs]
    assert "execute_task_exception" not in names, names
    assert "agent_loop_aborted" not in names, names
    result = next(e for e in events if e.get("type") == "smart_result")
    body = json.loads(result["content"])
    assert body.get("changes_by_file"), body
