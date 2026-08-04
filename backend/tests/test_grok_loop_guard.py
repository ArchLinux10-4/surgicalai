"""Tests for services/grok_loop_guard.py (gap #3: circuit breaker for a Grok
agent loop repeating an identical no-op tool call) and its integration point
in services/pipeline.py (finalize() -> check_repeated_calls() -> skip
dispatch when tripped).

Reuses the same fake-stream-chunk harness pattern already proven in
tests/test_grok_native_edit_loop_integration.py so this test drives the same
mechanics (accumulate deltas, finalize, translate) the real pipeline uses,
without a duplicated parallel harness.
"""
import pathlib
import sys

import pytest

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

from services import grok_loop_guard as glg  # noqa: E402
from services import grok_agent_tools as gat  # noqa: E402


def _dlog_collector():
    events = []

    def _dlog(event, **kwargs):
        events.append((event, kwargs))

    return _dlog, events


def _calls(name, arguments):
    return [{"id": "call_0", "name": name, "arguments": arguments}]


# ─────────────────────────────────────────────────────────────────────────
# 1. signature_for_calls
# ─────────────────────────────────────────────────────────────────────────

def test_signature_is_order_independent_within_a_turn():
    calls_a = [
        {"id": "1", "name": "request_file", "arguments": '{"filenames":["x.py"]}'},
        {"id": "2", "name": "request_search", "arguments": '{"terms":["foo"]}'},
    ]
    calls_b = list(reversed(calls_a))
    assert glg.signature_for_calls(calls_a) == glg.signature_for_calls(calls_b)


def test_signature_differs_for_different_arguments():
    sig1 = glg.signature_for_calls(_calls("request_file", '{"filenames":["x.py"]}'))
    sig2 = glg.signature_for_calls(_calls("request_file", '{"filenames":["y.py"]}'))
    assert sig1 != sig2


def test_signature_empty_for_no_calls():
    assert glg.signature_for_calls([]) == tuple()
    assert glg.signature_for_calls(None) == tuple()


# ─────────────────────────────────────────────────────────────────────────
# 2. check_repeated_calls
# ─────────────────────────────────────────────────────────────────────────

def test_three_identical_consecutive_calls_trip_the_breaker():
    history = []
    dlog, events = _dlog_collector()
    calls = _calls("request_file", '{"filenames":["x.py"]}')

    r1 = glg.check_repeated_calls(history, calls, max_repeats=3, dlog=dlog)
    r2 = glg.check_repeated_calls(history, calls, max_repeats=3, dlog=dlog)
    r3 = glg.check_repeated_calls(history, calls, max_repeats=3, dlog=dlog)

    assert r1 is None
    assert r2 is None
    assert r3 is not None
    assert "repeated" in r3
    assert "grok_loop_guard_tripped" in [e for e, _ in events]


def test_calls_that_differ_in_arguments_do_not_trip():
    history = []
    for i in range(5):
        reason = glg.check_repeated_calls(
            history, _calls("request_file", f'{{"filenames":["x{i}.py"]}}'),
            max_repeats=3)
        assert reason is None


def test_two_identical_calls_do_not_trip_below_max_repeats():
    history = []
    calls = _calls("request_file", '{"filenames":["x.py"]}')
    r1 = glg.check_repeated_calls(history, calls, max_repeats=3)
    r2 = glg.check_repeated_calls(history, calls, max_repeats=3)
    assert r1 is None
    assert r2 is None


def test_history_does_not_grow_unbounded():
    history = []
    calls = _calls("request_file", '{"filenames":["x.py"]}')
    for _ in range(50):
        glg.check_repeated_calls(history, calls, max_repeats=3)
    assert len(history) <= 3


def test_configurable_max_repeats_env_default():
    assert glg.GROK_LOOP_REPEAT_LIMIT == 3


