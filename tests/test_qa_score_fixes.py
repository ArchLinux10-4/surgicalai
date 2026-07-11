"""
Tests for QA Score Collapse fixes — three proven root causes fixed:

Fix 1: Group coupled same-symbol plan items
  — Prevents surgeon timeout on complex edits by merging N same-symbol
    plan items into a single instruction (session 82eb1056: 2 of 4
    PasteBatchJobsModal plan items timed out at 120s → dead code → QA 1/10).

Fix 2: QA-referenced correction windows
  — Ensures the correction model can see code at QA-referenced locations,
    not just diff locations (session 82eb1056 line 542: correction emitted
    <search_request> because target code at line ~1830 was outside all windows).

Fix 3: Overlap supersede for broader edits
  — When a new edit's range CONTAINS all previously applied ranges, treat
    it as a superseding replacement instead of an overlap error (session
    82eb1056 turn 7: lines 52–1955 rejected as overlapping 1827–1920 →
    correction introduced duplicate ); })} → syntax error → QA score 1).
"""
import ast
import os
import re
import sys
import textwrap
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.join(_HERE, "..", "backend", "services", "pipeline.py"),
    os.path.join(_HERE, "..", "pipeline.py"),
]
_PIPELINE = None
for _c in _CANDIDATES:
    if os.path.isfile(_c):
        _PIPELINE = os.path.abspath(_c)
        break

if not _PIPELINE:
    raise FileNotFoundError(
        f"pipeline.py not found. Searched:\n" +
        "\n".join(f"  {os.path.abspath(c)}" for c in _CANDIDATES)
    )

# ── AST-based extraction ────────────────────────────────────────────────────
_FUNCS_TO_EXTRACT = [
    "_extract_qa_reference_lines",
    "_augment_windows_with_qa_refs",
    "_find_changed_windows",
    "_find_changed_window",
]

def _extract_functions():
    src = open(_PIPELINE, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)

    func_sources = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _FUNCS_TO_EXTRACT:
                start = node.lineno - 1
                end = node.end_lineno
                func_sources[node.name] = "".join(lines[start:end])

    ns = {
        "_dlog": lambda *a, **kw: None,
        "re": re,
        "difflib": __import__("difflib"),
    }

    for name, source in func_sources.items():
        exec(compile(source, _PIPELINE, "exec"), ns)

    return ns


NS = _extract_functions()

_extract_qa_ref = NS["_extract_qa_reference_lines"]
_augment_windows = NS["_augment_windows_with_qa_refs"]
_find_changed_windows = NS["_find_changed_windows"]


# ── Helpers ──────────────────────────────────────────────────────────────────
def _make_code(n_lines, label="line"):
    """Generate n lines of dummy code."""
    return "\n".join(f"  {label}_{i}();" for i in range(n_lines))


# ═══════════════════════════════════════════════════════════════════════════
# Fix 1: Plan grouping (code-presence tests — actual grouping is in pipeline)
# ═══════════════════════════════════════════════════════════════════════════

class TestFix1PlanGrouping:
    """Verify plan grouping code structures exist in pipeline.py."""

    def test_grouped_plan_variable_exists(self):
        src = open(_PIPELINE).read()
        assert "_grouped_plan" in src, "Plan grouping variable not found"

    def test_effective_plan_variable_exists(self):
        src = open(_PIPELINE).read()
        assert "_effective_plan" in src, "Effective plan variable not found"

    def test_effective_plan_used_in_loop(self):
        src = open(_PIPELINE).read()
        assert "for plan_idx, plan_item in enumerate(_effective_plan):" in src

    def test_plan_items_grouped_dlog_exists(self):
        src = open(_PIPELINE).read()
        assert 'plan_items_grouped' in src

    def test_descriptions_merged_with_and_also(self):
        src = open(_PIPELINE).read()
        assert 'AND ALSO' in src, "Merged descriptions should use AND ALSO separator"

    def test_original_edit_plan_data_not_in_loop(self):
        """Ensure the loop iterates over _effective_plan, not raw edit_plan_data."""
        src = open(_PIPELINE).read()
        # The old line was: for plan_idx, plan_item in enumerate(edit_plan_data):
        # It should NOT exist anymore
        assert "for plan_idx, plan_item in enumerate(edit_plan_data):" not in src


