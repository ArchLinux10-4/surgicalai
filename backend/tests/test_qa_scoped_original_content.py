"""
Regression test: QA must be shown a "before" snapshot scoped to the same
region as the symbol's `new_code`, not the whole intermediate file, when
the symbol's own original text is still present verbatim in that file.

Root cause (fixed): `qa_original_content` was unconditionally set to the
WHOLE intermediate file (`_intermediate_contents[filename]`), even though
`new_code` for most edits is scoped to a single symbol -- e.g. a
`_preamble` "add missing import" edit is ~40 lines of `new_code` against
an 878-line file. `run_qa_agent` then diffs a 40-line NEW CODE against an
878-line ORIGINAL CODE and both the deterministic pre_qa_sanity check and
the LLM QA judge conclude the edit "catastrophically truncates the file",
blocking or scoring low a correct, safe edit.

Evidence: surgical_debug_c557dd1b.jsonl -- AddJobModal.jsx and
RegionBatchGenerator.jsx, both `_preamble` symbol edits ("add missing
import"), both flagged by QA as truncating the file when the edit itself
was correct and complete.

Fix: before handing `qa_original_content` to `run_qa_agent`, check whether
`symbol.code` (the correctly-scoped original snippet, already used for
`_make_diff`) is still findable verbatim inside the intermediate file. If
so, use `symbol.code` itself as the QA "before" snapshot (scoped,
like-for-like comparison). Only fall back to the whole intermediate file
when the symbol's original text can no longer be found verbatim -- i.e. a
prior change in this run already touched that exact span, which is the
cross-change-ordering case "Fix 1" was built to handle. This preserves
Fix 1's intent while fixing the common single-symbol-edit case.
"""
import ast
import pathlib

_SRC_PATH = pathlib.Path(__file__).parent.parent / "services" / "pipeline.py"
_SRC = _SRC_PATH.read_text()

_ANCHOR_START = "# QA sees the file state AFTER all previous changes to this file"
_ANCHOR_END = "diff = _make_diff(symbol.code, new_code, symbol.name)"


def _block():
    start = _SRC.index(_ANCHOR_START)
    end = _SRC.index(_ANCHOR_END, start)
    return _SRC[start:end]


def test_qa_original_content_prefers_scoped_symbol_code_when_found_verbatim():
    block = _block()
    assert 'if symbol.code and symbol.code in _qa_intermediate:' in block, (
        "QA must first check whether the symbol's own original text is "
        "still present verbatim in the intermediate file before falling "
        "back to the whole file"
    )
    assert "qa_original_content = symbol.code" in block, (
        "when the symbol's original text is still findable verbatim, QA's "
        "'before' snapshot must be scoped to just that symbol -- not the "
        "whole file -- so it is comparable in size to `new_code`"
    )


def test_qa_original_content_falls_back_to_whole_file_when_symbol_not_found():
    block = _block()
    assert "qa_original_content = _qa_intermediate" in block, (
        "when the symbol's original text can no longer be found verbatim "
        "(a prior change in this run already touched that exact span), QA "
        "must fall back to the whole intermediate file -- preserving the "
        "cross-change-ordering behavior Fix 1 was built for"
    )


def test_qa_original_content_scoping_has_dlog_on_both_branches():
    block = _block()
    assert "qa_original_content_scoped_to_symbol" in block, (
        "the scoped-to-symbol branch must _dlog so this decision is "
        "traceable per session/symbol"
    )
    assert "qa_original_content_fallback_whole_file" in block, (
        "the whole-file-fallback branch must _dlog so this decision is "
        "traceable per session/symbol"
    )


def test_qa_original_content_computed_before_diff_and_before_dict_assignment():
    # qa_original_content must be finalized before it is (a) used for the
    # diff and (b) stashed into the change_shells dict for later QA calls.
    scoped_idx = _SRC.index("qa_original_content = symbol.code")
    diff_idx = _SRC.index('diff = _make_diff(symbol.code, new_code, symbol.name)')
    dict_idx = _SRC.index('"qa_original_content": qa_original_content, # intermediate (for QA)')
    assert scoped_idx < diff_idx < dict_idx, (
        "qa_original_content must be fully resolved before it is used to "
        "build the diff and before it is stored for the QA call sites"
    )


def test_pipeline_module_still_parses():
    ast.parse(_SRC)


# ── Functional test: the exact scoping decision, isolated ──────────────────
def _resolve_qa_original_content(symbol_code: str, whole_file: str) -> str:
    """
    Mirrors the exact decision in services/pipeline.py's per-edit loop:
    scope to symbol.code when it is still verbatim-findable in the
    intermediate file, else fall back to the whole intermediate file.
    """
    if symbol_code and symbol_code in whole_file:
        return symbol_code
    return whole_file


def test_functional_scopes_to_symbol_when_present_verbatim():
    whole_file = "import os\nimport sys\n\ndef foo():\n    return 1\n" * 40  # ~40 lines-ish repeated, still whole file
    symbol_code = "import os\nimport sys\n"
    assert symbol_code in whole_file
    result = _resolve_qa_original_content(symbol_code, whole_file)
    assert result == symbol_code
    # This is the crux of the bug: scoped result must be far smaller than
    # the whole file for a small symbol like a _preamble import block.
    assert len(result.splitlines()) < len(whole_file.splitlines())


def test_functional_falls_back_to_whole_file_when_symbol_already_changed():
    whole_file = "import os, sys, json  # merged by change #1\n\ndef foo():\n    return 1\n"
    # Original symbol.code (pre-change #1) is NO LONGER present verbatim --
    # change #1 already rewrote this exact region (merged onto one line) in
    # this run, so the old two-line import block can't be found anymore.
    symbol_code = "import os\nimport sys\n"
    assert symbol_code not in whole_file
    result = _resolve_qa_original_content(symbol_code, whole_file)
    assert result == whole_file


def test_functional_handles_empty_symbol_code_without_crashing():
    # Defensive: symbol.code should never be empty in practice, but the
    # `if symbol.code and ...` guard must not let an empty string match
    # `in` (which would trivially be True for any string) and short-circuit
    # to the whole-file fallback safely instead.
    whole_file = "import os\n"
    result = _resolve_qa_original_content("", whole_file)
    assert result == whole_file
