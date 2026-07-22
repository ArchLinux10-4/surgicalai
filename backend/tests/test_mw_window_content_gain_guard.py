"""
Regression test for the multi-window correction content-GAIN / duplication
guard — the symmetric counterpart to test_mw_window_content_loss_guard.py.

Root cause (proven via surgical_debug_29fba21b (2).jsonl, a real Claude
agent-mode QA-correction run on Dashboard.jsx): the multi-window splice had a
guard for windows that came back TOO SHORT (_mw_window_content_loss), but
none for windows that came back TOO LONG. Window_idx=3 (window 2222-2281,
expected 60 lines) came back at 83 lines (+23, +38%) and was spliced in
unconditionally (correction_multi_window_splice_done, line_delta=23). The
very next QA retry round (retry_round=1) flagged that exact region:
"The NEW CODE is severely corrupted in the Dashboard Help modal's Tab 3/Tab
4/Footer JSX region — duplicated and scrambled content blocks, orphaned
object literals outside any array, and mismatched/dup[licated tags]".

Two new guards close this gap:
  1. _mw_window_content_duplication — detects the actual repeated-line-block
     signature QA described, independent of any length ratio.
  2. _mw_window_content_gain — coarse ratio backstop for other degenerate
     over-length windows that aren't a literal repeated block.

Every real splice_done pair from the SAME Dashboard trace (window_idx
0, 1, 2, 4, and the eventual accepted window_idx=5) must NOT be rejected by
either new guard — this fix must not regress normal, honest edits.
"""
from services.pipeline import _mw_window_content_duplication, _mw_window_content_gain


def _distinct_lines(n, prefix="code line"):
    """Non-duplicated synthetic content — no two lines are alike."""
    return [f"  {prefix} {i} unique content token_{i}" for i in range(n)]


def test_real_dashboard_duplication_bug_is_rejected():
    # Reconstruct the real bug: a 60-line legit window whose corrected
    # version re-emits (duplicates) a chunk it already wrote, landing at the
    # real observed corrected length of 83.
    legit_60 = _distinct_lines(60)
    duplicated_83 = (legit_60[:35] + legit_60[10:35] + legit_60[35:60])[:83]
    assert len(duplicated_83) == 83

    reason = _mw_window_content_duplication(duplicated_83)
    assert reason is not None, "must detect the real duplicated-block corruption"


def test_legitimate_larger_window_without_duplication_is_not_rejected():
    # Same +38% growth (60 -> 83) as the real bug, but with genuinely
    # distinct content (a legitimate larger rewrite) — must NOT be rejected
    # by either guard.
    legit_expansion = _distinct_lines(83)
    assert _mw_window_content_duplication(legit_expansion) is None
    assert _mw_window_content_gain(60, len(legit_expansion)) is False


def test_real_dashboard_legit_splices_are_not_rejected():
    # Every OTHER real splice_done pair from the same trace (window_idx
    # 0, 1, 2, 4, and the eventually-accepted window_idx 5) — all identical
    # expected/corrected line counts, all legitimate.
    real_legit = [(44, 44), (51, 51), (77, 77), (41, 41), (40, 41)]
    for expected, corrected in real_legit:
        lines = _distinct_lines(corrected)
        assert _mw_window_content_duplication(lines) is None, \
            f"false positive on legit splice expected={expected} corrected={corrected}"
        assert _mw_window_content_gain(expected, corrected) is False, \
            f"false positive on legit splice expected={expected} corrected={corrected}"


def test_trivial_bracket_repeats_are_not_flagged_as_duplication():
    # Chains of closing brackets legitimately recur in real code and must
    # not trip the duplication guard (min_repeat_char_len filter).
    lines = ["  }", "  }", "  );", "  }"] * 5 + _distinct_lines(20)
    assert _mw_window_content_duplication(lines) is None


def test_small_windows_are_exempt_from_gain_guard():
    # Same exemption pattern as the loss guard — trivial windows legitimately
    # grow without indicating a problem.
    assert _mw_window_content_gain(3, 8) is False
    assert _mw_window_content_gain(7, 20) is False


def test_gain_guard_boundary_at_exactly_75_percent_not_rejected():
    assert _mw_window_content_gain(40, 70) is False  # exactly 75% gain


def test_gain_guard_just_over_75_percent_is_rejected():
    assert _mw_window_content_gain(40, 71) is True  # 77.5% gain
