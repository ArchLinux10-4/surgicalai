"""The harness can drive one tool dispatch and finish the loop."""
import asyncio

from tests.loop_harness import _run, tool_turn, _text_events, _tokens


def test_claude_search_dispatches_once_then_answers(monkeypatch):
    client, events = asyncio.run(_run(
        monkeypatch,
        "what does the logout hook do?",
        [
            tool_turn(
                "I'll look that up. ",
                "<search_request>",
                '{"terms": ["logout"]}',
                "</search_request>",
            ),
            _text_events("The logout hook clears the session."),
        ],
    ))

    assert len(client.messages.calls) == 2
    assert any(e.get("type") == "tool_activity" for e in events)
    assert "clears the session" in _tokens(events)
    assert events[-1]["type"] == "done"
