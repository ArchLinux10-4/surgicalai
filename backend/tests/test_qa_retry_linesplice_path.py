"""
Regression test: the QA-retry correction applier must honor the
edit_start_line/edit_end_line + new_code format, not just old_code/new_code
snippets and full-symbol replacement.

Root cause (fixed): Claude's correction prompt explicitly allows THREE
response shapes for a fix: (1) windowed new_code (large symbols only),
(2) old_code + new_code (verbatim snippet match), (3) plain
edit_start_line/edit_end_line + new_code (absolute file line numbers --
the SAME "Option B" format already supported end-to-end on the main
resolution path via `_apply_snippet_by_lines`, see services/pipeline.py
~line 4432 and its call site ~line 17674). Before this fix, the
QA-retry correction applier only read `new_code` and `old_code` out of
the parsed correction JSON -- it silently ignored edit_start_line/
edit_end_line entirely. A correction using shape (3) fell straight
through to the full-symbol-replacement path (Path 3), which correctly
fragment-rejected the small snippet as "too small to be the whole
symbol" and the legitimate fix was discarded.

Evidence: session 29fba21b -- ExportDropdown.jsx correction round 1
returned edit_start_line/edit_end_line/new_code, was fragment-rejected
(`qa_retry_correction_fragment_rejected`), and the broken
duplicate-propTypes file shipped anyway (`verdict: blocked, score: 2`,
advisory-only ship). A later re-run of the identical prompt happened to
get old_code/new_code back instead, which Path 2 handled, and the fix
shipped (`score: 9`). Same bug, pure luck of response format.

Contract under test: the correction applier must extract
edit_start_line/edit_end_line from the parsed correction, and must call
_apply_snippet_by_lines for the un-windowed case BEFORE falling through
to the full-replacement fragment check.
"""
import ast
import pathlib

_SRC_PATH = pathlib.Path(__file__).parent.parent / "services" / "pipeline.py"
_SRC = _SRC_PATH.read_text()


def test_corrected_edit_start_end_line_are_extracted_from_parsed_correction():
    assert 'corrected_edit_start_line = edit_data.get("edit_start_line")' in _SRC, (
        "correction applier must extract edit_start_line out of the parsed "
        "correction JSON (it previously only read new_code/old_code)"
    )
    assert 'corrected_edit_end_line   = edit_data.get("edit_end_line")' in _SRC, (
        "correction applier must extract edit_end_line out of the parsed "
        "correction JSON (it previously only read new_code/old_code)"
    )


def test_linesplice_path_calls_apply_snippet_by_lines_with_symbol_start():
    assert "_apply_snippet_by_lines(\n                                    _sym_code, _sym_abs_start, _isl_corr, _iel_corr, corrected_code\n                                )" in _SRC, (
        "the new un-windowed line-splice correction path must call the "
        "existing, proven _apply_snippet_by_lines helper (reused from the "
        "main resolution path) with the symbol's absolute start_line -- "
        "not reinvent line-splicing logic"
    )


def test_linesplice_path_is_gated_to_unwindowed_corrections_only():
    # Must not fire when a windowed splice was already attempted, and must
    # require new_code without old_code (so Path 2's old_code/new_code
    # splice still takes priority when the model supplies old_code).
    needle = (
        "if (accepted is None and not _winfo and not _windowed_path_attempted\n"
        "                                and corrected_code and not corrected_old\n"
        "                                and corrected_edit_start_line and corrected_edit_end_line):"
    )
    assert needle in _SRC, (
        "line-splice correction path must be gated: accepted is None, no "
        "windowed correction was attempted, new_code present without "
        "old_code, and both edit_start_line/edit_end_line present"
    )


def test_linesplice_path_appears_before_path_2_old_code_splice():
    ls_idx = _SRC.index("# ── Path L: edit_start_line/edit_end_line splice (un-windowed) ──")
    path2_idx = _SRC.index("# ── Path 2: old_code/new_code snippet splice (fallback) ──")
    assert ls_idx < path2_idx, (
        "Path L (line-splice) must be attempted before Path 2 falls through "
        "to old_code/new_code matching, per the documented priority order"
    )


def test_linesplice_path_fragment_checks_its_result_before_accepting():
    # The spliced result must still go through _fragment_reason before
    # being accepted -- a valid-looking line range against a stale/mismatched
    # symbol snapshot could still produce garbage, so the same safety net
    # used everywhere else in the correction applier must apply here too.
    ls_idx = _SRC.index("# ── Path L: edit_start_line/edit_end_line splice (un-windowed) ──")
    path2_idx = _SRC.index("# ── Path 2: old_code/new_code snippet splice (fallback) ──")
    path_l_block = _SRC[ls_idx:path2_idx]
    assert "_fragment_reason(_sym_code, _ls_full)" in path_l_block, (
        "line-splice path must fragment-check its spliced result before "
        "accepting it, matching the safety guarantee every other "
        "correction-acceptance path in this loop provides"
    )


def test_linesplice_path_falls_through_on_bounds_failure_not_raise():
    # If _apply_snippet_by_lines reports out-of-bounds, the code must not
    # accept a None result -- it must fall through to Path 2/3 instead of
    # crashing or silently accepting garbage.
    ls_idx = _SRC.index("# ── Path L: edit_start_line/edit_end_line splice (un-windowed) ──")
    path2_idx = _SRC.index("# ── Path 2: old_code/new_code snippet splice (fallback) ──")
    path_l_block = _SRC[ls_idx:path2_idx]
    assert "correction_linesplice_bounds_failed" in path_l_block
    assert "falling through to Path 2/3" in path_l_block


def test_pipeline_module_still_parses():
    # Byte-level sanity: the whole file must still be valid Python after
    # this surgical insertion.
    ast.parse(_SRC)
