"""Prose held back for tag parsing is emitted when the turn ends."""
import asyncio

from tests.loop_harness import (
    _run,
    _text_events,
    _tokens,
    grok_repo_round,
    oai_text_round,
    stub_repo,
    tokens_before,
)

_SENTENCE = "I'll read templates/index.html now."
_ROUND1 = "The header lives in templates/index.html today."


def test_grok_native_tool_turn_emits_the_full_sentence(monkeypatch):
    stub_repo(monkeypatch)
    _client, events = asyncio.run(_run(
        monkeypatch,
        "where is the header defined?",
        [
            grok_repo_round(_SENTENCE),
            oai_text_round("It is in the header partial."),
        ],
        family="grok",
    ))
    assert tokens_before(events, "tool_activity") == _SENTENCE


def test_claude_text_only_nudge_keeps_the_first_sentence(monkeypatch):
    _client, events = asyncio.run(_run(
        monkeypatch,
        "add a logout button",
        [
            _text_events(_ROUND1),
            _text_events("Still only describing the button, with no edit."),
        ],
    ))
    assert _ROUND1 in _tokens(events)


def test_gpt_text_only_nudge_keeps_the_first_sentence(monkeypatch):
    _client, events = asyncio.run(_run(
        monkeypatch,
        "add a logout button",
        [
            oai_text_round(_ROUND1),
            oai_text_round("Still only describing the button, with no edit."),
        ],
        family="gpt",
    ))
    assert _ROUND1 in _tokens(events)


def test_partial_open_tag_is_not_shown(monkeypatch):
    _client, events = asyncio.run(_run(
        monkeypatch,
        "add a logout button",
        [
            _text_events("I'll read <file_re"),
            _text_events("The button goes in the header."),
        ],
    ))
    text = _tokens(events)
    assert "<file_re" not in text
    assert "I'll read" in text
