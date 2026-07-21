"""
Manual verification test (not part of the committed suite) for the new
Ask/Plan <github_request> read-only tool support. Mirrors the exact mocking
pattern already used and passing in tests/test_ask_plan_tools.py, but
patches services.github_natural_tag (imported lazily inside run_chat_stream)
to prove: (1) a read tool executes and feeds results back, (2) a write tool
is structurally blocked even if the model emits it, (3) the tag is a total
no-op when github isn't enabled for this user, and (4) round budget was
actually raised from 3 to 8.
"""
import json
import sys
import pytest

sys.path.insert(0, ".")

from services import pipeline
from services import github_natural_tag as gh_tag


class _FakeEvent:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeDelta:
    def __init__(self, text=None, thinking=None):
        self.text = text
        self.thinking = thinking


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
        for e in self._events:
            yield e


class _FakeMessages:
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        events = self._rounds.pop(0)
        return _FakeStreamCtx(events)


class _FakeClient:
    def __init__(self, rounds):
        self.messages = _FakeMessages(rounds)


async def _collect(agen):
    out = []
    async for chunk in agen:
        out.append(json.loads(chunk[6:]))
    return out


def _common_monkeypatch(monkeypatch, fake_client):
    monkeypatch.setattr(pipeline, "_is_claude_model", lambda m: True)
    monkeypatch.setattr(pipeline, "_is_gemini_model", lambda m: False)
    monkeypatch.setattr(pipeline, "_should_use_ollama", lambda m: False)
    monkeypatch.setattr(pipeline, "_get_anthropic_key", lambda user_id="": "fake-key")
    monkeypatch.setattr(pipeline, "AsyncAnthropic", lambda api_key: fake_client)
    monkeypatch.setattr(pipeline, "_max_output_tokens", lambda m: 4096)
    monkeypatch.setattr(pipeline, "_get_thinking_kwargs", lambda m, budget: {})
    monkeypatch.setattr(pipeline, "_get_effort_kwargs", lambda m: {})


SESSION_FILES = [{"filename": "a.py", "content": "x = 1\n"}]


@pytest.mark.asyncio
async def test_github_read_tool_executes_and_feeds_back(monkeypatch):
    monkeypatch.setattr(gh_tag, "natural_github_availability",
                         lambda user_id, dlog=None: (True, [{"account_login": "acme"}]))
    monkeypatch.setattr(gh_tag, "get_known_repos", lambda user_id, session_id, dlog=None: ["acme/repo"])
    monkeypatch.setattr(gh_tag, "execute_github_request",
                         lambda parsed, user_id, dlog=None: "PR #1: fix bug (open)")

    round1 = '<github_request>{"tool": "list_prs", "args": {"owner": "acme", "repo": "repo"}}</github_request>'
    round2 = "There is one open PR: fix bug."
    fake_client = _FakeClient([_text_events(round1), _text_events(round2)])
    _common_monkeypatch(monkeypatch, fake_client)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "any open PRs?"}],
        model="claude-fake-model", user_id="u1", session_id="s1",
        session_files=SESSION_FILES,
    ))

    assert len(fake_client.messages.calls) == 2
    second_call = json.dumps(fake_client.messages.calls[1]["messages"])
    assert "PR #1: fix bug (open)" in second_call
    final_text = "".join(e["content"] for e in events if e.get("type") == "token")
    assert "<github_request>" not in final_text
    assert "one open PR" in final_text
    print("PASS: github read tool executes and result is fed back")


@pytest.mark.asyncio
async def test_github_write_tool_structurally_blocked(monkeypatch):
    monkeypatch.setattr(gh_tag, "natural_github_availability",
                         lambda user_id, dlog=None: (True, [{"account_login": "acme"}]))
    monkeypatch.setattr(gh_tag, "get_known_repos", lambda user_id, session_id, dlog=None: [])

    executed = {"called": False}

    def _should_not_be_called(*a, **k):
        executed["called"] = True
        return "SHOULD NOT HAPPEN"

    monkeypatch.setattr(gh_tag, "execute_github_request", _should_not_be_called)

    round1 = '<github_request>{"tool": "push_files", "args": {"owner": "acme", "repo": "repo", "branch": "main", "message": "m", "files": []}}</github_request>'
    round2 = "Understood, I can't push from here."
    fake_client = _FakeClient([_text_events(round1), _text_events(round2)])
    _common_monkeypatch(monkeypatch, fake_client)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "push my changes"}],
        model="claude-fake-model", user_id="u1", session_id="s1",
        session_files=SESSION_FILES,
    ))

    assert executed["called"] is False, "push_files must NEVER reach execute_github_request from Ask/Plan"
    second_call = json.dumps(fake_client.messages.calls[1]["messages"])
    assert "not available in this mode" in second_call
    assert "Agent/Edit" in second_call
    print("PASS: push_files structurally blocked before execute_github_request")


@pytest.mark.asyncio
async def test_github_tag_noop_when_not_enabled_for_user(monkeypatch):
    monkeypatch.setattr(gh_tag, "natural_github_availability",
                         lambda user_id, dlog=None: (False, []))

    # Model still emits a github_request (e.g. stale prompt cache) — must be
    # inert since the tag is never even matched when _gh_nat_enabled is False.
    round1 = '<github_request>{"tool": "list_prs", "args": {}}</github_request>\nHere is my answer anyway.'
    fake_client = _FakeClient([_text_events(round1)])
    _common_monkeypatch(monkeypatch, fake_client)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "any open PRs?"}],
        model="claude-fake-model", user_id="u2", session_id="s2",
        session_files=SESSION_FILES,
    ))

    # Only ONE model call — no tool round triggered, github tag is literally
    # invisible to the loop when disabled for this user.
    assert len(fake_client.messages.calls) == 1
    final_text = "".join(e["content"] for e in events if e.get("type") == "token")
    assert "Here is my answer anyway." in final_text
    print("PASS: github tag is a true no-op when not enabled for this user")


@pytest.mark.asyncio
async def test_round_budget_raised_to_eight(monkeypatch):
    """Proves _ASK_PLAN_MAX_ROUNDS is 8 now, not 3 — 8 search rounds should
    all execute before the budget cuts the model off."""
    monkeypatch.setattr(gh_tag, "natural_github_availability", lambda user_id, dlog=None: (False, []))

    rounds = [_text_events('<search_request>{"terms": ["x"]}</search_request>') for _ in range(8)]
    rounds.append(_text_events("final answer after 8 rounds"))
    fake_client = _FakeClient(rounds)
    _common_monkeypatch(monkeypatch, fake_client)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "trace x everywhere"}],
        model="claude-fake-model", user_id="u1", session_id="s1",
        session_files=SESSION_FILES,
    ))

    # 8 tool rounds + 1 final = 9 model calls total.
    assert len(fake_client.messages.calls) == 9, fake_client.messages.calls
    final_text = "".join(e["content"] for e in events if e.get("type") == "token")
    assert "final answer after 8 rounds" in final_text
    print("PASS: round budget is 8, not 3")
