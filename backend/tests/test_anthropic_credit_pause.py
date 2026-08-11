"""Anthropic credit-pause: classify, persist, resume plumbing.

Evidence: surgical_debug_cb380321.jsonl final Retry with QA —
plan_execute_error with "credit balance is too low", then no_edits_produced
with misleading "Focused edit call produced no valid edit block".
"""

import asyncio
import json

import pytest

from services.anthropic_billing import (
    AnthropicCreditExhaustedError,
    is_anthropic_credit_error,
    raise_if_anthropic_credit_error,
    anthropic_credit_user_message,
    save_credit_pause,
    get_credit_pause,
    get_active_credit_pause,
    update_credit_pause_status,
    credit_pause_public_view,
)


class _FakeExc(Exception):
    def __init__(self, message, status_code=400, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def test_classify_cb380321_literal_credit_message():
    msg = (
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'Your credit balance is too low to access the Anthropic API. "
        "Please go to Plans & Billing to upgrade or purchase credits.'}, "
        "'request_id': 'req_011Cdv5ARdBo1H5iDKLwkvPQ'}"
    )
    assert is_anthropic_credit_error(_FakeExc(msg)) is True


def test_classify_does_not_false_positive_on_generic_400():
    assert is_anthropic_credit_error(
        _FakeExc("Error code: 400 - invalid_request_error: bad schema")
    ) is False


def test_raise_if_wraps_as_marker():
    with pytest.raises(AnthropicCreditExhaustedError):
        raise_if_anthropic_credit_error(
            _FakeExc("Your credit balance is too low to access the Anthropic API")
        )


def test_marker_isinstance_short_circuits():
    err = AnthropicCreditExhaustedError("already marked")
    assert is_anthropic_credit_error(err) is True
    with pytest.raises(AnthropicCreditExhaustedError) as ei:
        raise_if_anthropic_credit_error(err)
    assert ei.value is err


def test_user_message_mentions_resume_and_billing():
    msg = anthropic_credit_user_message()
    assert "credits" in msg.lower()
    assert "Resume" in msg
    assert "anthropic" in msg.lower()


def test_friendly_error_surfaces_credit_copy():
    from services.pipeline import _friendly_error
    msg = _friendly_error(
        AnthropicCreditExhaustedError(
            "Your credit balance is too low to access the Anthropic API"
        )
    )
    assert "credits" in msg.lower()
    assert "Resume" in msg


def test_save_and_load_credit_pause_roundtrip(tmp_path, monkeypatch):
    """Persist remaining plan + held Grok writes so resume can restore work."""
    import database as db

    # Point SQLite at a fresh temp DB and ensure schema includes credit_pauses.
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "credit_pause_test.db"))
    db.init_db()

    pause_id = save_credit_pause(
        session_id="sess-cb380321",
        user_id="user-1",
        user_request="Retry with QA for Smart Export",
        remaining_plan=[
            {"filename": "index.html", "symbol": "script_2", "description": "fix steps"},
            {"filename": "index.html", "symbol": "button#smartExportApplyBtn", "description": "label"},
        ],
        completed_edit_blocks=[],
        held_grok_writes=[
            json.dumps({
                "filename": "index.html",
                "symbol": "script_2",
                "new_code": "function _setSmartExportStep(n) {}",
            })
        ],
        file_content_snapshot={"index.html": "<html></html>"},
        error_message="credit balance is too low",
    )
    assert pause_id

    loaded = get_credit_pause(pause_id)
    assert loaded is not None
    assert loaded["status"] == "paused"
    assert loaded["session_id"] == "sess-cb380321"
    assert len(loaded["remaining_plan"]) == 2
    assert len(loaded["held_grok_writes"]) == 1
    assert loaded["file_content_snapshot"]["index.html"] == "<html></html>"

    active = get_active_credit_pause("sess-cb380321")
    assert active and active["id"] == pause_id

    view = credit_pause_public_view(loaded)
    assert view["pause_id"] == pause_id
    assert view["remaining_count"] == 2
    assert view["held_write_count"] == 1
    assert "file_content_snapshot" not in view  # never leak full files to client

    assert update_credit_pause_status(pause_id, "completed") is True
    assert get_active_credit_pause("sess-cb380321") is None


def test_execute_single_edit_rethrows_credit_error(monkeypatch):
    """Regression: credit errors must not be swallowed as None (cb380321)."""
    from services import pipeline as pl

    class _BoomStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @property
        def text_stream(self):
            async def _gen():
                raise _FakeExc(
                    "Error code: 400 - Your credit balance is too low to access "
                    "the Anthropic API. Please go to Plans & Billing."
                )
                yield "unreachable"  # pragma: no cover
            return _gen()

    class _BoomClient:
        class messages:
            @staticmethod
            def stream(**kwargs):
                return _BoomStream()

    async def _run():
        with pytest.raises(AnthropicCreditExhaustedError):
            await pl._execute_single_edit(
                _BoomClient(),
                "claude-sonnet-5",
                "index.html",
                "script_2",
                "fix step indicator",
                "function _setSmartExportStep(n) {}",
                None,
                "retry",
                "sess-test",
                "user-test",
                max_wait_s=5.0,
            )

    asyncio.run(_run())


def test_plan_gate_stashes_held_writes():
    from services.grok_plan_gate import GrokPlanGate

    class _TR:
        edit_json_strings = [
            '{"filename":"index.html","symbol":"script_2","new_code":"x"}'
        ]
        new_file_json_strings = []
        edit_plan = None
        results_by_id = {"call-1": "ok"}
        calls = [{"id": "call-1", "name": "write_surgical_edit"}]

    gate = GrokPlanGate(
        "1. fix step indicator\n2. route download\n3. update button\n4. help copy",
        is_agent_task=False,
        mode="edit",
    )
    assert gate.compound is True
    tr = _TR()
    held = gate.filter_translation(tr, turn=4)
    assert held is True
    assert tr.edit_json_strings == []
    assert len(gate.last_held_edit_json_strings) == 1
    assert "script_2" in gate.last_held_edit_json_strings[0]
