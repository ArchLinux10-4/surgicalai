"""
Regression tests for the architect-phase disconnect checkpoint.

Root cause (proven from surgical_debug_414dfaef.jsonl + Railway deploy
logs.1785437646610.json, session 414dfaef-c6f8-4fe7-99e0-c46416678272):

  - The user sent 3 prompts. The 1st completed normally (pipeline_complete).
  - The 2nd and 3rd both hit `sse_stream_disconnect` in chat.py's finally
    block mid-generation (turn 4 and turn 3 of the architect loop,
    respectively) — confirmed by a burst of full-page-reload-shaped GET
    calls (/api/chat/sessions, /api/chat/.../files, /api/surgical/applied/...)
    in the Railway logs 3-15s before each disconnect timestamp, and zero
    server-side tracebacks or stream_error_not_retried/streaming_deadline_abort
    events in the debug log — i.e. a genuine client-side disconnect
    (page reload), not a backend crash.
  - On the 2nd message, 4 <edit> tags had already closed cleanly in
    edit_blocks_raw (787+1089+1180+342 raw chars) BEFORE the 5th was cut off
    by the disconnect. agent_loop_done never fired, so resolution_phase_start
    (and the checkpoint added for session e4e9d098) never ran either.
    Those 4 completed, valid edits were silently discarded — the user saw
    "no code changes" despite Opus having actually written them.
  - The existing disconnect-checkpoint mechanism (session e4e9d098) only
    covers the LATER resolution/correction phase. This suite covers its
    new architect-phase counterpart: `_build_architect_checkpoint_resolved`
    plus its wiring into the turn loop in `run_natural_pipeline_stream`.
"""
import inspect
import json
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

from services.pipeline import _build_architect_checkpoint_resolved, run_natural_pipeline_stream


# ── Pure helper: shape and robustness ──────────────────────────────────────

def test_single_edit_block_parsed_into_recovery_shape():
    raw = json.dumps({
        "filename": "main.py",
        "symbol": "BatchProcessor.load_reference_data",
        "description": "Make the country filter strict",
        "new_code": "def load_reference_data(self):\n    pass\n",
    })
    result = _build_architect_checkpoint_resolved([raw], [])
    assert result == [{
        "filename": "main.py",
        "symbol": "BatchProcessor.load_reference_data",
        "description": "Make the country filter strict",
        "new_code": "def load_reference_data(self):\n    pass\n",
    }]


def test_matches_exact_shape_chat_py_recovery_reads():
    """chat.py's safety_net_checkpoint_recovered path reads exactly these
    four keys off each item — every key must be present even if the model
    omitted one, so `.get(..., default)` calls there never see a KeyError
    surface as a broken recovery message."""
    raw = json.dumps({"filename": "a.py", "new_code": "x = 1\n"})  # no symbol/description
    result = _build_architect_checkpoint_resolved([raw], [])
    assert set(result[0].keys()) == {"filename", "symbol", "description", "new_code"}
    assert result[0]["symbol"] == "?"
    assert result[0]["description"] == ""


def test_multiple_edit_blocks_all_recovered_in_order():
    raws = [
        json.dumps({"filename": "a.py", "symbol": "foo", "new_code": "1"}),
        json.dumps({"filename": "b.py", "symbol": "bar", "new_code": "2"}),
        json.dumps({"filename": "c.py", "symbol": "baz", "new_code": "3"}),
    ]
    result = _build_architect_checkpoint_resolved(raws, [])
    assert [r["filename"] for r in result] == ["a.py", "b.py", "c.py"]
    assert [r["new_code"] for r in result] == ["1", "2", "3"]


def test_new_file_block_uses_content_as_new_code_and_marks_symbol():
    raw = json.dumps({
        "filename": "new_module.py",
        "content": "print('brand new file')\n",
        "description": "Add helper module",
    })
    result = _build_architect_checkpoint_resolved([], [raw])
    assert result == [{
        "filename": "new_module.py",
        "symbol": "(new file)",
        "description": "Add helper module",
        "new_code": "print('brand new file')\n",
    }]


def test_mixes_edit_and_new_file_blocks():
    edit_raw = json.dumps({"filename": "a.py", "symbol": "foo", "new_code": "1"})
    file_raw = json.dumps({"filename": "b.py", "content": "2"})
    result = _build_architect_checkpoint_resolved([edit_raw], [file_raw])
    assert len(result) == 2
    assert result[0]["filename"] == "a.py" and result[0]["symbol"] == "foo"
    assert result[1]["filename"] == "b.py" and result[1]["symbol"] == "(new file)"