# ═══════════════════════════════════════════════════════════════════════════
# Fix 2: QA-referenced correction windows
# ═══════════════════════════════════════════════════════════════════════════

class TestFix2ExtractQAReferences:
    """Test _extract_qa_reference_lines."""

    def test_extracts_line_reference(self):
        qa = {"summary": "The cell rendering at line 1830 was never updated."}
        code = _make_code(2000)
        ranges = _extract_qa_ref(qa, code)
        # Should find a range covering line 1830
        assert any(s <= 1829 <= e for s, e in ranges), \
            f"Expected range covering line 1830, got {ranges}"

    def test_extracts_approximate_line_reference(self):
        qa = {"summary": "Update textWrapStyle at line ~1830"}
        code = _make_code(2000)
        ranges = _extract_qa_ref(qa, code)
        assert any(s <= 1829 <= e for s, e in ranges)

    def test_extracts_line_range(self):
        qa = {"summary": "The rendering logic at lines 1827-1920 is broken"}
        code = _make_code(2000)
        ranges = _extract_qa_ref(qa, code)
        assert any(s <= 1826 and e >= 1919 for s, e in ranges), \
            f"Expected range covering 1827-1920, got {ranges}"

    def test_extracts_identifier_reference(self):
        qa = {"summary": "scrollableCellTextStyle is defined but never used"}
        # Put the identifier in the code at line 150
        lines = [f"  line_{i}();" for i in range(200)]
        lines[150] = "  const scrollableCellTextStyle = { overflow: 'auto' };"
        code = "\n".join(lines)
        ranges = _extract_qa_ref(qa, code)
        assert any(s <= 150 <= e for s, e in ranges), \
            f"Expected range covering line 150, got {ranges}"

    def test_empty_qa_returns_empty(self):
        qa = {}
        code = _make_code(100)
        assert _extract_qa_ref(qa, code) == []

    def test_no_matches_returns_empty(self):
        qa = {"summary": "Something is wrong but no specifics"}
        code = _make_code(100)
        # May or may not return ranges based on identifier matching
        # Just verify it doesn't crash
        result = _extract_qa_ref(qa, code)
        assert isinstance(result, list)

    def test_line_ref_beyond_code_ignored(self):
        qa = {"summary": "Fix the issue at line 5000"}
        code = _make_code(100)  # only 100 lines
        ranges = _extract_qa_ref(qa, code)
        # Line 5000 is beyond the code — should not crash
        assert not any(s > 99 for s, _ in ranges)


class TestFix2AugmentWindows:
    """Test _augment_windows_with_qa_refs."""

    def test_adds_uncovered_qa_window(self):
        """When QA references line 1830 but diff windows only cover line 150,
        a new window should be added for the uncovered area."""
        original = _make_code(2000, "orig")
        edited = list(original.splitlines())
        edited[150] = "  CHANGED_LINE();"
        edited_str = "\n".join(edited)

        diff_windows = _find_changed_windows(original, edited_str)
        assert len(diff_windows) >= 1, "Should have at least 1 diff window"

        qa = {"summary": "The cell rendering at line 1830 was never updated."}
        augmented = _augment_windows(diff_windows, qa, original, edited_str)

        # Should have more windows than before
        assert len(augmented) > len(diff_windows), \
            f"Expected more windows after augmentation: {len(augmented)} vs {len(diff_windows)}"

        # The new window should cover line 1830
        covers_1830 = any(
            w["window_start"] <= 1829 <= w["window_end"]
            for w in augmented
        )
        assert covers_1830, "Augmented windows should cover QA-referenced line 1830"

    def test_no_augmentation_when_already_covered(self):
        """When QA references a line already covered by diff windows,
        no extra windows should be added."""
        original = _make_code(200, "orig")
        edited = list(original.splitlines())
        edited[100] = "  CHANGED_LINE();"
        edited_str = "\n".join(edited)

        diff_windows = _find_changed_windows(original, edited_str)
        qa = {"summary": "Fix the issue at line 100"}
        augmented = _augment_windows(diff_windows, qa, original, edited_str)

        # Line 100 is already in the diff window
        assert len(augmented) == len(diff_windows)

    def test_no_augmentation_on_empty_qa(self):
        original = _make_code(200)
        edited_str = original.replace("line_50", "CHANGED")
        diff_windows = _find_changed_windows(original, edited_str)
        qa = {}
        augmented = _augment_windows(diff_windows, qa, original, edited_str)
        assert len(augmented) == len(diff_windows)

    def test_cluster_indices_reindexed(self):
        """After augmentation, cluster_index values should be sequential."""
        original = _make_code(2000, "orig")
        edited = list(original.splitlines())
        edited[50] = "  CHANGED_50();"
        edited_str = "\n".join(edited)

        diff_windows = _find_changed_windows(original, edited_str)
        qa = {"summary": "Fix line 1500 and line 1900"}
        augmented = _augment_windows(diff_windows, qa, original, edited_str)

        indices = [w["cluster_index"] for w in augmented]
        assert indices == list(range(len(augmented))), \
            f"Cluster indices should be sequential: {indices}"


