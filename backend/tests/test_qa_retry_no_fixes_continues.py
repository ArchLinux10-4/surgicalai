"""
Regression test: QA retry loop must not give up after a round that
fixed zero indices, as long as retry budget remains.

Root cause (fixed): the multi-window content-gain/duplication guard can
correctly reject a bad window, which rolls back the WHOLE symbol
(hard_fail) even though other windows in that symbol spliced clean.
When that happens, `fixed_indices` is empty for the round even though
the symbol is still legitimately blocked and MAX_QA_RETRIES budget
remains. The retry loop used to treat "0 fixed this round" as
"nothing left to try" and `break` immediately -- silently discarding
an unused retry round the user was paying for.

Evidence: trace surgical_debug_29fba21b (3).jsonl -- round 0 rejected
a bad multi-window splice (correctly), fixed_indices == [], loop broke
via `qa_retry_no_fixes_breaking`, and the pipeline shipped the
still-blocked (score 2/10, 8 tsc errors) code. The user then manually
resubmitted with the same QA notes -- functionally identical to the
automatic round 1 that never ran -- and got score 9/10. Since
`qa_results` for a symbol is only updated for indices in
`fixed_indices` (unchanged when the round produced none), the next
round's `blocked_indices` recompute would have found the same symbol
still blocked and retried it fresh with accumulated correction
history -- exactly like the manual resubmission.

Contract under test: within the QA retry loop in
run_natural_pipeline_stream, when `fixed_indices` is empty, the code
must `continue` (not `break`) unless it is on the final retry round
(`_qa_retry_round >= MAX_QA_RETRIES - 1`), where breaking is correct
because there is no next round to use.
"""
import ast
import pathlib

_SRC_PATH = pathlib.Path(__file__).parent.parent / "services" / "pipeline.py"


def _natural_fn():
    src = _SRC_PATH.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_natural_pipeline_stream":
            return node, src
    raise AssertionError("run_natural_pipeline_stream not found")


def _find_qa_retry_for(fn_node):
    """Find the `for _qa_retry_round in range(MAX_QA_RETRIES):` loop."""
    for node in ast.walk(fn_node):
        if isinstance(node, ast.For):
            target = getattr(node.target, "id", None)
            if target == "_qa_retry_round":
                return node
    raise AssertionError("qa_retry_round for-loop not found inside run_natural_pipeline_stream")


def _find_fixed_indices_if(loop_node):
    """Find the `if not fixed_indices:` branch inside the retry loop."""
    for node in ast.walk(loop_node):
        if isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.UnaryOp)
                and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name)
                and test.operand.id == "fixed_indices"
            ):
                return node
    raise AssertionError("`if not fixed_indices:` branch not found inside qa retry loop")


def test_qa_retry_loop_exists_and_is_findable():
    fn_node, _ = _natural_fn()
    loop_node = _find_qa_retry_for(fn_node)
    assert loop_node is not None


def test_no_fixes_branch_does_not_unconditionally_break():
    """
    The top-level statement list of `if not fixed_indices:` must NOT be a bare
    `break` (or `_dlog(...)` immediately followed by a bare `break`) with no
    conditional gate on retry-round exhaustion. There must be an `ast.If`
    checking round exhaustion, and a `continue` for the case where rounds
    remain.
    """
    fn_node, _ = _natural_fn()
    loop_node = _find_qa_retry_for(fn_node)
    if_node = _find_fixed_indices_if(loop_node)

    body_stmt_types = [type(s) for s in if_node.body]
    assert ast.Continue in body_stmt_types, (
        "`if not fixed_indices:` branch must contain a `continue` for the "
        "case where retry rounds remain -- found statement types: "
        f"{[t.__name__ for t in body_stmt_types]}"
    )

    # Must have a nested If gating the break on round exhaustion.
    nested_ifs = [s for s in if_node.body if isinstance(s, ast.If)]
    assert nested_ifs, (
        "`if not fixed_indices:` branch must gate its `break` behind a "
        "check for retry-round exhaustion (e.g. "
        "`if _qa_retry_round >= MAX_QA_RETRIES - 1: break`), not break "
        "unconditionally."
    )

    # The bare (ungated) top-level body of `if not fixed_indices:` itself
    # must NOT contain a bare `break` outside of the nested exhaustion-gated If.
    top_level_breaks = [s for s in if_node.body if isinstance(s, ast.Break)]
    assert not top_level_breaks, (
        "found an unconditional `break` directly inside `if not "
        "fixed_indices:` -- this re-introduces the bug where a round with "
        "zero fixes discards remaining retry budget"
    )

    # The gated If's condition must reference MAX_QA_RETRIES so it only
    # fires when the budget is actually exhausted.
    gate = nested_ifs[0]
    gate_src = ast.dump(gate.test)
    assert "MAX_QA_RETRIES" in gate_src and "_qa_retry_round" in gate_src, (
        "round-exhaustion gate must compare _qa_retry_round against "
        f"MAX_QA_RETRIES -- got: {gate_src}"
    )

    # And that gated If's own body must break.
    gate_breaks = [s for s in gate.body if isinstance(s, ast.Break)]
    assert gate_breaks, "round-exhaustion gate must `break` when rounds are exhausted"


def test_source_no_longer_has_bare_no_fixes_breaking_dlog_as_final_action():
    """
    Byte-level guard: the exact old buggy pattern
        if not fixed_indices:
            _dlog("qa_retry_no_fixes_breaking", ...)
            break
    (with `break` as the ONLY action, no continue/round-check reachable)
    must not reappear verbatim.
    """
    _, src = _natural_fn()
    assert "continue  # rounds remain — recompute blocked_indices and retry fresh" in src, (
        "expected the round-exhaustion-gated continue statement to be present "
        "in run_natural_pipeline_stream's QA retry loop"
    )

