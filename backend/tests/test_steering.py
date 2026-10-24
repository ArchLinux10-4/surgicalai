"""Steering inbox: routing, drain caps, and tool-boundary injection."""
from __future__ import annotations

import asyncio
import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services.steering import (  # noqa: E402
    STEER_MAX_CHARS,
    STEER_MAX_PER_DRAIN,
    apply_steer_at_boundary,
    attach_steer_to_tool_results,
    drain_steer,
    flush_unapplied,
    format_steer_block,
    route_ws_message,
    take_steer,
)


def _queue(*msgs):
    q = asyncio.Queue()
    for msg in msgs:
        q.put_nowait(msg)
    return q


def _steer(text, id_="s1"):
    return {"type": "steer", "id": id_, "text": text}


def test_receiver_routes_steer_away_from_the_approval_inbox():
    client = asyncio.Queue()
    steer = asyncio.Queue()
    frames = [
        _steer("use the other file", "s1"),
        {"type": "mcp_call_response", "action": "approve"},
        {"type": "file_response", "action": "skip"},
        {"type": "nope"},
        "not-a-dict",
    ]
    for frame in frames:
        route = route_ws_message(frame)
        if route == "steer":
            steer.put_nowait(frame)
        elif route == "client":
            client.put_nowait(frame)

    # Same drain the MCP / AWS waiters do before they block.
    drained = []
    while True:
        try:
            drained.append(client.get_nowait())
        except Exception:
            break
    assert [m["type"] for m in drained] == ["mcp_call_response", "file_response"]
    assert steer.get_nowait()["text"] == "use the other file"
    assert route_ws_message({"type": "aws_cli_response"}) == "client"
    assert route_ws_message({"type": "preview_check_response"}) == "client"
    assert route_ws_message(None) is None


def test_drain_caps_skips_empty_and_formats():
    long = "x" * (STEER_MAX_CHARS + 50)
    q = _queue(
        _steer("  ", "blank"),
        _steer("first", "a"),
        {"type": "other"},
        _steer(long, "b"),
        *[_steer(f"extra-{i}", f"e{i}") for i in range(STEER_MAX_PER_DRAIN)],
    )
    items = drain_steer(q)
    assert len(items) == STEER_MAX_PER_DRAIN
    assert items[0] == {"id": "a", "text": "first"}
    assert len(items[1]["text"]) == STEER_MAX_CHARS
    assert items[1]["text"] == "x" * STEER_MAX_CHARS
    # The sixth real steer is still waiting.
    rest = drain_steer(q, limit=None)
    assert rest
    assert rest[0]["text"].startswith("extra-")
    block = format_steer_block(items[:1])
    assert block.startswith(
        "The user sent a follow-up while you were working. "
        "Treat it as updated instructions and adapt:"
    )
    assert "first" in block


def test_no_inbox_is_a_no_op():
    messages = [{"role": "user", "content": "hello"}]
    assert drain_steer(None) == []
    assert flush_unapplied(None) == []
    assert apply_steer_at_boundary(messages, None, "sess") == (messages, [])
    assert take_steer(None, "sess") == ("", [])


def test_each_boundary_injects_once_and_openai_string_grows(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "services.steering.persist_applied_steer",
        lambda sid, items: saved.append((sid, items)),
    )
    q = _queue(_steer("check the tests", "s1"), _steer("and the types", "s2"))
    messages = [
        {"role": "assistant", "content": "looking"},
        {"role": "user", "content": "tool output"},
    ]
    updated, events = apply_steer_at_boundary(messages, q, "sess-1")
    assert [e["type"] for e in events] == ["steer_applied", "steer_applied"]
    assert events[0]["id"] == "s1"
    assert "check the tests" in updated[-1]["content"]
    assert "and the types" in updated[-1]["content"]
    assert updated[-1]["content"].startswith("tool output")
    assert saved and saved[0][0] == "sess-1"
    again, more = apply_steer_at_boundary(updated, q, "sess-1")
    assert more == []
    assert again == updated


def test_anthropic_tool_results_keep_one_user_turn(monkeypatch):
    monkeypatch.setattr("services.steering.persist_applied_steer", lambda *a, **k: None)
    q = _queue(_steer("rename it", "s9"))
    results = [{"type": "tool_result", "tool_use_id": "tu_1", "content": "found it"}]
    attached, events = attach_steer_to_tool_results(results, q, "sess")
    assert events[0]["type"] == "steer_applied"
    assert attached[0]["type"] == "tool_result"
    assert attached[1]["type"] == "text"
    assert "rename it" in attached[1]["text"]
    # Same shape when the user turn is already in the message list.
    q2 = _queue(_steer("rename it", "s9"))
    messages = [{"role": "user", "content": results}]
    folded, evs = apply_steer_at_boundary(messages, q2, "sess")
    assert len(folded) == 1
    assert folded[0]["role"] == "user"
    assert folded[0]["content"][0]["type"] == "tool_result"
    assert folded[0]["content"][1]["type"] == "text"
    assert evs[0]["type"] == "steer_applied"


def test_openai_tool_role_gets_a_new_user_message(monkeypatch):
    monkeypatch.setattr("services.steering.persist_applied_steer", lambda *a, **k: None)
    q = _queue(_steer("stop", "s3"))
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    folded, events = apply_steer_at_boundary(messages, q, "sess")
    assert events[0]["id"] == "s3"
    assert folded[-2]["role"] == "tool"
    assert folded[-1]["role"] == "user"
    assert "stop" in folded[-1]["content"]


def test_leftovers_are_unapplied_and_not_persisted(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "services.steering.persist_applied_steer",
        lambda *a, **k: saved.append(1),
    )
    q = _queue(_steer("too late", "s4"), _steer("  ", "blank"))
    events = flush_unapplied(q)
    assert events == [{"type": "steer_unapplied", "id": "s4", "text": "too late"}]
    assert saved == []
    assert flush_unapplied(q) == []
