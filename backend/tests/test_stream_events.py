"""The Python and TypeScript stream event lists stay the same."""
import re
from pathlib import Path

from services.stream_events import EVENT_TYPES, check, phase_for_progress


def test_event_lists_match():
    ts = Path(__file__).resolve().parents[2].joinpath(
        "frontend/src/api/streamEvents.ts"
    ).read_text()
    names = re.findall(r"'([a-z0-9_]+)'", ts.split("] as const", 1)[0])
    assert set(names) == set(EVENT_TYPES)
    assert len(names) == len(EVENT_TYPES)


def test_check_ignores_events_unless_debug(monkeypatch):
    monkeypatch.delenv("SURGICAL_DEBUG", raising=False)
    check({"type": "not-a-real-event"})
    monkeypatch.setenv("SURGICAL_DEBUG", "1")
    try:
        check({"type": "not-a-real-event"})
        raised = False
    except ValueError:
        raised = True
    assert raised
    check({"type": "token", "content": "hi"})


def test_phase_for_progress_matches_the_pills():
    assert phase_for_progress("Searching codebase: logout") == "analyze"
    assert phase_for_progress("✍️ Writing code changes… 1.0 KB") == "code"
    assert phase_for_progress("Running QA on 2 change(s)...") == "qa"
    assert phase_for_progress("Thinking...") is None
