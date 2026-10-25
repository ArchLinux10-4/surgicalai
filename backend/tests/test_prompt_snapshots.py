"""Golden text for the prompts and the tool footer.

These fail when the words change. Update them on purpose with
UPDATE_SNAPSHOTS=1 and read the diff.
"""
import asyncio

from services.agent_tool_spec import local_text, repo_text
from services.edit_intent import build_xml_agent_instruction
from services.grok_agent_tools import build_grok_system_suffix
from services.pipeline import NATURAL_SYSTEM, _assemble_natural, grok_natural_system
from services.tool_footer import tool_footer
from tests.loop_harness import _run, _text_events
from tests.snapshot_util import assert_snapshot


def test_natural_system_claude():
    assert_snapshot("natural_system_claude", NATURAL_SYSTEM)


def test_natural_system_gpt():
    assert_snapshot("natural_system_gpt", _assemble_natural("gpt"))


def test_natural_system_grok():
    assert_snapshot("natural_system_grok", grok_natural_system())


def test_xml_instruction_edit():
    assert_snapshot("xml_instruction_edit", build_xml_agent_instruction(wants_edit=True))


def test_xml_instruction_answer():
    assert_snapshot("xml_instruction_answer", build_xml_agent_instruction(wants_edit=False))


def test_grok_suffix_edit():
    assert_snapshot("grok_suffix_edit", build_grok_system_suffix(mode="edit", wants_edit=True))


def test_grok_suffix_answer():
    assert_snapshot("grok_suffix_answer", build_grok_system_suffix(mode="edit", wants_edit=False))


def test_repo_and_local_text():
    assert_snapshot("repo_text_tag", repo_text("tag"))
    assert_snapshot("repo_text_function", repo_text("function"))
    assert_snapshot("local_text_tag", local_text("tag"))
    assert_snapshot("local_text_function", local_text("function"))


def test_tool_footer_is_the_same_for_every_kind():
    plain = tool_footer("search", "hit a.py:1")
    window = tool_footer(
        "file", "def main():\n    pass",
        continues_at=81, start_line=1, end_line=80, total=200,
    )
    assert_snapshot("footer_plain", plain)
    assert_snapshot("footer_window", window)
    for kind in ("search", "file", "github", "history", "preview", "mcp", "aws", "git", "local"):
        assert tool_footer(kind, "hit a.py:1") == plain


def test_text_only_nudge_snapshot(monkeypatch):
    client, _events = asyncio.run(_run(
        monkeypatch,
        "add a logout button",
        [
            _text_events("The header lives in templates/index.html today."),
            _text_events("Still only describing the button, with no edit."),
        ],
    ))
    messages = client.messages.calls[1]["messages"]
    last_user = next(m["content"] for m in reversed(messages) if m.get("role") == "user")
    if isinstance(last_user, list):
        last_user = "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in last_user
        )
    assert_snapshot("text_only_nudge", last_user)
