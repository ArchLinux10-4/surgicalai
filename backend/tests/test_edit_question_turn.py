"""Edit-mode questions are answered once. Edit requests still get a nudge.

Drives run_natural_pipeline_stream with a fake Claude stream so a question
cannot be pushed into a second "you must produce edits" turn, and cannot
end with the no-edits banner. An actual edit request still does both.
"""
import asyncio
import json
import sys
import types
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import pipeline  # noqa: E402


class _FakeDelta:
    def __init__(self, text=None):
        self.text = text
        self.thinking = None


class _FakeEvent:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


def _text_events(text):
    return [
        _FakeEvent("content_block_delta", delta=_FakeDelta(text=text)),
        _FakeEvent("content_block_stop"),
    ]


class _FakeStreamCtx:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for event in self._events:
            yield event

    async def get_final_message(self):
        return types.SimpleNamespace(stop_reason="end_turn", stop_sequence=None)


class _FakeMessages:
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._rounds:
            raise AssertionError("model was called again after the scripted turns")
        return _FakeStreamCtx(self._rounds.pop(0))


class _FakeClient:
    def __init__(self, rounds):
        self.messages = _FakeMessages(rounds)


def _user_text(call):
    parts = []
    for msg in call.get("messages") or []:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text") or "")
    return "\n".join(parts)


def _tokens(events):
    return "".join(
        e.get("content") or "" for e in events if e.get("type") == "token"
    )


async def _collect(agen):
    out = []
    async for chunk in agen:
        text = chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk
        if isinstance(text, str) and text.startswith("data: "):
            out.append(json.loads(text[6:]))
        else:
            out.append({"type": "_raw", "content": text})
    return out


def _install(monkeypatch, rounds):
    client = _FakeClient(rounds)

    def _setting(key, default=None):
        if key == "architect_model":
            return "claude-sonnet-4-20250514"
        return "" if default is None else default

    monkeypatch.setattr(pipeline, "get_setting", _setting)
    monkeypatch.setattr(pipeline, "_get_anthropic_key", lambda user_id="": "sk-test")
    monkeypatch.setattr(pipeline, "AsyncAnthropic", lambda api_key=None: client)
    monkeypatch.setattr(
        "services.github_natural_tag.natural_github_availability",
        lambda user_id, dlog=None: (False, []),
    )
    return client


async def _run(monkeypatch, user_request, rounds, is_agent_task=False):
    client = _install(monkeypatch, rounds)
    events = await _collect(pipeline.run_natural_pipeline_stream(
        session_files=[],
        user_request=user_request,
        conversation_history=[],
        session_id="",
        user_id="",
        is_agent_task=is_agent_task,
    ))
    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, errors
    assert events[-1]["type"] == "done"
    return client, events


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
    assert "didn't change any code" not in text
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
    assert "didn't change any code" in text
    assert "Tell me to go ahead" in text
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
    assert "didn't change any code" in _tokens(events)
