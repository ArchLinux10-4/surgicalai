"""
Regression guard for targeted (snippet) edits on large symbols.

Covers the fix for the "creates a new file instead of editing the large
existing file" anti-pattern: when Claude can only partially see a big symbol
it must be able to express a small old_code/new_code change that the pipeline
splices into the full symbol — rather than degrading to a new file + manual
wire-up instructions.

The helper under test reconstructs the complete new symbol body so every
downstream stage (diff, QA, structural QA, retry loop, apply) keeps operating
on the full before/after symbol exactly as before.
"""
import os
import re

import pytest


def _load_helper():
    """Load _apply_snippet_to_symbol from pipeline.py in isolation."""
    here = os.path.dirname(__file__)
    candidates = [
        os.path.join(here, "..", "services", "pipeline.py"),
        os.path.join(here, "pipeline.py"),
    ]
    src = None
    for c in candidates:
        if os.path.exists(c):
            src = open(c).read()
            break
    assert src is not None, "pipeline.py not found"
    start = src.index("def _apply_snippet_to_symbol")
    end = src.index("\n# ---", start)
    ns = {"re": re}
    exec(src[start:end], ns)
    return ns["_apply_snippet_to_symbol"]


apply_snippet = _load_helper()


def _big_symbol():
    sym = "\n".join(f"  line {i}: const x{i} = {i};" for i in range(900))
    return sym.replace(
        "  line 736: const x736 = 736;",
        '        <h1 className="hero-title">Ship code</h1>',
    )


def test_exact_unique_match_deep_in_symbol():
    sym = _big_symbol()
    old = '        <h1 className="hero-title">Ship code</h1>'
    new = old + "\n        <HeroMockupAnimation />"
    res, ok, reason = apply_snippet(sym, old, new)
    assert ok and reason == "exact"
    assert "<HeroMockupAnimation />" in res
    # exactly one line added; every other line preserved
    assert res.count("\n") == sym.count("\n") + 1
    assert "line 0: const x0 = 0;" in res
    assert "line 899: const x899 = 899;" in res


def test_ambiguous_match_is_rejected():
    sym = "  foo()\n  bar()\n  foo()\n"
    res, ok, reason = apply_snippet(sym, "  foo()", "  baz()")
    assert not ok and "ambiguous" in reason.lower()


def test_not_found_is_rejected():
    res, ok, reason = apply_snippet(_big_symbol(), "absent text xyz", "x")
    assert not ok


def test_line_number_prefix_is_stripped():
    sym = _big_symbol()
    numbered = '  736:         <h1 className="hero-title">Ship code</h1>'
    res, ok, reason = apply_snippet(sym, numbered, "        <h1>NEW</h1>")
    assert ok
    assert "<h1>NEW</h1>" in res


def test_whitespace_tolerant_match():
    sym = "def a():\n    return 1   \n    pass\n"
    old = "    return 1\n    pass"  # no trailing spaces
    res, ok, reason = apply_snippet(sym, old, "    return 2\n    pass")
    assert ok
    assert "return 2" in res


def test_empty_old_code_is_rejected():
    res, ok, reason = apply_snippet(_big_symbol(), "", "x")
    assert not ok


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
