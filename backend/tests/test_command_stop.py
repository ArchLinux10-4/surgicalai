"""A command result stops the loop only when the command is the whole task."""
import asyncio

from tests.loop_harness import (
    _run,
    _text_events,
    _user_text,
    stub_local,
    tool_turn,
)

_LOCAL = (
    "\naction: exec\nwhoami\n"
)


def _enable_local(monkeypatch):
    monkeypatch.setattr("services.local_exec.local_available", lambda user_id="": True)
    stub_local(monkeypatch)


def test_an_edit_is_not_told_to_stop_after_a_command(monkeypatch):
    _enable_local(monkeypatch)
    client, _events = asyncio.run(_run(
        monkeypatch,
        "update the header and make the code edits needed",
        [
            tool_turn("Checking the account. ", "<local_cli>", _LOCAL, "</local_cli>"),
            _text_events("I would update the header next."),
            _text_events("Still describing the header, with no edit."),
        ],
    ))
    assert len(client.messages.calls) == 3
    sent = _user_text(client.messages.calls[1])
    assert "Do not write code." not in sent
    assert "A command result is not the end of the task." in sent


def test_a_bare_command_is_told_to_stop(monkeypatch):
    _enable_local(monkeypatch)
    logs = []
    client, _events = asyncio.run(_run(
        monkeypatch,
        "whoami",
        [
            tool_turn("Running it. ", "<local_cli>", _LOCAL, "</local_cli>"),
            _text_events("You are pm."),
        ],
        logs=logs,
    ))
    assert len(client.messages.calls) == 2
    assert "Do not write code." in _user_text(client.messages.calls[1])
    assert any(name == "agent_loop_terminal_answer_exit" for name, _kw in logs)
