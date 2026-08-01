"""
Regression test for the bug reported by the user (production evidence:
surgical_debug_05b9699e.jsonl, session 05b9699e-f785-4e45-9fa1-28ab2a72cee9):
Ask/Plan mode showed zero <github_request> tool access whenever no
session_files were attached to the message, for every model (Grok, GPT,
Claude alike) — even though the user's GitHub was connected and Edit mode
in the very same session used it freely.

Root cause: `_ask_plan_tools_enabled = bool(session_files) and not
_should_use_ollama(chat_model)` gated the ENTIRE tool loop (search/file
AND github) behind file attachment, even though GitHub access has nothing
to do with attached files.

This test proves: with session_files=None/empty and GitHub connected,
Ask/Plan mode now (1) still runs the tool loop, (2) advertises ONLY the
github_request tag (no search_request/file_request, since there are no
files to search), and (3) actually executes a github_request round-trip.
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


@pytest.mark.asyncio
async def test_github_tools_available_with_zero_session_files(monkeypatch):
    monkeypatch.setattr(gh_tag, "natural_github_availability",
                         lambda user_id, dlog=None: (True, [{"account_login": "acme"}]))
    monkeypatch.setattr(gh_tag, "get_known_repos", lambda user_id, session_id, dlog=None: ["acme/repo"])
    monkeypatch.setattr(gh_tag, "execute_github_request",
                         lambda parsed, user_id, dlog=None: "Repo has 1 open PR.")

    round1 = '<github_request>{"tool": "list_prs", "args": {"owner": "acme", "repo": "repo"}}</github_request>'
    round2 = "Yes, your GitHub is connected — there is 1 open PR."
    fake_client = _FakeClient([_text_events(round1), _text_events(round2)])
    _common_monkeypatch(monkeypatch, fake_client)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "is my github connected? any open PRs?"}],
        model="claude-fake-model", user_id="u1", session_id="s1",
        session_files=None,  # <-- the exact reported bug condition: no files attached
    ))

    # The tool loop ran at all (bug: it used to be fully skipped here).
    assert len(fake_client.messages.calls) == 2, (
        "github_request tool loop did not run with zero session_files — "
        "this is the exact bug reported: GitHub tools require attaching "
        "a file first, even for pure Q&A about the connected repo."
    )

    # The system prompt sent to the model must NOT claim there's a file
    # index (there are no files), but MUST include the github tag docs.
    # Anthropic's API takes `system` as its own kwarg, not a role="system"
    # message inside `messages` — so it's read straight off the call kwargs.
    system_msg = fake_client.messages.calls[0]["system"]
    assert "Session Files (index only)" not in system_msg
    assert "search_request" not in system_msg
    assert "file_request" not in system_msg
    assert "github_request" in system_msg
    assert "acme/repo" in system_msg  # known repos line present

    # The github_request round actually executed and fed results back.
    second_call_msgs = fake_client.messages.calls[1]["messages"]
    assert "Repo has 1 open PR." in json.dumps(second_call_msgs)

    final_text = "".join(e["content"] for e in events if e.get("type") == "token")
    assert "<github_request>" not in final_text
    assert "1 open PR" in final_text
    print("PASS: github tools now available in Ask/Plan with zero session_files")


@pytest.mark.asyncio
async def test_no_tools_and_no_github_falls_back_to_legacy_streaming(monkeypatch):
    """When there are no files AND GitHub isn't connected/enabled, the tool
    loop must stay off entirely and behavior must be byte-identical to
    before this fix (live streaming, no tag docs in the system prompt)."""
    monkeypatch.setattr(gh_tag, "natural_github_availability",
                         lambda user_id, dlog=None: (False, []))

    round1 = "Plain answer, no tools needed."
    fake_client = _FakeClient([_text_events(round1)])
    _common_monkeypatch(monkeypatch, fake_client)

    events = await _collect(pipeline.run_chat_stream(
        messages=[{"role": "user", "content": "hello"}],
        model="claude-fake-model", user_id="u1", session_id="s1",
        session_files=None,
    ))

    assert len(fake_client.messages.calls) == 1
    system_msg = fake_client.messages.calls[0]["system"]
    assert "Tools available in this conversation" not in system_msg
    assert "Session Files (index only)" not in system_msg

    token_events = [e for e in events if e.get("type") == "token"]
    assert token_events
    assert "".join(e["content"] for e in token_events) == round1
    print("PASS: legacy no-tools streaming path unchanged when neither files nor github are available")
