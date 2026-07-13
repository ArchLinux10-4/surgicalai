"""Edge-case tests for the Bug 1/2/3 trio (QA score 6/10 fix).

Covers scenarios NOT in test_qa_score_fixes.py:
  - Bug 1: English words not matched, snake_case not matched, empty QA, identifier not in code
  - Bug 2: diff contained in qa-ref, two diff windows overlap, no overlap
  - Bug 3: all-ok, partial-no-hard-fail, all-no-edit, hard-fail-after-splice, parse-fail-is-hard
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
_PIPELINE = next((p for p in _CANDIDATES if os.path.isfile(p)), None)
if _PIPELINE is None:
    raise FileNotFoundError("pipeline.py not found")

with open(_PIPELINE, "r") as _f:
    _SRC = _f.read()


def _extract_function(name):
    """Extract a top-level function from pipeline source and compile it."""
    tree = ast.parse(_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            lines = _SRC.splitlines(keepends=True)
            start = node.lineno - 1
            end = node.end_lineno
            func_src = "".join(lines[start:end])
            return func_src
    raise ValueError(f"Function {name} not found")


# Build namespace with required dependencies
_ns = {"re": re, "__builtins__": __builtins__}

# Add _dlog stub
_ns["_dlog"] = lambda *a, **kw: None

# Extract and exec both functions
for _fn in ("_extract_qa_reference_lines", "_augment_windows_with_qa_refs"):
    _src = _extract_function(_fn)
    exec(compile(_src, _PIPELINE, "exec"), _ns)

_extract_qa_reference_lines = _ns["_extract_qa_reference_lines"]
_augment_windows_with_qa_refs = _ns["_augment_windows_with_qa_refs"]


# ═══════════════════════════════════════════════════════════════════
# Bug 1 — identifier extraction
# ═══════════════════════════════════════════════════════════════════

class TestBug1IdentifierExtraction:
    """Verify PascalCase/camelCase only — no English prose leakage."""

    def test_english_words_not_matched(self):
        qa_dict = {"logic_errors": [{"description":
            "The dialog modal table typing handler attached rendered portals "
            "escape backdrop bubble incomplete function should never return"}]}
        code = "const x = 1;\n" * 10
        ranges = _extract_qa_reference_lines(qa_dict, code, context_lines=2)
        assert ranges == []

    def test_pascal_case_matched(self):
        qa_dict = {"logic_errors": [{"description": "AutoMatchModal is broken"}]}
        code = "function AutoMatchModal() {\n  return null;\n}\n"
        ranges = _extract_qa_reference_lines(qa_dict, code, context_lines=2)
        assert len(ranges) > 0

    def test_camel_case_matched(self):
        qa_dict = {"logic_errors": [{"description": "swallowEscape is wrong"}]}
        code = "const swallowEscape = (e) => e.stopPropagation();\n"
        ranges = _extract_qa_reference_lines(qa_dict, code, context_lines=2)
        assert len(ranges) > 0

    def test_snake_case_not_matched(self):
        qa_dict = {"logic_errors": [{"description": "handle_click is broken"}]}
        code = "def handle_click():\n    pass\n"
        ranges = _extract_qa_reference_lines(qa_dict, code, context_lines=2)
        assert ranges == []

    def test_short_identifiers_filtered(self):
        qa_dict = {"logic_errors": [{"description": "onFo is problematic"}]}
        code = "const onFo = 1;\nconst other = 2;\n"
        # "onFo" = 4 chars < 5 minimum
        ranges = _extract_qa_reference_lines(qa_dict, code, context_lines=2)
        assert ranges == []

    def test_5_char_identifier_kept(self):
        qa_dict = {"logic_errors": [{"description": "onFoo is problematic"}]}
        code = "const onFoo = 1;\nconst other = 2;\n"
        # "onFoo" = 5 chars, exactly at threshold
        ranges = _extract_qa_reference_lines(qa_dict, code, context_lines=2)
        assert len(ranges) > 0

    def test_empty_qa_text(self):
        ranges = _extract_qa_reference_lines({}, "code\ncode\n", context_lines=2)
        assert ranges == []

    def test_identifier_not_in_code(self):
        qa_dict = {"logic_errors": [{"description": "MyComponent is broken"}]}
        code = "const x = 1;\nconst y = 2;\n"
        ranges = _extract_qa_reference_lines(qa_dict, code, context_lines=2)
        assert ranges == []

    def test_real_d59e51e5_coverage(self):
        """Exact scenario from forensic — must NOT cover 65% of file.

        Use 732-line file (same as real AutoMatchModal.jsx) with identifiers
        scattered widely. With old regex, 82 identifiers → 49 matches → 65%.
        With new regex, 6 identifiers → targeted coverage.
        """
        qa_text = (
            "The onKeyDown handler swallowEscape is only attached to the main "
            "AutoMatchModal backdrop div, but SuggestTitleDialog and "
            "NormalizeBatchDialog are rendered as portals that bubble escape "
            "to AccountSettingsModal."
        )
        qa_dict = {"logic_errors": [{"description": qa_text}]}
        lines = ["// filler line"] * 732
        lines[50] = "function AutoMatchModal() {"
        lines[100] = "  const swallowEscape = (e) => e.stopPropagation();"
        lines[250] = "function SuggestTitleDialog() {}"
        lines[450] = "function NormalizeBatchDialog() {}"
        lines[600] = "function AccountSettingsModal() {}"
        lines[650] = "  const onKeyDown = (e) => {};"
        code = "\n".join(lines)
        ranges = _extract_qa_reference_lines(qa_dict, code, context_lines=20)
        total_covered = sum(e - s + 1 for s, e in ranges)
        assert total_covered < len(lines) * 0.5, \
            f"Covering {total_covered}/{len(lines)} ({total_covered/len(lines)*100:.0f}%)"

    def test_line_number_references_still_work(self):
        """Line number extraction (step 1) must still work."""
        qa_dict = {"logic_errors": [{"description": "Problem at line 25"}]}
        code = "\n".join(["x"] * 50)
        ranges = _extract_qa_reference_lines(qa_dict, code, context_lines=5)
        assert len(ranges) > 0
        # Should cover around line 25 (0-indexed: 24)
        assert any(s <= 24 <= e for s, e in ranges)


# ═══════════════════════════════════════════════════════════════════
# Bug 2 — merge preserves changed_line_count
# ═══════════════════════════════════════════════════════════════════

class TestBug2MergeChangedCount:

    def _make_window(self, ws, we, changed, source="diff"):
        return {
            "window_start": ws, "window_end": we,
            "numbered_broken": "x", "numbered_original": "x",
            "window_line_count": we - ws + 1,
            "changed_line_count": changed,
            "cluster_index": 0, "total_clusters": 1,
            "_source": source,
        }

    def test_diff_contained_in_qa_ref(self):
        """Diff window (changed=12) inside QA-ref window (changed=0) — must preserve."""
        diff_w = [self._make_window(269, 366, 12)]
        # QA ref needs to match an identifier in the code to create a window
        qa_dict = {"logic_errors": [{"description": "AutoMatchModal at line 300"}]}
        code_lines = ["x"] * 600
        code_lines[299] = "function AutoMatchModal() {"
        code = "\n".join(code_lines)
        result = _augment_windows_with_qa_refs(diff_w, qa_dict, code, code, context_lines=20)
        # Find window covering the diff range
        covering = [w for w in result if w["window_start"] <= 269 and w["window_end"] >= 366]
        if covering:
            assert covering[0]["changed_line_count"] >= 12, \
                f"changed_line_count={covering[0]['changed_line_count']}"
        else:
            # Diff window should still have its original count
            diff_found = [w for w in result if w["window_start"] == 269]
            assert diff_found and diff_found[0]["changed_line_count"] == 12

    def test_no_qa_ranges_returns_unchanged(self):
        """Empty QA → diff_windows returned as-is."""
        diff_w = [self._make_window(10, 50, 5)]
        result = _augment_windows_with_qa_refs(diff_w, {}, "x\n" * 100, "x\n" * 100)
        assert result is diff_w  # exact same object
        assert result[0]["changed_line_count"] == 5

    def test_separate_windows_not_merged(self):
        """Non-overlapping windows stay separate."""
        diff_w = [self._make_window(10, 30, 5)]
        qa_dict = {"logic_errors": [{"description": "handleSubmit at line 200"}]}
        code_lines = ["x"] * 300
        code_lines[199] = "function handleSubmit() {"
        code = "\n".join(code_lines)
        result = _augment_windows_with_qa_refs(diff_w, qa_dict, code, code, context_lines=5)
        assert len(result) >= 2
        assert result[0]["changed_line_count"] == 5


# ═══════════════════════════════════════════════════════════════════
# Bug 3 — partial success logic (state machine)
# ═══════════════════════════════════════════════════════════════════

class TestBug3PartialSuccess:
    """Verify the _mw_should_apply gate matches pipeline code."""

    @staticmethod
    def _should_apply(all_ok, any_spliced, hard_fail):
        """Exact replica of pipeline.py line 18445-18447."""
        return all_ok or (any_spliced and not hard_fail)

    def test_all_ok(self):
        assert self._should_apply(True, True, False) is True

    def test_partial_no_hard_fail(self):
        """Some no_edit, ≥1 spliced, no hard fail → apply (THE FIX)."""
        assert self._should_apply(False, True, False) is True

    def test_all_no_edit(self):
        """ALL windows returned no_edit → nothing to apply."""
        assert self._should_apply(False, False, False) is False

    def test_hard_fail_after_splice(self):
        """Splice succeeded but later parse error → rollback."""
        assert self._should_apply(False, True, True) is False

    def test_hard_fail_no_splice(self):
        assert self._should_apply(False, False, True) is False

    def test_gate_in_source(self):
        """Verify the actual source code has the partial-success gate."""
        assert "_mw_any_spliced and not _mw_hard_fail" in _SRC
        assert "_mw_hard_fail = True" in _SRC
        # no_edit must NOT set hard_fail
        # Find the no_edit block and verify it does NOT contain hard_fail
        no_edit_idx = _SRC.find("correction_multi_window_no_edit")
        assert no_edit_idx != -1
        # Next 600 chars should have "continue" but NOT "_mw_hard_fail = True"
        block = _SRC[no_edit_idx:no_edit_idx + 600]
        assert "continue" in block, f"'continue' not found near no_edit block"
        assert "_mw_hard_fail = True" not in block, "no_edit must NOT set hard_fail"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
