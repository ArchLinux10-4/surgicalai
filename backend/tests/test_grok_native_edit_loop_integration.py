"""Integration-style test proving the reported bug is fixed on the Grok native
path: Grok emitting native `tool_calls` (fragmented, indexed deltas) for a
`write_surgical_edit` must end up as a populated `edit_blocks_raw` entry and a
completed turn — whereas the old behavior (Grok narrating with no tool call)
produces zero edits.

This mirrors EXACTLY the mechanics wired into run_natural_pipeline_stream
(services/pipeline.py): the streamed-delta accumulation (R9) and the
translate→producer-variable step (R10). We drive them with fake OpenAI/xAI SDK
stream chunks so no network call is made.

Run from backend/:  pytest tests/test_grok_native_edit_loop_integration.py
"""
import json

from services import grok_agent_tools as gat
from services.grok_provider import _is_grok_model


# ── Fake OpenAI/xAI streaming SDK objects ───────────────────────────────────
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


def _drive_stream(chunks):
    """Replicate the pipeline's per-chunk handling for Grok: accumulate
    tool_call deltas (R9) and collect content, then translate (R10). Returns
    (edit_blocks_raw, new_file_blocks_raw, edit_plan_data, pending_tool,
    full_response, blocked)."""
    edit_blocks_raw = []
    new_file_blocks_raw = []
    edit_plan_data = None
    pending_tool = None
    full_response = ""
    blocked = False

    acc = gat.StreamedToolCallAccumulator(session_id="s", user_id="u")

    for ch in chunks:
        choice = ch.choices[0]
        if choice.finish_reason:
            break
        delta = choice.delta
        # content handling (mirrors pipeline)
        if delta is not None and getattr(delta, "content", None):
            full_response += delta.content
        # R9: tool-call delta assembly
        if delta is not None:
            tcd = getattr(delta, "tool_calls", None)
            if tcd:
                acc.add_delta(tcd)

    # R10: translate
    if acc.has_calls():
        calls = acc.finalize()
        tr = gat.translate_tool_calls(calls, session_id="s", user_id="u")
        if tr.edit_json_strings:
            edit_blocks_raw.extend(tr.edit_json_strings)
        if tr.new_file_json_strings:
            new_file_blocks_raw.extend(tr.new_file_json_strings)
        if tr.edit_plan is not None and edit_plan_data is None:
            edit_plan_data = tr.edit_plan
        if tr.context_request is not None and pending_tool is None:
            pending_tool = tr.context_request  # (kind, body) — pipeline runs _capture_request
        if tr.blocked_reason:
            blocked = True
            full_response += f"<blocked>{tr.blocked_reason}</blocked>"

    return edit_blocks_raw, new_file_blocks_raw, edit_plan_data, pending_tool, full_response, blocked


def test_grok_model_id_detected():
    # sanity: the gating predicate the pipeline uses recognizes grok-4.5
    assert _is_grok_model("grok-4.5") is True
    assert _is_grok_model("claude-sonnet-4-5") is False


def test_fragmented_surgical_edit_populates_edit_blocks_raw():
    """THE BUG FIX: fragmented native tool_calls for write_surgical_edit must
    become a real edit_blocks_raw entry that downstream json.loads can parse."""
    edit_args_full = json.dumps({
        "filename": "src/app.py",
        "symbol": "handle_login",
        "description": "return 200 on success",
        "new_code": "def handle_login(req):\n    return 200",
        "old_code": "def handle_login(req):\n    return 500",
    })
    # split the arguments JSON into 4 fragments across chunks, indexed
    third = len(edit_args_full) // 4
    frags = [edit_args_full[i:i + third] for i in range(0, len(edit_args_full), third)]

    chunks = []
    # first tool-call chunk carries id + name + first arg fragment
    chunks.append(_Chunk(_Delta(tool_calls=[
        _TCDelta(0, id="call_1", name="write_surgical_edit", arguments=frags[0])])))
    # remaining fragments carry args only
    for fr in frags[1:]:
        chunks.append(_Chunk(_Delta(tool_calls=[_TCDelta(0, arguments=fr)])))
    chunks.append(_Chunk(finish_reason="stop"))

    edits, newfiles, plan, pending, full_response, blocked = _drive_stream(chunks)

    # bug fixed: exactly one edit captured
    assert len(edits) == 1, "Grok tool call did not produce an edit block"
    decoded = json.loads(edits[0])
    assert decoded["filename"] == "src/app.py"
    assert decoded["symbol"] == "handle_login"
    assert decoded["new_code"].endswith("return 200")
    # downstream "turn produced" predicate: _new_edits > 0
    _new_edits = len(edits) + len(newfiles)
    assert _new_edits > 0
    assert not blocked