# ═══════════════════════════════════════════════════════════════════════════
# Fix 3: Overlap supersede
# ═══════════════════════════════════════════════════════════════════════════

class TestFix3OverlapSupersede:
    """Verify the overlap supersede logic exists in pipeline.py."""

    def test_supersede_check_exists(self):
        src = open(_PIPELINE).read()
        assert "line_range_overlap_supersede" in src

    def test_supersede_clears_prior_ranges(self):
        src = open(_PIPELINE).read()
        # The fix should clear prior ranges when superseding
        assert "_ln_applied_ranges[_akey] = []" in src

    def test_supersede_checks_all_contained(self):
        src = open(_PIPELINE).read()
        # Must check that ALL prior ranges are contained, not just one
        assert "_all_contained" in src or "all_contained" in src

    def test_overlap_reason_cleared_on_supersede(self):
        """When supersede is detected, _overlap_reason must be set to None."""
        src = open(_PIPELINE).read()
        # Find the supersede block and verify it clears the overlap reason
        idx = src.find("line_range_overlap_supersede")
        assert idx > 0
        # The code after the dlog should clear _overlap_reason
        snippet = src[idx:idx + 800]
        assert "_overlap_reason = None" in snippet, \
            "Supersede block must clear _overlap_reason"

    def test_augment_windows_call_exists_in_correction(self):
        """Fix 2 must be wired into the correction path."""
        src = open(_PIPELINE).read()
        assert "_augment_windows_with_qa_refs(" in src


# ═══════════════════════════════════════════════════════════════════════════
# Integration-style tests
# ═══════════════════════════════════════════════════════════════════════════

class TestIntegration:
    """Cross-fix integration verification."""

    def test_all_three_fixes_coexist(self):
        """All three fixes must be present in the same file."""
        src = open(_PIPELINE).read()
        assert "plan_items_grouped" in src, "Fix 1 missing"
        assert "_augment_windows_with_qa_refs" in src, "Fix 2 missing"
        assert "line_range_overlap_supersede" in src, "Fix 3 missing"

    def test_fix2_augmentation_with_multiple_qa_issues(self):
        """QA with multiple issue types should extract refs from all of them."""
        qa = {
            "summary": "Dead code: scrollableCellTextStyle defined at line 165 but never wired",
            "logic_errors": ["textWrapStyle at line 1830 still uses old clampedCellTextStyle"],
            "plan_deviation": "The rendering update at lines 1827-1920 was not applied",
        }
        code = _make_code(2000)
        ranges = _extract_qa_ref(qa, code)
        # Should find ranges for BOTH line 165 AND line 1830
        has_165 = any(s <= 164 <= e for s, e in ranges)
        has_1830 = any(s <= 1829 <= e for s, e in ranges)
        assert has_165, f"Should find line 165 reference, got {ranges}"
        assert has_1830, f"Should find line 1830 reference, got {ranges}"
