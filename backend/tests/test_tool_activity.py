"""Tool activity events: labels, redaction, tracker, trail, and the Grok loop.

Each test guards a failure a browser check would not reliably catch.
Run from backend/: pytest tests/test_tool_activity.py
"""
import asyncio
import json

from services.tool_activity import (
    ToolTracker,
    ToolTrail,
    describe_request,
    note_at,
    summarize_result,
)
from services import grok_ask_plan_native as gapn


def test_describe_request_accepts_json_string_and_dict_and_rejects_invalid():
    body = {"terms": ["handle_login", "session"], "reason": "need it"}
    from_dict = describe_request("search", body)
    from_json = describe_request("search", json.dumps(body))
    assert from_dict["label"] == from_json["label"] == "Searching the code"
    assert from_dict["target"] == from_json["target"] == "handle_login, session"
    assert describe_request("search", {"_invalid": "not json"}) is None


def test_redact_strips_bearer_sk_and_aws_keys_from_targets():
    git = describe_request("git", "git -c http.extraheader=Bearer supersecret123 push")
    assert "supersecret123" not in git["target"]
    assert "[redacted]" in git["target"]

    aws = describe_request(
        "aws",
        "aws s3 cp --token sk-abcdefghijklmnopqrstuvwxyz s3://bucket",
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in aws["target"]
    assert "[redacted]" in aws["target"]

    key = describe_request("git", "git push https://AKIAIOSFODNN7EXAMPLE@github.com/x")
    assert "AKIAIOSFODNN7EXAMPLE" not in key["target"]
    assert "[redacted]" in key["target"]


def test_summarize_file_never_includes_body_and_marks_missing_failed():
    body = "super-secret-body-xyz"
    note = f"FILE: a.py (2 lines)\n```\ndef secret():\n    return '{body}'\n```"
    ok, summary, preview = summarize_result("file", note)
    assert ok is True
    assert body not in summary
    assert body not in preview
    assert "a.py (2 lines)" in preview

    ok, summary, _preview = summarize_result("file", "FILE NOT FOUND: 'missing.py'")
    assert ok is False
    assert "FILE NOT FOUND" in summary


def test_tracker_approval_then_running_and_close_all():
    tracker = ToolTracker(agent="grok", id_prefix="")
    start = tracker.start("git", "git status")
    waiting = tracker.observe({"type": "git_approval_needed", "command": "git status"})
    running = tracker.observe({"type": "git_cli", "exit_code": 0, "stdout": "clean"})
    assert waiting["id"] == start["id"]
    assert waiting["status"] == "waiting"
    assert running["id"] == start["id"]
    assert running["status"] == "running"

    tracker.start("search", {"terms": ["left open"]})
    closed = tracker.close_all()
    assert len(closed) == 2
    assert all(ev["status"] == "stopped" and ev["summary"] == "Stopped" for ev in closed)
    assert tracker.end("should be a no-op") is None


def test_local_activity_waits_then_runs():
    tracker = ToolTracker(agent="claude", id_prefix="")
    start = tracker.start("local", "action: exec\nnpm test")
    assert start["label"] == "Running a local command"
    assert "npm test" in start["target"]
    waiting = tracker.observe({"type": "local_approval_needed", "command": "npm test"})
    running = tracker.observe({"type": "local_cli", "exit_code": 0, "stdout": "ok"})
    assert waiting["status"] == "waiting"
    assert running["status"] == "running"
    assert waiting["id"] == start["id"] == running["id"]


def test_trail_merges_caps_and_stops_open_rows():
    trail = ToolTrail()
    trail.observe({"type": "tool_activity", "id": "t1", "phase": "start", "kind": "search", "label": "Searching the code"})
    trail.observe({"type": "tool_activity", "id": "t1", "phase": "end", "ok": True, "ms": 12, "summary": "3 hits"})
    trail.observe({"type": "tool_activity", "id": "t1", "phase": "start", "label": "duplicate"})
    assert trail.rows()[0]["summary"] == "3 hits"
    assert trail.rows()[0]["status"] == "done"

    for i in range(45):
        trail.observe({"type": "tool_activity", "id": f"x{i}", "phase": "start", "kind": "file", "label": "Reading files"})
    rows = trail.rows()
    assert len(rows) == 40
    assert rows[5]["id"] == "more"
    assert "more steps" in rows[5]["label"]
    # The unfinished x* rows are stopped at save time, not left running.
    assert rows[-1]["status"] == "stopped"


def test_note_at_ignores_a_steering_message_appended_after_the_pair():
    messages = [
        {"role": "assistant", "content": "I'll look."},
        {"role": "user", "content": "FILE: a.py (4 lines)"},
        {"role": "user", "content": "Also, steer: change the color."},
    ]
    assert note_at(messages, 0) == "FILE: a.py (4 lines)"
    assert note_at(messages, 5) == ""


# ── Grok Ask/Plan loop emits matched start/end without dropping progress ──

class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _TCDelta:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, delta=None, finish_reason=None):
        self.choices = [_Choice(delta, finish_reason)]


