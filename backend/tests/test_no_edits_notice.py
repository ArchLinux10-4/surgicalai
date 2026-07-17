"""
Regression test for the agent-mode "no edits produced" notice selection.

Root cause (proven from session 6930f196, uploaded by user): `full_response`
is reset every turn in the agent loop (it's the LAST turn's transcript
only) and can contain raw, un-executed <search_request>/<filereq>/<github>
tag text that was NEVER streamed to the user as a "token" SSE event (only
`normal_buf` is streamed). The old code checked `full_response.strip()` and
always said "the response above was a plan/explanation only" -- false when
the AGENT_MAX_TURNS ceiling hit mid tool-call, which is exactly what
happened in session 6930f196: 24 turns spent searching/reading files, the
final turn was cut off mid <search_request>, and the user saw a vague,
inaccurate "plan only" notice with zero edits and zero explanation of why.

This test exercises `pick_no_edits_notice` directly against the real
recorded state combinations from that session and other edge cases.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import pick_no_edits_notice  # noqa: E402


def test_real_session_6930f196_max_turns_mid_search():
    """Real case: hit AGENT_MAX_TURNS while mid <search_request>. normal_buf
    empty (no real prose shown), full_response non-empty (raw tag text).
    Must NOT claim "the response above was a plan" -- must say it ran out
    of turns."""
    event, msg = pick_no_edits_notice(
        hit_agent_max_turns=True,
        has_skipped_changes=False,
        normal_buf="",
        full_response='<search_request>{"query": "isEditedFile"',
        agent_max_turns=24,
    )
    assert event == "no_edits_max_turns_notice"
    assert "ran out of turns" in msg
    assert "24" in msg
    assert "plan/explanation" not in msg


def test_max_turns_wins_even_if_some_normal_text_exists():
    """Even if a little real prose was streamed before the cutoff, the
    max-turns explanation is more accurate/honest than the generic
    'plan only' message, so max-turns takes priority."""
    event, msg = pick_no_edits_notice(
        hit_agent_max_turns=True,
        has_skipped_changes=False,
        normal_buf="Let me look at that file...",
        full_response='Let me look at that file...<search_request>{"query": "x"',
        agent_max_turns=24,
    )
    assert event == "no_edits_max_turns_notice"


def test_real_prose_no_max_turns_gets_plan_only_message():
    """Model wrote genuine, fully-streamed explanatory prose and stopped
    naturally (not a max-turns cutoff) with no edits. Old behavior
    preserved for this case."""
    event, msg = pick_no_edits_notice(
        hit_agent_max_turns=False,
        has_skipped_changes=False,
        normal_buf="Here's my plan: I would rename the function and update callers.",
        full_response="Here's my plan: I would rename the function and update callers.",
        agent_max_turns=24,
    )
    assert event == "no_edits_text_only_notice"
    assert "plan/explanation only" in msg


def test_cutoff_no_max_turns_no_real_prose_gets_honest_fallback():
    """Cut off some other way (e.g. phase/pipeline deadline, not
    AGENT_MAX_TURNS) with only raw un-executed tag text in full_response
    and nothing in normal_buf. Must not falsely claim a plan was shown."""
    event, msg = pick_no_edits_notice(
        hit_agent_max_turns=False,
        has_skipped_changes=False,
        normal_buf="",
        full_response='<filereq>["some_file.py"]',
        agent_max_turns=24,
    )
    assert event == "no_edits_notext_notice"
    assert "plan/explanation" not in msg
    assert "ran out of time" in msg


def test_nothing_at_all_gives_no_notice():
    """Both buffers empty -> no notice at all (caller just sends 'done')."""
    event, msg = pick_no_edits_notice(
        hit_agent_max_turns=False,
        has_skipped_changes=False,
        normal_buf="",
        full_response="",
        agent_max_turns=24,
    )
    assert event == "no_edits_silent"
    assert msg is None
