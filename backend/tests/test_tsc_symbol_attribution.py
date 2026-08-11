"""tsc error attribution to owning composed edits (session 3a6150e9).

Proven: compose-then-tsc (6930f196) is correct, but force-blocking EVERY
index when any syntactic error appears poisoned good sibling edits
(EMPTY_CRITERIA score 10 → blocked/3 with ``tsc: 8 compile error(s)``).
Attribute by composed-line ownership instead.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import attribute_tsc_errors_to_indices  # noqa: E402


def _composed_two_edits():
    """Simulate two applied full-symbol replacements in one file."""
    a = "const EMPTY_CRITERIA = {\n  Country: '',\n};\n"
    b = (
        "function FilterSidebar() {\n"
        "  return (\n"
        "    <div broken\n"  # syntactic damage lives HERE
        "  );\n"
        "}\n"
    )
    composed = a + "\n" + b
    return composed, [(0, a), (1, b)]


def test_errors_in_broken_symbol_do_not_blanket_good_sibling():
    composed, applied = _composed_two_edits()
    # Lines for b: a has 3 lines + blank = 4, so b starts at line 5.
    a_lines = applied[0][1].count("\n") + (0 if applied[0][1].endswith("\n") else 1)
    # find() based: exact
    pos_b = composed.find(applied[1][1])
    start_b = composed.count("\n", 0, pos_b) + 1
    err_line = start_b + 2  # inside FilterSidebar body
    errors = [
        {"code": "TS1005", "line": err_line, "message": "';' expected."},
        {"code": "TS1005", "line": err_line, "message": "';' expected."},
    ]
    by_idx = attribute_tsc_errors_to_indices(composed, applied, errors)
    assert 0 not in by_idx, (
        "EMPTY_CRITERIA-equivalent (idx 0) must not be force-blocked when "
        f"errors sit only in idx 1 — got {by_idx!r} (a_lines={a_lines})"
    )
    assert 1 in by_idx and len(by_idx[1]) == 2


def test_error_inside_good_symbol_only_blocks_that_owner():
    composed, applied = _composed_two_edits()
    pos_a = composed.find(applied[0][1])
    start_a = composed.count("\n", 0, pos_a) + 1
    errors = [{"code": "TS1005", "line": start_a + 1, "message": "';' expected."}]
    by_idx = attribute_tsc_errors_to_indices(composed, applied, errors)
    assert list(by_idx.keys()) == [0]
    assert len(by_idx[0]) == 1


def test_empty_errors_returns_empty():
    composed, applied = _composed_two_edits()
    assert attribute_tsc_errors_to_indices(composed, applied, []) == {}


def test_unlocatable_bodies_return_empty_for_safe_fallback():
    """Caller falls back to blanket-block when attribution returns {}."""
    errors = [{"code": "TS1005", "line": 10, "message": "x"}]
    assert attribute_tsc_errors_to_indices("nope", [(0, "missing")], errors) == {}


def test_orphan_line_attaches_to_nearest_symbol():
    composed, applied = _composed_two_edits()
    # Line far past end of file → nearest is last symbol
    far = composed.count("\n") + 50
    by_idx = attribute_tsc_errors_to_indices(
        composed, applied, [{"code": "TS1005", "line": far, "message": "x"}]
    )
    assert list(by_idx.keys()) == [1]


def test_pipeline_wiring_uses_attribution_before_force_block():
    import inspect
    from services import pipeline as p

    src = inspect.getsource(p)
    assert "attribute_tsc_errors_to_indices" in src
    # pre_check must not loop `for _ti in _tidxs: _force_block_on_tsc(_ti, _t_introduced`
    # blindly anymore — must use _block_map / _errs
    assert "_block_map = _t_by_idx if _t_by_idx else" in src
    assert '_force_block_on_tsc(_ti, _errs, "")' in src
    assert '_force_block_on_tsc(_ti, _errs, " remain after auto-fix")' in src
