"""Path L fallthrough after Path W fragment-reject (session 3d9da3fd_round6).

Evidence:
--------
Full-symbol collapse snapped the window to lines 1–1147 and asked Sonnet to
re-emit the complete symbol. Sonnet correctly returned a surgical fix:

    edit_data_keys = [filename, symbol, edit_start_line, edit_end_line, new_code]
    new_code_len   = 238   # 6 lines — the missing useCallback dep

Path W treated the 6-line payload as a full-window replacement → fragment
rejected. Path L (edit_start/end_line splice) was gated behind `not _winfo`,
so the valid surgical heal was discarded and the symbol stayed hard-blocked.

Fix under test:
  * should_try_path_l_linesplice — allow Path L when Path W was attempted
    but did not accept
  * _apply_correction_line_splice — try symbol-relative numbering first when
    falling through from a windowed prompt (window prompts number 1..N inside
    the symbol), with absolute-file as the second attempt
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import (  # noqa: E402
    should_try_path_l_linesplice,
    _apply_correction_line_splice,
    _apply_snippet_by_lines,
)


# ---------------------------------------------------------------------------
# Gate predicate
# ---------------------------------------------------------------------------

def test_path_l_runs_without_window():
    assert should_try_path_l_linesplice(
        accepted=None, has_new_code=True, has_old_code=False,
        has_edit_lines=True, has_winfo=False, windowed_attempted=False,
    ) is True


def test_path_l_blocked_when_window_owns_and_not_yet_attempted():
    # Window is set up but Path W hasn't run — Path L must not steal the edit.
    # (In practice Path W always runs first when _winfo + new_code + !old_code;
    # this guards the predicate contract.)
    assert should_try_path_l_linesplice(
        accepted=None, has_new_code=True, has_old_code=False,
        has_edit_lines=True, has_winfo=True, windowed_attempted=False,
    ) is False


def test_path_l_fallthrough_after_windowed_reject():
    """The round6 smoking gun: Path W attempted + rejected → Path L may run."""
    assert should_try_path_l_linesplice(
        accepted=None, has_new_code=True, has_old_code=False,
        has_edit_lines=True, has_winfo=True, windowed_attempted=True,
    ) is True


def test_path_l_skipped_when_already_accepted():
    assert should_try_path_l_linesplice(
        accepted="already spliced", has_new_code=True, has_old_code=False,
        has_edit_lines=True, has_winfo=True, windowed_attempted=True,
    ) is False


def test_path_l_skipped_without_edit_lines():
    assert should_try_path_l_linesplice(
        accepted=None, has_new_code=True, has_old_code=False,
        has_edit_lines=False, has_winfo=True, windowed_attempted=True,
    ) is False


def test_path_l_skipped_when_old_code_present():
    # old_code/new_code belongs to Path 2
    assert should_try_path_l_linesplice(
        accepted=None, has_new_code=True, has_old_code=True,
        has_edit_lines=True, has_winfo=True, windowed_attempted=True,
    ) is False


# ---------------------------------------------------------------------------
# Line-number dual interpretation (window numbering vs absolute file)
# ---------------------------------------------------------------------------

def _synthetic_symbol(n=80, dep_idx=60):
    """Stand-in for DashboardAIAssistant: dep line near the middle/end."""
    lines = [f"  const pad{i} = {i};" for i in range(n)]
    lines[0] = "function DashboardAIAssistant() {"
    lines[dep_idx] = "  }, [mode, chatInput, loading, buildHistory, apiBase]);"
    lines[-1] = "}"
    return "\n".join(lines), dep_idx


def test_symbol_relative_splice_lands_deps_fix():
    """Round6 shape: window numbered 1..N; model emits symbol-relative lines."""
    sym, dep_idx = _synthetic_symbol()
    # Symbol lives at absolute file start_line=1662 (like the real component).
    file_start = 1662
    # Model copies the window number (1-indexed within symbol).
    isl = dep_idx + 1
    iel = dep_idx + 1
    new_code = "  }, [mode, chatInput, loading, buildHistory, apiBase, researchCountry]);"

    # Absolute interpretation alone would be OUT of bounds (isl=61, start=1662).
    _full_abs, ok_abs, _ = _apply_snippet_by_lines(sym, file_start, isl, iel, new_code)
    assert ok_abs is False

    full, ok, reason, mode = _apply_correction_line_splice(
        sym, file_start, isl, iel, new_code, prefer_symbol_relative=True,
    )
    assert ok is True
    assert mode == "symbol_relative"
    assert "researchCountry" in full
    assert full.splitlines()[dep_idx].endswith("researchCountry]);")


def test_absolute_file_lines_still_work_on_non_windowed_path():
    sym, dep_idx = _synthetic_symbol()
    file_start = 1662
    isl = file_start + dep_idx
    iel = isl
    new_code = "  }, [mode, chatInput, loading, buildHistory, apiBase, researchCountry]);"

    full, ok, reason, mode = _apply_correction_line_splice(
        sym, file_start, isl, iel, new_code, prefer_symbol_relative=False,
    )
    assert ok is True
    assert mode == "absolute_file"
    assert "researchCountry" in full


def test_absolute_emitted_on_windowed_path_still_lands_via_fallback():
    """If the model uses absolute file lines even on a windowed prompt, the
    second attempt (absolute_file) must still succeed."""
    sym, dep_idx = _synthetic_symbol()
    file_start = 1662
    isl = file_start + dep_idx
    iel = isl
    new_code = "  }, [mode, chatInput, loading, buildHistory, apiBase, researchCountry]);"

    full, ok, reason, mode = _apply_correction_line_splice(
        sym, file_start, isl, iel, new_code, prefer_symbol_relative=True,
    )
    assert ok is True
    # First attempt (symbol_relative) is out of bounds for isl≈1722 on an
    # 80-line symbol; second attempt (absolute_file) lands.
    assert mode == "absolute_file"
    assert "researchCountry" in full


def test_six_line_surgical_payload_does_not_trigger_content_loss():
    """The round6 6-line new_code replacing a ~6-line span must be accepted —
    the content-loss guard only fires on catastrophic shrinks (130→8)."""
    sym, dep_idx = _synthetic_symbol(n=100, dep_idx=50)
    # Replace a 5-line span with 6 lines (net +1) — must pass.
    lines = sym.splitlines()
    # Build a small contiguous span around the dep line.
    span_start = dep_idx - 2  # 0-based
    span_end = dep_idx + 2
    new_lines = lines[span_start:span_end + 1]
    new_lines[2] = "  }, [mode, chatInput, loading, buildHistory, apiBase, researchCountry]);"
    new_code = "\n".join(new_lines)

    full, ok, reason, mode = _apply_correction_line_splice(
        sym, 1, span_start + 1, span_end + 1, new_code, prefer_symbol_relative=True,
    )
    assert ok is True, reason
    assert "researchCountry" in full
    # Symbol length preserved within a line or two
    assert abs(len(full.splitlines()) - 100) <= 1
