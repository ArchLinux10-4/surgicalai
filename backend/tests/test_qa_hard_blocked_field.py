"""
Regression test: a genuinely `blocked` QA verdict that survives every
auto-retry attempt must be distinguishable, in both the log and the schema
shipped to the frontend, from a routine soft/borderline advisory.

Root cause (proven, trace surgical_debug_414dfaef (1).jsonl):
`qa_agent_parsed` verdict `blocked` (score 2/10) with real tsc-confirmed
compile errors was logged and shipped through the exact same
`qa_advisory_warning` path -- same icon, same generic message -- as a
routine "score is 7, borderline" or "QA transiently failed to run" case.
There was no field anywhere that said "this one is different: it is
confirmed broken and out of retries", so nothing (frontend or otherwise)
could act on that distinction even though the backend already knew it.

Contract under test:
  - QAResult schema carries `hard_blocked` / `regression_detected` fields.
  - `_force_block_on_tsc` only sets `hard_blocked=True` when called with a
    non-empty `_suffix` (i.e. from the FINAL gate, after retries are
    exhausted) -- never for the routine pre_check call that the retry loop
    is expected to fix.
  - The advisory-emission block marks a genuine `blocked` verdict
    `hard_blocked=True` and dlogs the flag explicitly.
  - The final `_QAResult(...)` construction actually forwards these fields
    to the object the frontend receives -- a field that exists in the
    schema but is never populated on the response object is a no-op fix.
"""
import ast
import pathlib

_SRC_PATH = pathlib.Path(__file__).parent.parent / "services" / "pipeline.py"
_SCHEMA_PATH = pathlib.Path(__file__).parent.parent / "models" / "schemas.py"


def _pipeline_src() -> str:
    return _SRC_PATH.read_text()


def test_schema_has_hard_blocked_and_regression_fields_with_safe_defaults():
    src = _SCHEMA_PATH.read_text()
    tree = ast.parse(src)
    qa_class = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "QAResult":
            qa_class = node
            break
    assert qa_class is not None, "QAResult class not found in schemas.py"

    field_defaults = {}
    for stmt in qa_class.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            field_defaults[stmt.target.id] = stmt.value

    for field in ("hard_blocked", "regression_detected"):
        assert field in field_defaults, f"QAResult must declare `{field}`"
        default_node = field_defaults[field]
        assert isinstance(default_node, ast.Constant) and default_node.value is False, (
            f"`{field}` must default to False so every existing caller that "
            f"doesn't set it explicitly stays exactly as safe/advisory as "
            f"before -- got default {ast.dump(default_node)}"
        )


def test_force_block_on_tsc_only_hard_blocks_on_final_gate_suffix():
    """pre_check errors are EXPECTED to be caught and fixed by the retry
    loop -- marking them hard_blocked immediately would misrepresent a
    normal, working part of the pipeline as a failure. Only the final gate
    (which passes a non-empty _suffix) should set the flag."""
    src = _pipeline_src()
    idx = src.index("def _force_block_on_tsc")
    body = src[idx:idx + 1500]
    assert 'if _suffix:' in body and 'qa_results[_idx]["hard_blocked"] = True' in body
    # Must be gated by the suffix check, not unconditional.
    gated_slice = body[body.index("if _suffix:"):body.index("if _suffix:") + 120]
    assert 'hard_blocked' in gated_slice


def test_pre_check_call_passes_empty_suffix_final_gate_passes_nonempty():
    src = _pipeline_src()
    # Session 3a6150e9: pre_check / final_gate force-block ONLY attributed
    # owners (or fall back to full set). Suffix contract is unchanged.
    assert '_force_block_on_tsc(_ti, _errs, "")' in src, (
        "pre_check call to _force_block_on_tsc must pass an empty suffix "
        "(never hard_blocked -- the retry loop hasn't even run yet)"
    )
    assert '_force_block_on_tsc(_ti, _errs, " remain after auto-fix")' in src, (
        "final gate call must pass a non-empty suffix so surviving errors "
        "are marked hard_blocked"
    )
    assert "attribute_tsc_errors_to_indices" in src


def test_advisory_block_marks_genuine_blocked_verdict_hard_blocked():
    src = _pipeline_src()
    idx = src.index('_dlog("qa_advisory_warning"')
    # Look backwards a reasonable window for the marking logic that must
    # precede this dlog call in the same conditional branch.
    window = src[max(0, idx - 1500):idx]
    assert 'if _gv == "blocked":' in window
    assert 'qa_dict["hard_blocked"] = True' in window


def test_advisory_dlog_reports_hard_blocked_and_regression_flags():
    src = _pipeline_src()
    idx = src.index('_dlog("qa_advisory_warning"')
    call_src = src[idx:idx + 700]
    assert "hard_blocked=qa_dict.get(\"hard_blocked\"" in call_src, (
        "qa_advisory_warning dlog must report hard_blocked so a log alone "
        "(without needing frontend state) proves whether a shipped-anyway "
        "change was a genuine hard block"
    )
    assert "regression_detected=qa_dict.get(\"regression_detected\"" in call_src


def test_qa_result_object_forwards_new_fields_to_frontend_payload():
    """A field that exists on the pydantic model but is never passed when
    constructing the response object is a no-op fix -- the frontend would
    always see the default (False) regardless of what actually happened."""
    src = _pipeline_src()
    idx = src.index("qa_result_obj = _QAResult(")
    call_src = src[idx:idx + 1200]
    assert "hard_blocked=qa_dict.get(\"hard_blocked\"" in call_src
    assert "regression_detected=qa_dict.get(\"regression_detected\"" in call_src
    assert "block_sources=list(qa_dict.get(\"block_sources\")" in call_src
    assert "machine_verified=bool(qa_dict.get(\"machine_verified\")" in call_src
