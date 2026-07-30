"""
Regression test: the QA retry loop must catch a correction round that makes
the REAL compiler error count WORSE, and revert it, instead of silently
burning the retry budget on it and only discovering the damage at the final
gate (too late to try again).

Root cause (proven, trace surgical_debug_414dfaef (1).jsonl):
  - pre_check found 2 tsc-introduced errors in the edited file.
  - Round 0's auto-correction was re-QA'd (verdict still `blocked`, 1/10) but
    was NEVER re-measured by tsc -- only semantic QA reviewed it.
  - Round 1's "fix" was accepted into `change_shells[idx]["new_code"]` and
    re-QA'd (verdict `blocked`, 2/10) with no tsc check either.
  - Only the FINAL gate (after the whole 2-round retry budget was already
    spent) ran tsc again and found the file now had 8 introduced errors --
    round 1's attempt had made things strictly worse than round 0's, and
    nothing caught it until it was too late to try again automatically.
  - The change shipped with a `qa_advisory_warning` anyway. The user had to
    manually resubmit the QA report to get a working (9/10) fix -- something
    the automatic loop should have been able to do itself with its own
    budget, had it noticed the regression in time.

Contract under test: inside the QA retry loop (run_natural_pipeline_stream),
after each round's corrections + re-QA, every touched file must be
re-measured with the SAME tsc-introduced-errors helper used by pre_check and
the final gate. If a file's introduced-error count goes UP relative to the
best count ever seen for it, the round's changes to that file must be rolled
back to a captured pre-round snapshot rather than kept, and the event must be
`_dlog`-ed so it's diagnosable from a log, not just inferred after the fact.
"""
import ast
import pathlib

_SRC_PATH = pathlib.Path(__file__).parent.parent / "services" / "pipeline.py"


def _src() -> str:
    return _SRC_PATH.read_text()


def _natural_fn():
    tree = ast.parse(_src())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_natural_pipeline_stream":
            return node
    raise AssertionError("run_natural_pipeline_stream not found")


def _qa_retry_loop():
    fn_node = _natural_fn()
    for node in ast.walk(fn_node):
        if isinstance(node, ast.For) and getattr(node.target, "id", None) == "_qa_retry_round":
            return node
    raise AssertionError("qa_retry_round for-loop not found")


def test_tsc_round_best_baseline_seeded_before_retry_loop():
    src = _src()
    assert "_tsc_round_best: dict = {}" in src, (
        "expected a running best-introduced-error-count baseline dict to be "
        "declared before the QA retry loop"
    )
    assert "_tsc_round_best[_tfname] = len(_t_introduced)" in src, (
        "expected the pre_check loop to seed _tsc_round_best per filename "
        "with the pre_check tsc-introduced-error count"
    )


def test_pre_round_snapshot_captured_before_correction_attempts():
    src = _src()
    assert "_pre_round_snapshot = {" in src, (
        "expected a pre-round snapshot dict capturing new_code/diff/qa_result "
        "for every blocked index BEFORE this round's correction attempt runs "
        "-- required as a rollback target if the round regresses"
    )
    loop = _qa_retry_loop()
    loop_src = ast.dump(loop)
    assert "_pre_round_snapshot" in loop_src


def test_regression_check_runs_inside_retry_loop_for_every_round():
    """The regression check must be inside the `for _qa_retry_round` loop
    body (not only in the one-shot final gate after the loop), so every
    round -- not just the last -- is protected."""
    loop = _qa_retry_loop()
    found_call = False
    for node in ast.walk(loop):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_tsc_file_introduced_errors"
        ):
            found_call = True
            break
    assert found_call, (
        "expected a call to _tsc_file_introduced_errors INSIDE the "
        "_qa_retry_round loop body -- the per-round regression check must "
        "reuse the same real-compiler helper as pre_check/final_gate, not "
        "a separate ad-hoc check"
    )


def test_regression_triggers_rollback_not_silent_ship():
    src = _src()
    assert "qa_retry_regression_detected" in src, (
        "expected a _dlog event marking a detected regression -- required "
        "so a regressing correction round is diagnosable from a log, per "
        "the project's logging rule"
    )
    # The rollback must restore all three pieces of round-mutated state.
    for expected in (
        'change_shells[_ri]["new_code"] = _snap["new_code"]',
        'change_shells[_ri]["diff"] = _snap["diff"]',
        'qa_results[_ri] = _snap["qa_result"]',
    ):
        assert expected in src, f"rollback must restore: {expected}"
    assert 'qa_results[_ri]["regression_detected"] = True' in src, (
        "rolled-back indices must be flagged regression_detected so the "
        "frontend/history can show what happened, not silently disappear"
    )


def test_regression_rollback_only_fires_when_worse_than_best_ever_seen():
    """Must compare against the best (lowest) count ever observed for the
    file, not just the immediately preceding round -- otherwise two
    consecutive regressions could each look like an "improvement" relative
    to the previous (already-bad) round and never get caught."""
    src = _src()
    assert "_prev_best = _tsc_round_best.get(_rfname, 0)" in src
    assert "if _new_count > _prev_best:" in src
    # Only update the baseline (record as new best) on the non-regression path.
    assert "_tsc_round_best[_rfname] = _new_count" in src


def test_regression_rollback_emits_user_visible_progress_message():
    """Silence is exactly the original bug -- the pipeline must tell the
    user what happened, not just log it invisibly."""
    src = _src()
    assert "that correction attempt introduced MORE" in src, (
        "expected a user-visible SSE progress message when a regression is "
        "rolled back"
    )


def test_correction_history_reconciled_on_rollback():
    """A rolled-back round must not be recorded as `accepted: True` in the
    correction history the model sees on the next round -- otherwise the
    model has no signal that its own last attempt was reverted and may
    repeat it."""
    src = _src()
    assert '_correction_history[_ri][-1]["accepted"] = False' in src
