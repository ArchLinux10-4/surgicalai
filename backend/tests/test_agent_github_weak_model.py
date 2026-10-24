"""Edit-mode GitHub loop, driven by a fake Claude that makes cheap-model mistakes."""
import asyncio
import inspect
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


async def _collect(agen):
    out = []
    async for chunk in agen:
        text = chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk
        if isinstance(text, str) and text.startswith("data: "):
            out.append(json.loads(text[6:]))
    return out


def _install(monkeypatch, rounds, known=None, executor=None):
    client = _FakeClient(rounds)
    logs = []

    def _setting(key, default=None):
        if key == "architect_model":
            return "claude-sonnet-4-20250514"
        return "" if default is None else default

    def _log(event, **kw):
        logs.append((event, kw))

    monkeypatch.setattr(pipeline, "get_setting", _setting)
    monkeypatch.setattr(pipeline, "_dlog", _log)
    monkeypatch.setattr(pipeline, "_get_anthropic_key", lambda user_id="": "sk-test")
    monkeypatch.setattr(pipeline, "AsyncAnthropic", lambda api_key=None: client)
    monkeypatch.setattr(
        "services.github_natural_tag.natural_github_availability",
        lambda user_id, dlog=None: (True, [{"account_login": "acme", "permission_tier": "read_only"}]),
    )
    monkeypatch.setattr(
        "services.github_natural_tag.get_known_repos",
        lambda user_id, session_id, dlog=None, **kw: list(known or ["acme/app"]),
    )
    if executor is not None:
        monkeypatch.setattr("services.github_natural_tag.execute_github_request", executor)
    client.logs = logs
    return client


async def _run(monkeypatch, user_request, rounds, **kw):
    client = _install(monkeypatch, rounds, **kw)
    events = await _collect(pipeline.run_natural_pipeline_stream(
        session_files=[],
        user_request=user_request,
        conversation_history=[],
        session_id="s1",
        user_id="u1",
    ))
    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, errors
    return client, events


def test_unknown_tool_is_corrected_instead_of_called(monkeypatch):
    called = {"n": 0}

    def _exec(*a, **k):
        called["n"] += 1
        return "nope"

    client, _events = asyncio.run(_run(
        monkeypatch,
        "what's in my last PR?",
        [
            _text_events('<github_request>{"tool": "list_pull_requests", "args": {}}</github_request>'),
            _text_events("I should use list_prs."),
        ],
        executor=_exec,
    ))
    assert called["n"] == 0
    assert "Did you mean" in _user_text(client.messages.calls[1])


def test_placeholder_repo_uses_the_one_known_repo(monkeypatch):
    seen = {}

    def _exec(parsed, user_id, dlog=None, **kw):
        seen["args"] = parsed.get("args")
        return "PR #3 [open] Fix login"

    client, _events = asyncio.run(_run(
        monkeypatch,
        "what's in my last PR?",
        [
            _text_events('<github_request>{"tool": "list_prs", "args": {"repo": "REPO_NAME"}}</github_request>'),
            _text_events("Your latest PR is #3, Fix login."),
        ],
        known=["acme/app"],
        executor=_exec,
    ))
    assert seen["args"]["owner"] == "acme"
    assert seen["args"]["repo"] == "app"
    feedback = _user_text(client.messages.calls[1])
    assert "GitHub list_prs result:" in feedback
    assert "\\n" not in feedback


def test_paging_footer_survives_into_the_next_turn(monkeypatch):
    footer = "[TRUNCATED — 10 more lines. Call read_file again with start_line=301 to continue.]"
    payload = ("x" * 20000) + "\n" + footer

    def _exec(*a, **k):
        return payload

    client, _events = asyncio.run(_run(
        monkeypatch,
        "what's in my last PR?",
        [
            _text_events('<github_request>{"tool": "read_file", "args": {"owner": "acme", "repo": "app", "path": "big.py"}}</github_request>'),
            _text_events("The file starts with x."),
        ],
        executor=_exec,
    ))
    feedback = _user_text(client.messages.calls[1])
    assert "start_line=301" in feedback
    assert "\\n" not in feedback
    assert "GitHub read_file result:" in feedback


def test_identical_read_is_not_executed_twice(monkeypatch):
    called = {"n": 0}

    def _exec(*a, **k):
        called["n"] += 1
        return "PR #3 [open] Fix login"

    tag = '<github_request>{"tool": "list_prs", "args": {"owner": "acme", "repo": "app"}}</github_request>'
    client, _events = asyncio.run(_run(
        monkeypatch,
        "what's in my last PR?",
        [_text_events(tag), _text_events(tag), _text_events("PR #3 is Fix login.")],
        executor=_exec,
    ))
    assert called["n"] == 1
    assert "already have this result above" in _user_text(client.messages.calls[2])


def test_read_tools_are_not_charged_as_writes(monkeypatch):
    def _exec(*a, **k):
        return "PR #3 [open] Fix login"

    client, _events = asyncio.run(_run(
        monkeypatch,
        "what's in my last PR?",
        [
            _text_events('<github_request>{"tool": "list_prs", "args": {"owner": "acme", "repo": "app"}}</github_request>'),
            _text_events("There is one open PR."),
        ],
        executor=_exec,
    ))
    results = [kw for event, kw in client.logs if event == "agent_github_result"]
    assert results and results[0]["kind"] == "read"
    assert results[0]["tool"] == "list_prs"
    assert results[0]["writes"] == 0


def test_third_read_tells_an_edit_turn_it_has_enough(monkeypatch):
    def _exec(*a, **k):
        return "some github rows"

    def _tag(tool):
        return _text_events(
            '<github_request>{"tool": "%s", "args": {"owner": "acme", "repo": "app"}}</github_request>' % tool
        )

    client, _events = asyncio.run(_run(
        monkeypatch,
        "add a logout button",
        [
            _tag("list_prs"),
            _tag("list_issues"),
            _tag("list_branches"),
            _text_events("I would add a logout button."),
            _text_events("Still describing the button."),
        ],
        executor=_exec,
    ))
    assert "You have enough context. Answer or write the edits now." in _user_text(client.messages.calls[3])


def test_grok_rewrites_start_line_and_claude_does_not():
    source = inspect.getsource(pipeline.run_natural_pipeline_stream)
    assert "rewrite=_is_grok_model(arch_model)" in source
