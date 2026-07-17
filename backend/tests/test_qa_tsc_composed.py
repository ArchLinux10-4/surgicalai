"""
Regression tests for the QA auto-heal fixes (session 6930f196).

Proven root causes from surgical_debug_6930f196 (10 runs, Jul 16-17 2026):

Bug A (phantom tsc errors): _tsc_introduced_errors validated each symbol
edit in ISOLATION against the original file. Coordinated sibling edits
(widen FileFilter union in one edit + use the new 'edited' member in
another) each raised TS2367 alone but compile clean together. The isolated
check blocked the using-edit at score 3 in all 8 runs that touched
matchesFileFilter, and the final gate re-broke every successful correction
(safe/10 at 00:15:05 -> re-blocked at 00:15:12). Fix: compose all of a
file's edits before tsc — compose_symbol_edits + diff_introduced_errors.

Bug B (wrong-symbol corruption): the corrector, told to fix the phantom
TS2367, correctly returned the union widening — which was spliced in as the
full replacement of the 5-line matchesFileFilter function, deleting it
(re-QA: "replaces the function body with an unrelated type alias", score 1).
The fragment guard skips symbols under 40 lines. Fix: wrong_symbol_reason.

Run: pytest test_qa_tsc_composed.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import (  # noqa: E402
    compose_symbol_edits,
    diff_introduced_errors,
    wrong_symbol_reason,
)


# ── Simple apply_fn used by composition tests ─────────────────────────────
def _apply(content, proxy):
    """Mimics the pipeline apply: find/replace the single operation."""
    op = proxy.operations[0]
    if op["find"] not in content:
        raise ValueError("anchor not found")
    return content.replace(op["find"], op["replace"], 1)


def _proxy(find, replace):
    return type("_SC", (), {
        "operations": [{"find": find, "replace": replace}],
        "applied": False,
    })()


# Real shape from frontend/src/lib/fileClassify.tsx @ f54486a
ORIG_FILE = """export type FileFilter = 'all' | 'current' | 'new';

export function matchesFileFilter(f, filter) {
  if (filter === 'all') return true;
  if (filter === 'current') return isCurrentFile(f);
  return isNewFile(f);
}
"""

UNION_FIND = "export type FileFilter = 'all' | 'current' | 'new';"
UNION_REPLACE = "export type FileFilter = 'all' | 'current' | 'new' | 'edited';"
FN_FIND = "  if (filter === 'current') return isCurrentFile(f);"
FN_REPLACE = (
    "  if (filter === 'current') return isCurrentFile(f);\n"
    "  if (filter === 'edited') return isEditedFile(f);"
)


# ── compose_symbol_edits ──────────────────────────────────────────────────

class TestComposeSymbolEdits:
    def test_sibling_edits_compose_together(self):
        """The exact session-6930f196 pair: union widening + new member use."""
        composed, applied = compose_symbol_edits(
            ORIG_FILE,
            [(0, _proxy(UNION_FIND, UNION_REPLACE)),
             (1, _proxy(FN_FIND, FN_REPLACE))],
            _apply,
        )
        assert applied == [0, 1]
        assert "| 'edited'" in composed
        assert "isEditedFile(f)" in composed

    def test_edits_apply_sequentially_not_in_isolation(self):
        """Second edit must be applied on TOP of the first, not the original."""
        composed, applied = compose_symbol_edits(
            ORIG_FILE,
            [(0, _proxy(UNION_FIND, UNION_REPLACE)),
             (1, _proxy(UNION_REPLACE, UNION_REPLACE + " // touched twice"))],
            _apply,
        )
        assert applied == [0, 1]
        assert "// touched twice" in composed

    def test_failed_anchor_skipped_sibling_still_applies(self):
        """One bad anchor must never void its siblings."""
        composed, applied = compose_symbol_edits(
            ORIG_FILE,
            [(0, _proxy("NOT IN FILE", "x")),
             (1, _proxy(FN_FIND, FN_REPLACE))],
            _apply,
        )
        assert applied == [1]
        assert "isEditedFile(f)" in composed

    def test_noop_edit_not_counted_as_applied(self):
        composed, applied = compose_symbol_edits(
            ORIG_FILE, [(0, _proxy(UNION_FIND, UNION_FIND))], _apply)
        assert applied == []
        assert composed == ORIG_FILE

    def test_no_edits_returns_original(self):
        composed, applied = compose_symbol_edits(ORIG_FILE, [], _apply)
        assert composed == ORIG_FILE
        assert applied == []


# ── diff_introduced_errors ────────────────────────────────────────────────

def _err(code, msg, line=1):
    return {"code": code, "message": msg, "line": line}


class TestDiffIntroducedErrors:
    def test_clean_composed_file_introduces_nothing(self):
        pre = [_err("TS2307", "Cannot find module 'react'")]
        assert diff_introduced_errors(pre, list(pre)) == []

    def test_new_error_is_introduced(self):
        pre = [_err("TS2307", "Cannot find module 'react'")]
        post = pre + [_err("TS2367", "no overlap between '\"current\"' and '\"edited\"'")]
        out = diff_introduced_errors(pre, post)
        assert len(out) == 1
        assert out[0]["code"] == "TS2367"

    def test_preexisting_noise_never_blocks(self):
        """Pre-existing isolated-file noise (TS2307 etc.) must be filtered
        even when line numbers shift after the edit."""
        pre = [_err("TS2307", "Cannot find module 'react'", line=1)]
        post = [_err("TS2307", "Cannot find module 'react'", line=5)]
        assert diff_introduced_errors(pre, post) == []

    def test_duplicate_counting(self):
        """A genuinely introduced second copy of an existing error is caught."""
        pre = [_err("TS2304", "Cannot find name 'foo'")]
        post = [_err("TS2304", "Cannot find name 'foo'"),
                _err("TS2304", "Cannot find name 'foo'")]
        assert len(diff_introduced_errors(pre, post)) == 1

    def test_error_fixed_by_edit(self):
        pre = [_err("TS2304", "Cannot find name 'foo'")]
        assert diff_introduced_errors(pre, []) == []


# ── wrong_symbol_reason ───────────────────────────────────────────────────

class TestWrongSymbolReason:
    def test_session_6930f196_corruption_rejected(self):
        """The exact corruption: type-alias fix offered as the full
        replacement for the matchesFileFilter function."""
        reason = wrong_symbol_reason(
            "matchesFileFilter",
            "export type FileFilter = 'all' | 'current' | 'new' | 'edited';",
        )
        assert reason is not None
        assert "matchesFileFilter" in reason

    def test_correct_replacement_accepted(self):
        code = (
            "export function matchesFileFilter(f, filter) {\n"
            "  if (filter === 'edited') return isEditedFile(f);\n"
            "  return true;\n}"
        )
        assert wrong_symbol_reason("matchesFileFilter", code) is None

    def test_empty_symbol_name_never_flags(self):
        assert wrong_symbol_reason("", "anything") is None
        assert wrong_symbol_reason(None, "anything") is None

    def test_empty_new_code_never_flags(self):
        # Empty corrected code is handled by earlier guards, not this one.
        assert wrong_symbol_reason("matchesFileFilter", "") is None