def test_old_bug_narration_only_produces_zero_edits():
    """Demonstrates the ORIGINAL bug shape: Grok narrates in content with no
    tool_calls => zero edits (this is exactly what users reported)."""
    chunks = [
        _Chunk(_Delta(content="I would update handle_login to return 200 ")),
        _Chunk(_Delta(content="instead of 500, but here is a description only.")),
        _Chunk(finish_reason="stop"),
    ]
    edits, newfiles, plan, pending, full_response, blocked = _drive_stream(chunks)
    assert len(edits) == 0
    assert len(newfiles) == 0
    assert full_response  # narration present
    # the native-tool path is what converts this into real edits


def test_parallel_edit_and_newfile_in_one_turn():
    edit_args = json.dumps({"filename": "a.py", "symbol": "f", "description": "d",
                            "new_code": "def f(): return 1"})
    file_args = json.dumps({"filename": "b.py", "language": "python",
                            "summary": "new helper", "content": "def g():\n    return 2"})
    chunks = [
        _Chunk(_Delta(tool_calls=[_TCDelta(0, id="c0", name="write_surgical_edit",
                                           arguments=edit_args)])),
        _Chunk(_Delta(tool_calls=[_TCDelta(1, id="c1", name="write_new_file",
                                           arguments=file_args)])),
        _Chunk(finish_reason="stop"),
    ]
    edits, newfiles, plan, pending, full_response, blocked = _drive_stream(chunks)
    assert len(edits) == 1
    assert len(newfiles) == 1


def test_native_search_request_becomes_pending_tool():
    chunks = [
        _Chunk(_Delta(tool_calls=[_TCDelta(0, id="s1", name="request_search",
                                           arguments=json.dumps({"terms": ["handle_login"],
                                                                 "reason": "find it"}))])),
        _Chunk(finish_reason="stop"),
    ]
    edits, newfiles, plan, pending, full_response, blocked = _drive_stream(chunks)
    assert edits == [] and newfiles == []
    assert pending is not None
    kind, body = pending
    assert kind == "search"
    assert json.loads(body)["terms"] == ["handle_login"]


def test_native_report_blocked_sets_terminal_signal():
    chunks = [
        _Chunk(_Delta(tool_calls=[_TCDelta(0, id="b1", name="report_blocked",
                                           arguments=json.dumps({"reason": "need config.py"}))])),
        _Chunk(finish_reason="stop"),
    ]
    edits, newfiles, plan, pending, full_response, blocked = _drive_stream(chunks)
    assert blocked is True
    assert "<blocked>" in full_response and "need config.py" in full_response


def test_mixed_content_and_tool_calls_in_same_stream():
    """Grok may emit reasoning/summary text AND a tool call — both must be
    handled; the edit still lands."""
    edit_args = json.dumps({"filename": "a.py", "symbol": "f", "description": "d",
                            "new_code": "x=1"})
    chunks = [
        _Chunk(_Delta(content="Let me fix that. ")),
        _Chunk(_Delta(content="Applying the edit now.")),
        _Chunk(_Delta(tool_calls=[_TCDelta(0, id="c0", name="write_surgical_edit",
                                           arguments=edit_args)])),
        _Chunk(finish_reason="stop"),
    ]
    edits, newfiles, plan, pending, full_response, blocked = _drive_stream(chunks)
    assert len(edits) == 1
    assert "Applying the edit now." in full_response
