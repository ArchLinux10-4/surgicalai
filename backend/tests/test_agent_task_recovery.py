"""Agent-task recovery: credit-pause halt + disconnect checkpoint (C1/H2).

Proves execute-task no longer treats Anthropic credit pauses as done/no_edits,
and captures v2 checkpoints for safety-net Apply cards on disconnect.

Evidence: Agent Mode audit — execute_task chunk filter dropped credit_paused
and checkpoint; queue reported success / lost mid-task edits.
"""
from __future__ import annotations

import inspect
import json
import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from routers import chat as chat_mod  # noqa: E402


def test_result_is_credit_paused_helper():
    assert chat_mod._result_is_credit_paused({"credit_paused": True}) is True
    assert chat_mod._result_is_credit_paused({"credit_paused": False}) is False
    assert chat_mod._result_is_credit_paused({}) is False
    assert chat_mod._result_is_credit_paused(None) is False
    assert chat_mod._result_is_credit_paused("x") is False


def test_execute_task_source_handles_credit_paused_and_checkpoint():
    """Static reachability: live execute_task must wire both fixes."""
    src = inspect.getsource(chat_mod.execute_task)
    # Credit-pause forward + halt (must not only parse smart_result)
    assert '"credit_paused"' in src
    assert "sse_exec_task_credit_paused" in src
    assert 'verdict="credit_paused"' in src or "verdict': 'credit_paused'" in src or 'verdict="credit_paused"' in src
    # Must not leave credit pauses on the done/no_edits success path unmarked
    assert "_result_is_credit_paused" in src
    # Checkpoint capture for disconnect safety net
    assert '"checkpoint"' in src
    assert "checkpoint_content" in src
    assert "_persist_structured_checkpoint_recovery" in src
    assert "interrupted_recovered" in src
    # Ordering: credit-pause halt before done/no_edits
    credit_idx = src.index("sse_exec_task_credit_paused")
    no_edits_idx = src.index("sse_exec_task_no_edits")
    assert credit_idx < no_edits_idx


def test_persist_structured_checkpoint_recovery_empty_inputs():
    assert chat_mod._persist_structured_checkpoint_recovery("s", "")["saved"] is False
    assert chat_mod._persist_structured_checkpoint_recovery(
        "s", "not-json"
    )["saved"] is False
    assert chat_mod._persist_structured_checkpoint_recovery(
        "s", json.dumps({"resolved": [{"filename": "a.py"}]})
    )["saved"] is False
    # Thin v2 without changes → not saved
    assert chat_mod._persist_structured_checkpoint_recovery(
        "s", json.dumps({"format_version": 2, "changes_by_file": {}})
    )["saved"] is False


def test_persist_structured_checkpoint_recovery_saves_envelope(tmp_path, monkeypatch):
    """When changes_by_file present, saves via _save_task_message envelope."""
    saved = {}

    def _fake_save(session_id, natural_text, parsed, model="", web_search_sources=None):
        saved["session_id"] = session_id
        saved["natural_text"] = natural_text
        saved["parsed"] = parsed
        saved["model"] = model

    monkeypatch.setattr(chat_mod, "_save_task_message", _fake_save)
    ckpt = {
        "format_version": 2,
        "changes_by_file": {
            "app.py": {
                "filename": "app.py",
                "file_id": "fid",
                "changes": [{"id": "c1", "diff": "@@\n-a\n+b\n", "new_code": "b", "original_code": "a"}],
            }
        },
    }
    out = chat_mod._persist_structured_checkpoint_recovery(
        "sess-1",
        json.dumps(ckpt),
        fallback_text="partial",
        model="claude-test",
    )
    assert out["saved"] is True
    assert out["recovered_changes"] == 1
    assert saved["session_id"] == "sess-1"
    assert saved["parsed"]["recovered"] is True
    assert "app.py" in saved["parsed"]["changes_by_file"]
    assert "interrupted" in saved["natural_text"].lower()
    assert saved["natural_text"].startswith("partial")


def test_task_runner_halts_on_credit_paused_smart_result():
    src = inspect.getsource(
        __import__("services.task_runner", fromlist=["_execute_one_task"])._execute_one_task
    )
    assert 'parsed.get("credit_paused")' in src
    assert "saw_credit_paused" in src
    assert "runner_task_credit_paused" in src
    # Must run before done/no_edits style completion (avoid matching _cp_has_edits)
    assert src.index("runner_task_credit_paused") < src.index('verdict = "no_edits"')
    # Partial edits before pause must keep the Apply envelope
    assert '_rp["credit_paused"] = True' in src


def test_execute_task_credit_pause_keeps_applyable_edits():
    """Credit-pause mid-task must not discard changes_by_file (plan parity)."""
    src = inspect.getsource(chat_mod.execute_task)
    # Proves we no longer always pass parsed=None on credit pause
    assert "_cp_has_edits" in src
    credit_block = src[
        src.index("_cp_has_edits") : src.index("sse_exec_task_credit_paused")
    ]
    assert '"smart_result"' in credit_block
    assert '_rp["credit_paused"] = True' in credit_block


def test_client_execute_task_forwards_credit_paused():
    """Frontend executeTask processLine must invoke onCreditPaused."""
    client_path = os.path.join(
        os.path.dirname(_BACKEND), "frontend", "src", "api", "client.ts"
    )
    with open(client_path, encoding="utf-8") as f:
        src = f.read()
    idx = src.index("executeTask:")
    block = src[idx : idx + 3500]
    assert "onCreditPaused" in block
    assert "credit_paused" in block
    # Parity with smart(): object/string normalize before callback
    assert "typeof chunk.content === 'object'" in block


def test_disconnect_guard_source_prefers_structured_recovery():
    """Orphan running→pending only when no changes_by_file checkpoint."""
    src = inspect.getsource(chat_mod.execute_task)
    assert "_persist_structured_checkpoint_recovery" in src
    assert "interrupted_recovered" in src
    assert "sse_exec_task_orphan_recovered" in src
    assert "sse_exec_task_orphan_reset" in src
    # Prefer recovery before bare pending reset
    assert src.index("execute_task_safety_net_structured_recovery") < src.index(
        "sse_exec_task_orphan_reset"
    )
