"""Line-splice content-loss guard + collapse bonus reachability (3d9da3fd_round3).

Evidence:
  * plan_chain_update line_splice: edit_start=1693, edit_end=1822 (130-line
    span) with ~8-line new_code → file 123665→119144 chars, symbol gutted.
  * Correct old_code (useState block) was present but never used because
    line fields always won.
  * mw_collapse_hint_recorded on round 1, then qa_retry_no_fixes_breaking
    exited before the bonus span-collapse round.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import (  # noqa: E402
    _apply_snippet_by_lines,
    _line_splice_would_lose_content,
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