def test_json_with_literal_control_chars_repaired_not_dropped():
    """The exact failure _repair_json exists for: an LLM writing a literal
    newline inside a JSON string value instead of an escaped \\n. Must be
    recovered, not silently skipped — this is precisely the kind of edit
    block that's actually likely to be sitting in edit_blocks_raw when a
    disconnect hits mid-turn."""
    raw = '{"filename": "a.py", "symbol": "foo", "new_code": "line1\nline2"}'
    result = _build_architect_checkpoint_resolved([raw], [])
    assert len(result) == 1
    assert "line1" in result[0]["new_code"] and "line2" in result[0]["new_code"]


def test_completely_unparseable_block_skipped_not_raised():
    """A block that isn't JSON at all even after repair (e.g. cut off
    mid-stream by the exact disconnect this fix targets) must be dropped
    silently — never raise into the streaming loop that called this."""
    result = _build_architect_checkpoint_resolved(
        ['{"filename": "a.py", "symbol": "foo", "new_code": "1"}', "not json at all {{{"],
        [],
    )
    assert len(result) == 1
    assert result[0]["filename"] == "a.py"


def test_non_dict_json_skipped_not_raised():
    """A block that parses as valid JSON but isn't an object (e.g. a bare
    list or number) must be skipped, not crash on .get()."""
    result = _build_architect_checkpoint_resolved(["[1, 2, 3]", "42", "null"], [])
    assert result == []


def test_empty_inputs_return_empty_list():
    assert _build_architect_checkpoint_resolved([], []) == []


def test_never_raises_on_none_like_or_weird_values():
    """Structural guarantee mirrored from the rest of the codebase's
    tool-parsing helpers: a disconnect-checkpoint bug must never itself
    crash the very turn loop it's protecting."""
    result = _build_architect_checkpoint_resolved(
        ["", "   ", "{", '{"filename": 123}'], []
    )
    # Must not raise; whatever it returns must be a list.
    assert isinstance(result, list)


# ── Wiring: the turn loop actually calls this at the right point ──────────

def test_checkpoint_emission_wired_into_turn_loop_before_tool_dispatch():
    """Static-source check (same style as test_history_request_tool.py's
    is_agent_task gating test): the architect checkpoint must be built and
    yielded strictly AFTER `_new_edits` is computed for this turn and
    strictly BEFORE `pending_tool is not None` dispatch continues the loop
    into the next (disconnect-risking) turn — otherwise a turn's edits
    could be dispatched-past without ever having been checkpointed.
    """
    src = inspect.getsource(run_natural_pipeline_stream)

    new_edits_idx = src.index("_new_edits = (len(edit_blocks_raw)")
    dispatch_idx = src.index("Dispatch: run the requested tool, feed results back, continue")
    assert new_edits_idx < dispatch_idx, \
        "_new_edits must be computed before the tool-dispatch block"

    between = src[new_edits_idx:dispatch_idx]
    assert "_build_architect_checkpoint_resolved(" in between, \
        "checkpoint builder must be called between _new_edits and tool dispatch"
    assert '"type": "checkpoint"' in between, \
        "must yield an SSE checkpoint chunk in that same window"
    assert '"phase": "architect"' in between, \
        "checkpoint payload must be tagged so it's distinguishable from the " \
        "resolution-phase checkpoint if both ever coexist in one session"
    # Thin→rich enrich (disconnect → Apply cards, not markdown): must run
    # after the thin builder and before the checkpoint yield.
    thin_idx = between.index("_build_architect_checkpoint_resolved(")
    enrich_idx = between.index("enrich_thin_checkpoint_payload")
    yield_idx = between.index('"type": "checkpoint"')
    assert thin_idx < enrich_idx < yield_idx, \
        "enrich_thin_checkpoint_payload must sit between thin builder and yield"

    # Must be gated — never yield a checkpoint of nothing when the turn
    # produced no new edits (would spam every plain search/thinking turn).
    guard_idx = between.rfind("if _new_edits > 0:")
    assert guard_idx != -1
    ckpt_call_idx = between.index("_build_architect_checkpoint_resolved(")
    assert guard_idx < ckpt_call_idx


def test_checkpoint_yield_wrapped_in_try_except():
    """The checkpoint emission must never be able to kill the turn loop
    itself with an unrelated exception (e.g. a `yield` into a generator
    whose consumer already disconnected) — mirrors the resolution-phase
    checkpoint's own try/except (`resolution_checkpoint_error`)."""
    src = inspect.getsource(run_natural_pipeline_stream)
    idx = src.index('"phase": "architect"')
    surrounding = src[max(0, idx - 200):idx + 3200]
    assert "except Exception as _arch_ckpt_err:" in surrounding
    assert '"architect_checkpoint_error"' in surrounding
    assert "except Exception as _enrich_err:" in surrounding
    assert '"architect_checkpoint_enrich_error"' in surrounding
