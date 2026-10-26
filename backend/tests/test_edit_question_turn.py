"""Edit-mode questions are answered once. Edit requests still get a nudge.

Drives run_natural_pipeline_stream with a fake Claude stream so a question
cannot be pushed into a second "you must produce edits" turn, and cannot
end with the no-edits banner. An actual edit request still does both.
"""
import asyncio

from tests.loop_harness import _run, _text_events, _tokens, _user_text


def test_question_in_edit_mode_answers_once_without_a_banner(monkeypatch):
    answer = (
        "The hook re-renders because its dependency array is a new object "
        "on every render."
    )
    client, events = asyncio.run(_run(
        monkeypatch,
        "why does this hook re-render?",
        [_text_events(answer)],
    ))

    assert len(client.messages.calls) == 1
    sent = _user_text(client.messages.calls[0])
    assert "Answer the user's message directly" in sent
    assert "[AGENTIC EDIT MODE]" not in sent
    assert "write the edits now" not in sent
    assert "MUST produce edits" not in sent

    text = _tokens(events)
    assert "dependency array" in text
    assert "No code was changed" not in text
    assert "No code changes were produced" not in text
    assert "ran out of steps" not in text
    assert "⚠️" not in text


def test_edit_request_still_nudges_and_shows_the_notice(monkeypatch):
    client, events = asyncio.run(_run(
        monkeypatch,
        "add a logout button",
        [
            _text_events("I would add a logout button to the header."),
            _text_events("Still only describing the button, with no edit."),
        ],
    ))

    assert len(client.messages.calls) == 2
    first = _user_text(client.messages.calls[0])
    second = _user_text(client.messages.calls[1])
    assert "[AGENTIC EDIT MODE]" in first
    assert "write the edits now" in first
    assert "MUST produce edits" in second

    text = _tokens(events)
    assert "No code was changed" in text
    assert "start with a plan" in text
    assert "No code changes were produced" not in text
    assert "⚠️" not in text
    assert "Please try again" not in text


def test_agent_task_question_stays_on_the_strict_edit_loop(monkeypatch):
    client, events = asyncio.run(_run(
        monkeypatch,
        "why does this hook re-render?",
        [
            _text_events("It re-renders when the dependency array changes."),
            _text_events("Same explanation, still no edit."),
        ],
        is_agent_task=True,
    ))

    assert len(client.messages.calls) == 2
    first = _user_text(client.messages.calls[0])
    assert "[AGENTIC EDIT MODE]" in first
    assert "write the edits now" in first
    assert "MUST produce edits" in _user_text(client.messages.calls[1])
    assert "No code was changed" in _tokens(events)
