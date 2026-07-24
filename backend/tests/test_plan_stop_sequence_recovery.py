"""
Regression test for the dropped-plan starvation bug.

Root cause (proven from session d8f0ed39, uploaded by user, 2026-07-23,
turn 4 of the failing run):

Anthropic's `stop_sequence` list includes `</edit_plan>` so the tag can be
detected the instant it appears. When the model reaches that exact string,
Anthropic's API halts the stream immediately and never sends the closing
tag text itself (confirmed via `stream_halted_at_stop_sequence` log event:
`"matched": "</edit_plan>", "state_at_halt": "in_plan"`).

There is a per-turn recovery step meant to catch exactly this situation for
truncated tags, but its tuple was `("search", "filereq", "github",
"history")` -- "plan" was missing. So a fully-formed, valid plan the model
had just finished writing was thrown away with zero recovery attempt. The
very next log line is `agent_loop_text_only_nudge`, which falsely tells the
model "You wrote an explanation but did not produce any code changes" --
even though it *had* produced a complete plan. The model then restarted
its reasoning from scratch on the next turn, burning most of the phase's
time budget on pure re-thinking.

This test locks in `_recover_stopped_plan_tag`, the extracted helper that
now closes this gap, using realistic stop-sequence-truncated content (the
API always drops the closing tag itself, matching the real bytes observed
in production).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import _recover_stopped_plan_tag  # noqa: E402


def test_real_shape_stop_sequence_truncated_plan_recovers():
    """The API halts exactly at the stop_sequence match, so `full_response`
    ends with the plan JSON but WITHOUT the `</edit_plan>` closer -- the
    exact shape proven from session d8f0ed39 turn 4."""
    plan_json = (
        '[{"file": "routes/jobRoutes.js", "action": "edit", '
        '"detail": "Add country filter query param"}, '
        '{"file": "models/Job.js", "action": "edit", '
        '"detail": "Add country field to schema"}]'
    )
    full_response = (
        "I now have the context needed to wire up the country filter.\n\n"
        "<edit_plan>" + plan_json
        # No closing tag -- Anthropic's stop_sequence consumed it.
    )
    tag_buf = plan_json  # scanner's running buffer holds the same content
    result = _recover_stopped_plan_tag(full_response, tag_buf)
    assert result is not None
    assert len(result) == 2
    assert result[0]["file"] == "routes/jobRoutes.js"


def test_tag_buf_fallback_when_full_response_has_no_open_tag():
    """If the open-tag marker somehow isn't present in full_response (edge
    case), fall back to the scanner's own tag_buf rather than crashing."""
    plan_json = '[{"file": "a.py", "action": "edit", "detail": "x"}]'
    result = _recover_stopped_plan_tag("no tag marker here", plan_json)
    assert result == [{"file": "a.py", "action": "edit", "detail": "x"}]


def test_invalid_json_returns_none_not_crash():
    """Genuinely malformed content (mid-token cutoff, not stop_sequence)
    must degrade to None so the caller's normal empty/nudge path still
    runs -- never raise."""
    full_response = "<edit_plan>[{\"file\": \"a.py\", \"action\": \"ed"
    result = _recover_stopped_plan_tag(full_response, "not json at all")
    assert result is None


def test_non_list_json_returns_none():
    """A JSON object (not a list) is not a valid plan shape -- must not be
    accepted as one just because it parses."""
    full_response = '<edit_plan>{"file": "a.py"}'
    result = _recover_stopped_plan_tag(full_response, '{"file": "a.py"}')
    assert result is None


def test_custom_plan_open_marker_respected():
    """The helper must use whichever open-tag string TAG_DEFS supplies,
    not a hardcoded literal, so it stays correct if the tag ever changes."""
    plan_json = '[{"file": "b.py", "action": "edit", "detail": "y"}]'
    full_response = "<<CUSTOM_PLAN>>" + plan_json
    result = _recover_stopped_plan_tag(
        full_response, plan_json, plan_open="<<CUSTOM_PLAN>>")
    assert result == [{"file": "b.py", "action": "edit", "detail": "y"}]