def _iter_chunks(stream, **_kw):
    return iter(stream)


def _drain(agen):
    async def _run():
        return [x async for x in agen]
    return asyncio.run(_run())


def _events(chunks):
    return [json.loads(c[6:]) for c in chunks]


def _run(rounds, execute):
    call_log = []

    def _chat(client, model=None, messages=None, temperature=None, tools=None, stream=None):
        call_log.append(1)
        return rounds[len(call_log) - 1]

    agen = gapn.run_grok_ask_plan_native_stream(
        client=object(),
        chat_model="grok-4.5",
        all_messages=[
            {"role": "system", "content": "You are SurgicalAI."},
            {"role": "user", "content": "How does login work?"},
        ],
        mode="ask",
        gh_nat_enabled=False,
        gh_known_repos=[],
        symbol_maps_by_name={},
        file_content_lookup={},
        execute_tool_round_fn=execute,
        chat_create_fn=_chat,
        iter_chunks_fn=_iter_chunks,
        max_rounds=4,
        deadline_s=30,
        session_id="s", user_id="u", dlog=lambda *a, **k: None,
    )
    return _events(_drain(agen))


def test_grok_loop_emits_matched_tool_activity_and_keeps_progress():
    args = json.dumps({"terms": ["handle_login"], "reason": "need it"})
    rounds = [
        [
            _Chunk(_Delta(tool_calls=[_TCDelta(0, id="call_1", name="request_search", arguments=args)])),
            _Chunk(finish_reason="tool_calls"),
        ],
        [
            _Chunk(_Delta(content="handle_login returns 200.")),
            _Chunk(finish_reason="stop"),
        ],
    ]

    def _execute(*, sr_match, fr_match, gr_match, **_kw):
        assert sr_match is not None
        return ["FILE search hit: def handle_login"]

    events = _run(rounds, _execute)
    activity = [e for e in events if e["type"] == "tool_activity"]
    starts = [e for e in activity if e["phase"] == "start"]
    ends = [e for e in activity if e["phase"] == "end"]
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0]["id"] == ends[0]["id"]
    assert starts[0]["kind"] == "search"
    assert "handle_login" in starts[0]["target"]
    assert any(e["type"] == "progress" for e in events)
    tokens = [e for e in events if e["type"] == "token"]
    assert tokens and events.index(ends[0]) < events.index(tokens[-1])


def test_grok_blocked_round_leaves_no_open_tool_row():
    args = json.dumps({"reason": "The file is not in this chat."})
    rounds = [[
        _Chunk(_Delta(tool_calls=[_TCDelta(0, id="call_b", name="report_blocked", arguments=args)])),
        _Chunk(finish_reason="tool_calls"),
    ]]
    events = _run(rounds, lambda **_kw: ["unused"])
    activity = [e for e in events if e["type"] == "tool_activity"]
    started = {e["id"] for e in activity if e["phase"] == "start"}
    ended = {e["id"] for e in activity if e["phase"] == "end"}
    assert started == ended
