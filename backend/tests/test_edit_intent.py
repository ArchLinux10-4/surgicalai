"""Question vs edit classification for the Edit-mode loop.

A plain question must not be treated as an edit request. Ambiguous wording
stays an edit request so a real change is never downgraded to chat.
"""
import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.edit_intent import (  # noqa: E402
    build_xml_agent_instruction,
    message_wants_edit,
    pending_saved_plan,
    strip_attachment_hint,
)

_HINT = (
    "[2 file(s) attached: AuthGate.tsx, useAuth.ts. "
    "Use them only if relevant to the question.]\n\n"
)
_OLD_HINT = (
    "[1 file(s) attached: Button.tsx. "
    "Examine their contents before responding.]\n\n"
)


# (text, kwargs, expected). About forty rows: questions, edits, the
# attachment hint, a saved plan, and ambiguous phrasing.
_CASES = [
    ("why does this hook re-render?", {}, False),
    ("what does AuthGate do?", {}, False),
    ("how does login work?", {}, False),
    ("where is the token checked?", {}, False),
    ("which file owns the session?", {}, False),
    ("when does the cache expire?", {}, False),
    ("who calls refreshToken?", {}, False),
    ("explain the auth flow", {}, False),
    ("describe the renderer", {}, False),
    ("does this function return null?", {}, False),
    ("is the middleware checking tokens?", {}, False),
    ("are these tests covering the edge?", {}, False),
    ("can you tell me why this fails?", {}, False),
    ("could you walk me through the renderer?", {}, False),
    ("What's going on with the cache?", {}, False),
    # How-to questions stay questions. "can you add" / "add a button" edit.
    ("how do I add a logout button?", {}, False),
    ("why does the add button fail?", {}, False),
    ("why does it show a spinner?", {}, False),
    ("can you explain how auth works?", {}, False),
    ("tell me about the session store", {}, False),
    ("Hey, why does this re-render?", {}, False),
    ("should the button be blue?", {}, False),
    ("ok?", {}, False),
    ("yes?", {}, False),
    (_HINT + "why does this hook re-render?", {}, False),
    (_OLD_HINT + "where is the token stored?", {}, False),
    ("why did you pick redux?", {"has_saved_plan": True}, False),
    ("add a button", {}, True),
    ("can you change the button color?", {}, True),
    ("could you fix the null check?", {}, True),
    ("please rename the handler", {}, True),
    ("build the plan", {}, True),
    ("implement this plan", {}, True),
    ("build off the plan", {}, True),
    ("fix the null check in login", {}, True),
    ("update the header copy", {}, True),
    ("delete the old modal", {}, True),
    ("refactor UserMenu", {}, True),
    ("Let's move the hook into its own file", {}, True),
    ("I want you to wire up the save handler", {}, True),
    ("can you add a logout button?", {}, True),
    (_HINT + "add a button", {}, True),
    (_OLD_HINT + "please change the color", {}, True),
    # Ambiguous on purpose: these must stay edit requests.
    ("the header feels off", {}, True),
    ("login", {}, True),
    ("maybe the cache", {}, True),
    ("hmm", {}, True),
    ("", {}, True),
    ("   ", {}, True),
    ("yes", {}, True),
    ("why does this hook re-render?", {"is_agent_task": True}, True),
    ("ok?", {"has_saved_plan": True}, True),
    ("go ahead", {"has_saved_plan": True}, True),
    ("sounds good", {"has_saved_plan": True}, True),
    ("do not build the plan", {}, False),
    ("don't change the button color", {}, False),
    ("do the refactor", {}, True),
]


@pytest.mark.parametrize("text,kwargs,expected", _CASES, ids=[
    f"{'edit' if exp else 'question'}-{i}" for i, (_, _, exp) in enumerate(_CASES)
])
def test_message_wants_edit_table(text, kwargs, expected):
    assert message_wants_edit(text, **kwargs) is expected


def test_attachment_hint_is_stripped_before_classification():
    stripped = strip_attachment_hint(_HINT + "why does this hook re-render?")
    assert not stripped.startswith("[")
    assert stripped.startswith("why does")


def test_pending_saved_plan_does_not_touch_the_db_for_explicit_seeds():
    assert pending_saved_plan("", [{"filename": "a.py"}], None) is True
    assert pending_saved_plan("", None, "run-1") is True
    assert pending_saved_plan("", None, None) is False
    assert pending_saved_plan("", [], None) is False


def test_edit_instruction_still_orders_an_edit():
    text = build_xml_agent_instruction(
        wants_edit=True, github_enabled=True, history_enabled=True,
        aws_guidance="\naws guidance\n",
    )
    assert "[AGENTIC EDIT MODE]" in text
    assert "write the edits now" in text
    assert "<github_request>" in text
    assert "<history_request>" in text
    assert "aws guidance" in text
    assert "Answer the user's message directly" not in text


def test_question_instruction_does_not_announce_edit_mode():
    text = build_xml_agent_instruction(
        wants_edit=False, github_enabled=True, history_enabled=False,
    )
    assert "[AGENTIC EDIT MODE]" not in text
    assert "write the edits now" not in text
    assert "MUST produce" not in text
    assert "Answer the user's message directly" in text
    assert "only if the user asked you to change code" in text
    assert "<search_request>" in text
    assert "<file_request>" in text
    assert "<github_request>" in text
    assert "<history_request>" not in text
    assert "no code changes were produced" in text


def test_grok_question_instruction_does_not_demand_edits():
    from services import grok_agent_tools as gat

    edit = gat.build_grok_agent_instruction("edit", wants_edit=True)
    question = gat.build_grok_agent_instruction("edit", wants_edit=False)
    assert "AGENTIC EDIT MODE" in edit
    assert "write the edits now" in edit
    assert "AGENTIC EDIT MODE" not in question
    assert "write the edits now" not in question
    assert "only if the user asked you to change code" in question
    assert "report_blocked" in question

    suffix = gat.build_grok_system_suffix("edit", wants_edit=False)
    assert "Do NOT narrate" not in suffix
    assert "only if the user asked you to change code" in suffix
    assert "write_surgical_edit" in suffix

    # Default stays the strict edit contract.
    assert "Do NOT narrate" in gat.build_grok_system_suffix("edit")


def test_separate_turn_prose_inserts_a_blank_line_between_turns():
    from services.pipeline import separate_turn_prose

    text, pending, needs = separate_turn_prose(
        "Local changes are on main.", break_pending=True, needs_break=True,
    )
    assert text == "\n\nLocal changes are on main."
    assert pending is False
    assert needs is True

    text, pending, needs = separate_turn_prose(
        "\nAlready broken", break_pending=True, needs_break=True,
    )
    assert text == "\nAlready broken"
    assert pending is False

    text, pending, needs = separate_turn_prose(
        "same turn", break_pending=False, needs_break=True,
    )
    assert text == "same turn"


def test_shared_voice_does_not_cheerlead_or_name_a_mode():
    from services.pipeline import CHAT_PERSONA, NATURAL_SYSTEM

    intro = NATURAL_SYSTEM[:600]
    for text in (CHAT_PERSONA, intro):
        assert "world-class" not in text
        assert "warm, encouraging" not in text
        assert "ASK mode" not in text
        assert "EDIT mode" not in text
    assert "mermaid" in CHAT_PERSONA
    assert "Answer first" in intro
    assert "<surgical_edit>" in NATURAL_SYSTEM
    assert "WHEN YOU NEED MORE CODE" in NATURAL_SYSTEM
