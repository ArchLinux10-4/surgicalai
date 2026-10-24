"""The Research-off hint: obvious web-research requests, and nothing else."""
from pathlib import Path

import pytest

from services.edit_intent import build_xml_agent_instruction
from services.research_intent import HINT, message_requests_web_research, research_off_hint


@pytest.mark.parametrize("text", [
    "can you search the web for the latest Stripe API changes",
    "please do some online research on this",
    "google it and tell me",
    "look this up online first",
    "use web search to check",
    "search the internet for the error",
    "browse the web before you edit",
    "check online and then answer",
    "do a bit of web research",
    "with web search, what changed",
])
def test_obvious_research_requests_fire(text):
    assert message_requests_web_research(text) is True


@pytest.mark.parametrize("text", [
    "add a web search feature to the app",
    "fix the research page",
    "what is the latest version of next.js",
    "please research the caching bug",
    "",
    None,
    12,
    "update this:\n```\nsearch the web for bugs\nsearch_the_web = True\n```\nthanks",
    "set `search the web` in the config and ship it",
    "Traceback (most recent call last):\n"
    "  File \"app.py\", line 3, in main\n"
    "    search online\n"
    "RuntimeError: search online\n",
])
def test_ordinary_messages_do_not_fire(text):
    assert message_requests_web_research(text) is False


def test_hint_only_when_every_condition_lines_up(monkeypatch):
    store = {"research_off_hint": "on"}
    monkeypatch.setattr(
        "database.get_setting",
        lambda key, default="": store.get(key, default),
    )
    text = "please search the web for the stripe changelog"
    assert research_off_hint(text, flag_on=True, offered=True, is_agent_task=False) == ""
    assert research_off_hint(text, flag_on=False, offered=False, is_agent_task=False) == ""
    assert research_off_hint(text, flag_on=False, offered=True, is_agent_task=True) == ""
    assert research_off_hint("fix the button", flag_on=False, offered=True, is_agent_task=False) == ""
    store["research_off_hint"] = "off"
    assert research_off_hint(text, flag_on=False, offered=True, is_agent_task=False) == ""
    store["research_off_hint"] = "on"
    hint = research_off_hint(text, flag_on=False, offered=True, is_agent_task=False)
    assert hint == HINT
    assert "Research" in hint
    assert "turned off" in hint
    assert "<" not in hint


def test_pipeline_appends_the_hint_without_changing_the_instruction_builder():
    pipeline = Path("services/pipeline.py").read_text()
    assert "research_off_hint" in pipeline
    assert '_active_agent_instruction + "\\n\\n" + _research_hint' in pipeline
    assert 'system_parts.append("\\n" + _research_hint)' in pipeline
    plain = build_xml_agent_instruction(wants_edit=True, github_enabled=True, history_enabled=True)
    assert "turned off" not in plain
    assert "<web_search_request>" not in plain
    enabled = build_xml_agent_instruction(wants_edit=True, websearch_enabled=True)
    assert "<web_search_request>" in enabled
    assert "Do not include code" in enabled
