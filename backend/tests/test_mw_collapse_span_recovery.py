"""Span-based multi-window collapse recovery (session 3d9da3fd).

Evidence (surgical_debug_3d9da3fd + surgical_debug_3d9da3fd_round2):
--------------------------------------------------------------------
DashboardAIAssistant (1145-line React component) was blocked and routed to
the multi-window correction path. Two distinct failures were observed, both
caused by the SAME defect in collapse recovery:

Round 1 (score 2, LLM block — missing `useState`):
  * The surgeon referenced `researchCountry` in a changed window but its
    declaration site (a QA-ref-augmented window, `changed:0`) was never in a
    diff window. The old collapse (``build_collapsed_windows``, diff-only)
    would have spanned ONLY the changed usage — excluding the declaration —
    so the model could never add the missing `const [researchCountry, ...] =
    useState('')`. Worse, the brace-reject happened on the FINAL round, so the
    (deferred) collapse never even ran.

Round 2 (score 1, tsc+structural block — `[...)` bracket imbalance):
  * The collapse DID run (brace-reject on round 0 → round 1 available), but the
    old diff-only collapse produced a narrow 245-line window (symbol lines
    18–262) whose splice was STILL brace-unbalanced with the SAME error, then
    scheduled a context-widen for a round 2 that MAX_QA_RETRIES never allowed.

Fix under test (services.mw_collapse_recovery.build_span_collapsed_window):
  Collapse the ALREADY-AUGMENTED windows (diff windows + QA-ref windows) by
  their min/max bounds into ONE atomic span, so the single-window correction
  provably sees BOTH the changed region AND the fix site. Routes through the
  proven single-window splice path (its own brace guard still protects).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.mw_collapse_recovery import (  # noqa: E402
    build_span_collapsed_window,
    should_collapse,
    record_brace_collapse_hint,
)


def _noop_dlog(*_a, **_k):
    return None


def _events():
    """Return a dlog that captures emitted events for assertions."""
    seen = []

    def _d(evt, **kw):
        seen.append((evt, kw))

    return seen, _d


def _synthetic_component(total_lines=1145, decl_idx=35, usage_idx=404):
    """Faithful stand-in for the real DashboardAIAssistant.jsx symbol.

    * A `useState` declaration block near the top (decl_idx), matching the real
      file where researchCity/researchState are declared ~line 36 of the symbol.
    * A usage of an *undeclared* identifier far below (usage_idx ~404), matching
      `const country = researchCountry.trim();`.
    """
    lines = [f"  const _pad{i} = {i};" for i in range(total_lines)]
    lines[0] = "function DashboardAIAssistant({ apiBase = '/api/ai' }) {"
    lines[decl_idx] = "  const [researchCity, setResearchCity] = useState('');"
    lines[decl_idx + 1] = "  const [researchState, setResearchState] = useState('');"
    lines[usage_idx] = "    const country = researchCountry.trim();"
    lines[-1] = "}"
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Round-1 shape: omission bug (declaration far above the changed usage)
# ---------------------------------------------------------------------------

def test_span_includes_augmented_declaration_and_changed_usage():
    code = _synthetic_component()
    # Augmented windows as the pipeline builds them: a QA-ref declaration
    # window (changed=0) far above, plus the changed usage window (changed=3).
    windows = [
        {"window_start": 15, "window_end": 165, "changed_line_count": 0},   # decl region
        {"window_start": 344, "window_end": 437, "changed_line_count": 3},  # usage region
    ]
    seen, dlog = _events()
    res = build_span_collapsed_window(windows, code, code, 20, dlog)

    assert len(res) == 1, "collapse must yield exactly one atomic window"
    w = res[0]
    # Spans min(window_start)..max(window_end) across ALL windows.
    assert w["window_start"] == 15
    assert w["window_end"] == 437
    # The model can now see BOTH the declaration site and the usage.
    assert "const [researchCity" in w["numbered_broken"]
    assert "researchCountry.trim()" in w["numbered_broken"]
    assert w["total_clusters"] == 1
    assert any(e == "mw_span_collapse_built" for e, _ in seen)


def test_span_excludes_neither_site_even_when_only_usage_changed():
    """Regression: a diff-only collapse would span only the changed usage and
    drop the declaration. The span collapse must include the augmented decl
    window regardless of it being unchanged (changed_line_count == 0)."""
    code = _synthetic_component()
    decl_line = next(i for i, l in enumerate(code.splitlines())
                     if "const [researchCity" in l)
    usage_line = next(i for i, l in enumerate(code.splitlines())
                      if "researchCountry.trim()" in l)
    windows = [
        {"window_start": decl_line - 3, "window_end": decl_line + 3, "changed_line_count": 0},
        {"window_start": usage_line - 3, "window_end": usage_line + 3, "changed_line_count": 2},
    ]
    res = build_span_collapsed_window(windows, code, code, 20, _noop_dlog)
    w = res[0]
    assert w["window_start"] <= decl_line <= w["window_end"]
    assert w["window_start"] <= usage_line <= w["window_end"]


# ---------------------------------------------------------------------------
# Round-2 shape: 5 scattered windows across the whole symbol
# ---------------------------------------------------------------------------

def test_span_covers_whole_symbol_for_fully_scattered_windows():
    code = _synthetic_component(total_lines=1147)
    # The exact augmented windows logged in round 2 (1-indexed → 0-indexed).
    aug = [(1, 449, 80), (518, 558, 0), (791, 831, 0), (886, 1054, 0), (1087, 1147, 0)]
    windows = [
        {"window_start": ws - 1, "window_end": we - 1, "changed_line_count": ch}
        for ws, we, ch in aug
    ]
    res = build_span_collapsed_window(windows, code, code, 20, _noop_dlog)
    w = res[0]
    assert w["window_start"] == 0
    assert w["window_end"] == 1146  # last line, 0-indexed
    assert w["window_line_count"] == 1147


# ---------------------------------------------------------------------------
# Window-dict contract: must plug into the single-window splice path (Path W)
# ---------------------------------------------------------------------------

def test_collapsed_window_has_single_window_path_fields():
    code = _synthetic_component()
    windows = [
        {"window_start": 10, "window_end": 40, "changed_line_count": 5},
        {"window_start": 200, "window_end": 260, "changed_line_count": 0},
    ]
    w = build_span_collapsed_window(windows, code, code, 20, _noop_dlog)[0]
    for field in (
        "window_start", "window_end", "numbered_broken", "numbered_original",
        "window_line_count", "total_orig_lines", "total_edit_lines",
        "changed_line_count", "cluster_index", "total_clusters",
    ):
        assert field in w, f"missing field required by Path W: {field}"
    # numbered_broken line count must equal window_line_count (splice arithmetic)
    assert len(w["numbered_broken"].splitlines()) == w["window_line_count"]
    assert w["window_line_count"] == w["window_end"] - w["window_start"] + 1


# ---------------------------------------------------------------------------
# Safe degradation
# ---------------------------------------------------------------------------

def test_span_empty_windows_degrades_to_empty():
    assert build_span_collapsed_window([], "a\nb", "a\nb", 20, _noop_dlog) == []


def test_span_clamps_out_of_range_end_to_broken_length():
    code = "l0\nl1\nl2\nl3\nl4"
    windows = [{"window_start": 1, "window_end": 999, "changed_line_count": 1}]
    w = build_span_collapsed_window(windows, code, code, 20, _noop_dlog)[0]
    assert w["window_end"] == 4  # clamped to len-1
    assert w["window_start"] == 1


def test_span_never_raises_on_garbage():
    # Missing keys / wrong types must degrade to [] rather than raise.
    assert build_span_collapsed_window(
        [{"nope": 1}], "a\nb", "a\nb", 20, _noop_dlog
    ) == []


# ---------------------------------------------------------------------------
# Hint lifecycle (reachability precondition)
# ---------------------------------------------------------------------------

def test_collapse_hint_only_on_brace_imbalance():
    hint = {}
    assert record_brace_collapse_hint(
        hint, 2, is_brace_unbalanced=False, dlog=_noop_dlog) is False
    assert should_collapse(2, hint) is False
    assert record_brace_collapse_hint(
        hint, 2, is_brace_unbalanced=True, dlog=_noop_dlog) is True
    assert should_collapse(2, hint) is True
