"""Scripted turns against run_natural_pipeline_stream.

Claude, GPT, and Grok fakes. No network. Tool executors are patched at the
module they are imported from inside the dispatch branch.

The edit loop does not call `_get_client_for_model`. Grok uses
`get_grok_client`; GPT uses `_get_client`. Patch those.
"""
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


def tool_turn(text, tag_open, body, tag_close):
    """Prose, then one context-request tag, in a single Claude turn."""
    return [
        _FakeEvent("content_block_delta", delta=_FakeDelta(text=text)),
        _FakeEvent("content_block_delta", delta=_FakeDelta(text=tag_open + body + tag_close)),
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


class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _TCDelta:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _OAIDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _OAIChoice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _OAIChunk:
    def __init__(self, delta=None, finish_reason=None):
        self.choices = [_OAIChoice(delta, finish_reason)]


def oai_text_round(text, finish_reason="stop"):
    return [
        _OAIChunk(_OAIDelta(content=text)),
        _OAIChunk(_OAIDelta(), finish_reason=finish_reason),
    ]


def grok_repo_round(text, tool="read_file", path="templates/index.html"):
    """Content, then a request_repo tool call, then finish_reason.

    finish_reason is its own chunk. The loop breaks on finish_reason before
    it reads that chunk's delta, so the tool call has to arrive first.
    """
    args = json.dumps({"tool": tool, "path": path, "terms": []})
    return [
        _OAIChunk(_OAIDelta(content=text)),
        _OAIChunk(_OAIDelta(tool_calls=[_TCDelta(
            0, id="call_1", name="request_repo", arguments=args,
        )])),
        _OAIChunk(_OAIDelta(), finish_reason="tool_calls"),
    ]


class _FakeCompletions:
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._rounds:
            raise AssertionError("model was called again after the scripted turns")
        return iter(self._rounds.pop(0))


class _FakeOAIClient:
    def __init__(self, rounds):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(rounds))
        self.calls = self.chat.completions.calls


def _user_text(call):
    parts = []
    messages = call.get("messages") or []
    for msg in messages:
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


def user_text_from_oai(call):
    """User text from a chat.completions.create kwargs dict."""
    return _user_text(call)


def _tokens(events):
    return "".join(
        e.get("content") or "" for e in events if e.get("type") == "token"
    )


def tokens_before(events, type_name):
    parts = []
    for event in events:
        if event.get("type") == type_name:
            break
        if event.get("type") == "token":
            parts.append(event.get("content") or "")
    return "".join(parts)


async def _collect(agen):
    out = []
    async for chunk in agen:
        text = chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk
        if isinstance(text, str) and text.startswith("data: "):
            out.append(json.loads(text[6:]))
        else:
            out.append({"type": "_raw", "content": text})
    return out


_MODELS = {
    "claude": "claude-sonnet-4-20250514",
    "gpt": "gpt-4.1",
    "grok": "grok-4.5",
}


def _install(monkeypatch, rounds, family="claude", logs=None):
    model = _MODELS[family]

    def _setting(key, default=None):
        if key == "architect_model":
            return model
        return "" if default is None else default

    monkeypatch.setattr(pipeline, "get_setting", _setting)
    monkeypatch.setattr(pipeline, "_get_anthropic_key", lambda user_id="": "sk-test")
    monkeypatch.setattr(
        "services.github_natural_tag.natural_github_availability",
        lambda user_id, dlog=None: (False, []),
    )
    if logs is not None:
        def _log(event, **kw):
            logs.append((event, kw))
        monkeypatch.setattr(pipeline, "_dlog", _log)

    if family == "claude":
        client = _FakeClient(rounds)
        monkeypatch.setattr(pipeline, "AsyncAnthropic", lambda api_key=None: client)
        return client

    client = _FakeOAIClient(rounds)
    # Corrections still construct an Anthropic client. Give it an empty script
    # so a surprise call fails the test instead of hitting the network.
    monkeypatch.setattr(pipeline, "AsyncAnthropic", lambda api_key=None: _FakeClient([]))
    if family == "grok":
        monkeypatch.setattr(pipeline, "get_grok_client", lambda *a, **k: client)
    else:
        monkeypatch.setattr(pipeline, "_get_client", lambda user_id="": client)
    return client


def stub_repo(monkeypatch, note="FILE: x.py\n1: pass\n"):
    monkeypatch.setattr(
        "services.repo_tools.execute_repo_tool",
        lambda *a, **k: note,
    )


def stub_local(monkeypatch, note="pm\n", run=True):
    async def _fake(*a, **k):
        yield {"type": "local_done", "note": note, "run": run}

    monkeypatch.setattr("services.local_exec.resolve_local_call", _fake)


async def _run(monkeypatch, user_request, rounds, is_agent_task=False,
               family="claude", logs=None, session_files=None):
    client = _install(monkeypatch, rounds, family=family, logs=logs)
    events = await _collect(pipeline.run_natural_pipeline_stream(
        session_files=session_files or [],
        user_request=user_request,
        conversation_history=[],
        session_id="",
        user_id="",
        is_agent_task=is_agent_task,
    ))
    errors = [e for e in events if e.get("type") == "error"]
    assert not errors, errors
    assert events[-1]["type"] == "done", events[-1]
    return client, events