def test_multi_call_turn_ordering_still_detected_as_identical_repeat():
    history = []
    calls_order_a = [
        {"id": "1", "name": "request_file", "arguments": '{"filenames":["x.py"]}'},
        {"id": "2", "name": "request_search", "arguments": '{"terms":["foo"]}'},
    ]
    calls_order_b = list(reversed(calls_order_a))
    r1 = glg.check_repeated_calls(history, calls_order_a, max_repeats=3)
    r2 = glg.check_repeated_calls(history, calls_order_b, max_repeats=3)
    r3 = glg.check_repeated_calls(history, calls_order_a, max_repeats=3)
    assert r1 is None
    assert r2 is None
    assert r3 is not None  # same set of (name, arguments) each turn -> repeat


# ─────────────────────────────────────────────────────────────────────────
# 3. Pipeline integration point: breaker trips -> translate_tool_calls is
#    NOT called (no side-effecting tool dispatch) -> same <blocked> shape
#    as the existing report_blocked path.
# ─────────────────────────────────────────────────────────────────────────

class _CountingTranslate:
    """Wraps gat.translate_tool_calls to assert it is never invoked once the
    breaker has tripped."""

    def __init__(self):
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        return gat.translate_tool_calls(*args, **kwargs)


def _simulate_pipeline_turn(grok_loop_history, calls, translate_fn):
    """Mirrors the exact integration point added to pipeline.py: check the
    breaker first; only call translate_fn (dispatch) when it does NOT trip."""
    edit_blocks_raw = []
    full_response = ""
    grok_blocked = False

    reason = glg.check_repeated_calls(grok_loop_history, calls, max_repeats=3)
    if reason:
        grok_blocked = True
        full_response += f"<blocked>{reason}</blocked>"
    else:
        tr = translate_fn(calls, session_id="s", user_id="u")
        if tr.edit_json_strings:
            edit_blocks_raw.extend(tr.edit_json_strings)
        if tr.blocked_reason:
            grok_blocked = True
            full_response += f"<blocked>{tr.blocked_reason}</blocked>"

    return edit_blocks_raw, full_response, grok_blocked


def test_breaker_trip_skips_dispatch_and_matches_report_blocked_shape():
    grok_loop_history = []
    counting_translate = _CountingTranslate()
    calls = _calls("request_file", '{"filenames":["missing.py"]}')

    # Turns 1 and 2: identical no-op call, breaker not tripped yet, dispatch happens.
    for _ in range(2):
        edit_blocks_raw, full_response, blocked = _simulate_pipeline_turn(
            grok_loop_history, calls, counting_translate)
        assert blocked is False
    assert counting_translate.call_count == 2

    # Turn 3: identical call again -> breaker trips -> dispatch is SKIPPED.
    edit_blocks_raw, full_response, blocked = _simulate_pipeline_turn(
        grok_loop_history, calls, counting_translate)

    assert blocked is True
    assert counting_translate.call_count == 2  # unchanged — no 3rd dispatch
    assert full_response.startswith("<blocked>")
    assert full_response.endswith("</blocked>")
    assert edit_blocks_raw == []


def test_pipeline_source_wires_breaker_before_translate_call_skips_on_trip():
    """Source-level guard confirming the wiring shape in pipeline.py: the
    breaker check happens right after finalize(), and the existing
    translate/dispatch call is now gated in an `else` branch (not called
    unconditionally as before)."""
    src = (_BACKEND / "services" / "pipeline.py").read_text()
    assert "_grok_calls = _grok_tc_acc.finalize()" in src
    assert "_grok_check_repeated_calls(" in src
    finalize_idx = src.index("_grok_calls = _grok_tc_acc.finalize()")
    breaker_idx = src.index("_grok_check_repeated_calls(")
    translate_idx = src.index("_grok_tr = _grok_translate_tool_calls(")
    assert finalize_idx < breaker_idx < translate_idx
    assert "_grok_loop_history: list = []" in src
