"""
Regression test for the GPT multi-window correction content-loss guard.

Root cause (proven via surgical_debug_8773a043.jsonl, a real GPT agent-mode
run on PublicHome.jsx): the multi-window QA-correction splice accepted ANY
window GPT returned with no check on line-count loss. Round 1, window idx 3
asked GPT for a corrected 41-line window; GPT returned only 4 lines, and the
splice accepted it unconditionally (correction_multi_window_splice_done,
line_delta=-37), gutting JSX that cascaded into unrecoverable syntax errors.

The same trace also contains 6 LEGITIMATE splices (identical, +1/-1 shrink,
and grown windows) that must NOT be rejected by the new guard — a fix here
must not regress normal, honest edits.

All (expected, corrected) pairs below are taken byte-for-byte from that
trace's correction_multi_window_splice_done events across both rounds.
"""
from services.pipeline import _mw_window_content_loss


def test_real_gpt_truncation_bug_is_rejected():
    # Round 1, window idx 3: expected 41 lines, GPT returned 4. THE bug.
    assert _mw_window_content_loss(41, 4) is True


def test_real_legitimate_splices_are_not_rejected():
    # Every other real splice_done pair from the same two-round trace.
    legit_cases = [
        (41, 41),   # round1 win4 - identical
        (41, 41),   # round1 win2 - identical
        (39, 38),   # round1 win0 - minor 1-line shrink
        (44, 48),   # round2 win2 - grew
        (41, 42),   # round2 win1 - grew
        (114, 115),  # round2 win0 - grew
    ]
    for expected, corrected in legit_cases:
        assert _mw_window_content_loss(expected, corrected) is False, \
            f"false positive on legit splice expected={expected} corrected={corrected}"


def test_small_windows_are_exempt_even_with_high_loss_ratio():
    # A 3-line window shrinking to 1 line is a >60% "loss" but is normal for
    # tiny edits — must not be rejected (min_window_size guard).
    assert _mw_window_content_loss(3, 1) is False
    assert _mw_window_content_loss(7, 1) is False


def test_zero_or_negative_expected_never_rejected():
    assert _mw_window_content_loss(0, 5) is False
    assert _mw_window_content_loss(-1, 5) is False


def test_boundary_at_exactly_40_percent_loss_not_rejected():
    # 10 -> 6 is exactly a 40% loss; guard is "> max_loss_ratio", so this
    # boundary case must be ACCEPTED, not rejected.
    assert _mw_window_content_loss(10, 6) is False


def test_boundary_just_over_40_percent_loss_is_rejected():
    # 10 -> 5 is a 50% loss; must be rejected.
    assert _mw_window_content_loss(10, 5) is True
