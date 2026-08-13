"""Line-splice content-loss guard + collapse bonus reachability (3d9da3fd_round3).

Evidence:
  * plan_chain_update line_splice: edit_start=1693, edit_end=1822 (130-line
    span) with ~8-line new_code → file 123665→119144 chars, symbol gutted.
  * Correct old_code (useState block) was present but never used because
    line fields always won.
  * mw_collapse_hint_recorded on round 1, then qa_retry_no_fixes_breaking
    exited before the bonus span-collapse round.

Round9 exception (surgical_debug_3d9da3fd_round9.jsonl):
  * Opus deleted stale duplicate isWithinResearchRateLimit at L2029–2046
    (empty / comment-only new_code). Guard rejected 3× as content loss.
  * Bounded intentional deletes must be allowed; round3 gutting must not.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import (  # noqa: E402
    INTENTIONAL_DELETE_MAX_SPAN,
    _apply_snippet_by_lines,
    _is_intentional_line_deletion,
    _line_splice_would_lose_content,
    _new_code_is_delete_residue,
    _plan_chain_splice_method,
    should_defer_no_fixes_for_collapse,
)


def test_round3_shape_130_to_8_is_content_loss():
    assert _line_splice_would_lose_content(130, "\n".join(f"  line {i}" for i in range(8)))


def test_legitimate_small_shrink_allowed():
    # 20 → 15 = 25% loss, under 40% threshold
    assert not _line_splice_would_lose_content(20, "\n".join(f"x{i}" for i in range(15)))


def test_small_span_exempt():
    # < min_window_size=8: ratio math is noisy; allow
    assert not _line_splice_would_lose_content(5, "a\nb")


def test_apply_snippet_by_lines_rejects_130_to_8():
    # Symbol with 200 lines; splice absolute lines 10–139 (130 lines) with 8 lines
    sym_start = 1
    lines = [f"L{i}\n" for i in range(1, 201)]
    symbol = "".join(lines)
    new_code = "\n".join(f"N{i}" for i in range(8))
    full, ok, reason = _apply_snippet_by_lines(
        symbol, sym_start, 10, 139, new_code
    )
    assert ok is False
    assert full is None
    assert "line_splice_content_loss" in reason


def test_apply_snippet_by_lines_allows_compatible_span():
    sym_start = 100
    lines = [f"L{i}\n" for i in range(100, 130)]
    symbol = "".join(lines)
    # replace 5 lines with 5 lines
    new_code = "\n".join(f"N{i}" for i in range(5))
    full, ok, reason = _apply_snippet_by_lines(
        symbol, sym_start, 105, 109, new_code
    )
    assert ok is True
    assert "line-number-splice:105-109" in reason
    assert "N0" in full
    assert "L104\n" in full  # line before span preserved
    assert "L110\n" in full  # line after span preserved


def test_plan_chain_prefers_old_code_fallback_on_loss():
    old = (
        "  const [researchStep, setResearchStep] = useState(1);\n"
        "  const [researchCity, setResearchCity] = useState('');\n"
    )
    new = (
        "  const [researchStep, setResearchStep] = useState(1);\n"
        "  const [researchCity, setResearchCity] = useState('');\n"
        "  const [researchCountry, setResearchCountry] = useState('');\n"
    )
    file_content = "header\n" + old + "footer\n"
    method = _plan_chain_splice_method(130, new, old, file_content)
    assert method == "old_code_fallback"


def test_plan_chain_refuses_when_no_old_code():
    method = _plan_chain_splice_method(130, "a\nb\nc\nd\ne\nf\ng\nh", None, "file")
    assert method == "refuse"


def test_plan_chain_line_splice_when_size_ok():
    new = "\n".join(f"x{i}" for i in range(18))
    method = _plan_chain_splice_method(20, new, "unused", "unused")
    assert method == "line_splice"


def test_defer_collapse_on_final_normal_round_with_hint():
    assert should_defer_no_fixes_for_collapse(
        {3: True}, bonus_used=False, retry_round=1, max_retries=2
    ) is True


def test_no_defer_without_hint():
    assert should_defer_no_fixes_for_collapse(
        {}, bonus_used=False, retry_round=1, max_retries=2
    ) is False


def test_no_defer_when_bonus_already_used():
    assert should_defer_no_fixes_for_collapse(
        {3: True}, bonus_used=True, retry_round=1, max_retries=2
    ) is False


def test_no_defer_on_earlier_round():
    # Round 0 still has a normal retry left — the continue path handles it
    assert should_defer_no_fixes_for_collapse(
        {3: True}, bonus_used=False, retry_round=0, max_retries=2
    ) is False


# ── Round9: intentional mid-file deletion ───────────────────────────────────

def test_round9_empty_delete_is_intentional():
    assert _new_code_is_delete_residue("")
    assert _is_intentional_line_deletion(18, "")
    assert not _line_splice_would_lose_content(18, "")


def test_round9_comment_residue_is_intentional():
    residue = "\n// ─── JD title extractor ───────────────────────────────────"
    assert _new_code_is_delete_residue(residue)
    assert _is_intentional_line_deletion(19, residue)
    assert not _line_splice_would_lose_content(19, residue)


def test_round9_brace_plus_comment_residue_is_intentional():
    residue = "}\n\n// ─── JD title extractor ───────────────────────────────────"
    assert _new_code_is_delete_residue(residue)
    assert _is_intentional_line_deletion(20, residue)


def test_round3_real_code_shrink_still_not_intentional_delete():
    """130→8 with real statements must remain content-loss (not a delete)."""
    new = "\n".join(f"  const [x{i}, setX{i}] = useState(0);" for i in range(8))
    assert not _new_code_is_delete_residue(new)
    assert not _is_intentional_line_deletion(130, new)
    assert _line_splice_would_lose_content(130, new)


def test_huge_empty_span_still_refused():
    """Empty wipe of >INTENTIONAL_DELETE_MAX_SPAN lines still content-loss."""
    big = INTENTIONAL_DELETE_MAX_SPAN + 20
    assert not _is_intentional_line_deletion(big, "")
    assert _line_splice_would_lose_content(big, "")
    assert _plan_chain_splice_method(big, "", None, "file") == "refuse"


def test_apply_snippet_allows_round9_empty_delete():
    # Whole-file gap_bridge window; delete absolute lines 50–67 (18 lines)
    lines = [f"L{i}\n" for i in range(1, 101)]
    for i in range(49, 67):
        lines[i] = f"DUP{i+1}\n"
    symbol = "".join(lines)
    full, ok, reason = _apply_snippet_by_lines(symbol, 1, 50, 67, "")
    assert ok is True, reason
    assert "DUP50" not in full
    assert "DUP67" not in full
    assert "L49\n" in full
    assert "L68\n" in full


def test_apply_snippet_allows_round9_comment_residue_delete():
    lines = [f"L{i}\n" for i in range(1, 101)]
    for i in range(49, 67):
        lines[i] = f"DUP{i+1}\n"
    lines[67] = "// ─── JD title extractor ───\n"
    symbol = "".join(lines)
    residue = "\n// ─── JD title extractor ───"
    full, ok, reason = _apply_snippet_by_lines(symbol, 1, 50, 68, residue)
    assert ok is True, reason
    assert "DUP50" not in full
    assert "JD title extractor" in full


def test_plan_chain_allows_bounded_intentional_delete():
    method = _plan_chain_splice_method(18, "", None, "file")
    assert method == "line_splice"


def test_round9_real_dashboard_assistant_delete():
    """Desktop/New file: remove stale duplicate; keep canonical + JD header."""
    path = "/Users/pm/Desktop/New/dashboardAssistant.js"
    if not os.path.isfile(path):
        return  # skip if artifact absent in CI
    content = open(path).read()
    assert content.count("function isWithinResearchRateLimit") == 2
    full, ok, reason = _apply_snippet_by_lines(content, 1, 2029, 2046, "")
    assert ok is True, reason
    assert full.count("function isWithinResearchRateLimit") == 1
    assert "RESEARCH.rateLimit" not in full
    assert "RESEARCH.hourlyLimit" in full
    assert "JD title extractor" in full
