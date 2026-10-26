"""Ask mode must look a file up instead of promising to check.

Session dff697c8: gpt-6-luna finished at tool_round 0 with "I can check".
Grok, given native tools, called request_github and found the file.
"""
import asyncio
import json

from services import grok_ask_plan_native as gapn
from services.github_natural_tag import LOOKUP_BEFORE_ANSWER, build_github_prompt_section
from services.grok_agent_tools import build_grok_agent_tools
from services.pipeline import _gh_prompt_bits, _is_claude_model, _is_gemini_model, _is_gpt_model
from services.grok_provider import _is_grok_model
from tests.test_grok_ask_plan_native import (
    _Chunk, _Delta, _TCDelta, _drain, _iter_chunks_fn, _sse_events,
)


def _uses_native_ask_tools(model: str) -> bool:
    return _is_grok_model(model) or _is_gpt_model(model)


def test_gpt_ask_uses_native_tools_and_gemini_stays_on_tags():
    assert _uses_native_ask_tools("gpt-6-luna")
    assert _uses_native_ask_tools("gpt-4.1")
    assert _uses_native_ask_tools("grok-4.7")
    assert not _uses_native_ask_tools("gemini-2.5-pro")
    assert not _uses_native_ask_tools("claude-sonnet-5")
    assert _is_gemini_model("gemini-2.5-pro")
    assert _is_claude_model("claude-sonnet-5")
    assert not _is_gpt_model("gemini-2.5-pro")
    assert not _is_gpt_model("grok-4.7")


def test_grok_ask_tool_list_unchanged_without_a_repo():
    names = {t["function"]["name"] for t in build_grok_agent_tools("ask", github_enabled=True)}
    assert names == {
        "request_file", "request_search", "request_github", "report_blocked",
    }


def test_empty_known_repos_tells_the_model_to_use_the_users_name():
    bits = _gh_prompt_bits([])
    section = build_github_prompt_section([{"account_login": "acme"}])
    for text in (bits, section):
        assert "Do not invent a repo name" not in text
        assert "Call list_repos first if you do not know owner/repo." not in text
        assert LOOKUP_BEFORE_ANSWER in text


def test_promise_to_check_is_not_the_final_answer_when_github_is_on():
    promise = "I can check the repository for its login page files."
    gh_args = json.dumps({
        "tool": "list_files",
        "args": {"owner": "ArchLinux10-4", "repo": "marketrates.ai", "path_prefix": ""},
    })
    rounds = [
        [_Chunk(_Delta(content=promise)), _Chunk(finish_reason="stop")],
        [_Chunk(_Delta(tool_calls=[
            _TCDelta(0, id="call_gh", name="request_github", arguments=gh_args),
        ])), _Chunk(finish_reason="tool_calls")],
        [_Chunk(_Delta(content="The login page is src/auth/AuthGate.jsx.")),
         _Chunk(finish_reason="stop")],
    ]
    calls = []
    executed = []

    def _chat_create(client, model=None, messages=None, temperature=None, tools=None, stream=None, **_extra):
        calls.append({"tools": tools, "messages": list(messages or [])})
        return rounds[len(calls) - 1]

    def _execute(*, sr_match, fr_match, gr_match, **_kw):
        assert gr_match is not None
        executed.append(gr_match.group(1))
        return ["FILE src/auth/AuthGate.jsx"]

    agen = gapn.run_grok_ask_plan_native_stream(
        client=object(), chat_model="gpt-6-luna",
        all_messages=[
            {"role": "system", "content": "You are SurgicalAI."},
            {"role": "user", "content": "do you see the login page for marketrates.ai repo?"},
        ],
        mode="ask", gh_nat_enabled=True, gh_known_repos=[],
        symbol_maps_by_name={}, file_content_lookup={},
        execute_tool_round_fn=_execute,
        chat_create_fn=_chat_create, iter_chunks_fn=_iter_chunks_fn,
        max_rounds=8, deadline_s=30, session_id="s", user_id="u",
        dlog=lambda *a, **k: None,
    )
    events = _sse_events(_drain(agen))
    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert len(calls) == 3
    assert "request_github" in {t["function"]["name"] for t in calls[0]["tools"]}
    assert executed and "marketrates.ai" in executed[0]
    assert tokens and "AuthGate.jsx" in tokens[-1]
    assert promise not in (tokens[-1] if tokens else "")
    assert any("Looking it up" in (e.get("content") or "") for e in events if e["type"] == "progress")


def test_a_real_answer_is_still_final():
    chunks = [
        _Chunk(_Delta(content="The login page is src/auth/AuthGate.jsx.")),
        _Chunk(finish_reason="stop"),
    ]

    def _chat_create(client, model=None, messages=None, temperature=None, tools=None, stream=None, **_extra):
        return chunks

    agen = gapn.run_grok_ask_plan_native_stream(
        client=object(), chat_model="gpt-6-luna",
        all_messages=[{"role": "user", "content": "where is login?"}],
        mode="ask", gh_nat_enabled=True, gh_known_repos=[],
        symbol_maps_by_name={}, file_content_lookup={},
        execute_tool_round_fn=lambda **_kw: (_ for _ in ()).throw(AssertionError("no tool")),
        chat_create_fn=_chat_create, iter_chunks_fn=_iter_chunks_fn,
        max_rounds=4, deadline_s=30, session_id="s", user_id="u",
        dlog=lambda *a, **k: None,
    )
    events = _sse_events(_drain(agen))
    tokens = [e["content"] for e in events if e["type"] == "token"]
    assert tokens == ["The login page is src/auth/AuthGate.jsx."]
