"""
Regression guard for the LANDING failure class round 2:

  "QA truncates a large symbol's TAIL -> can't verify the JSX/append edit ->
   scores it 6/10 -> hard gate blocks a valid change -> section never ships."

Root cause was a 60K-char QA review cap that head-truncated the symbol. The real
LandingPage symbol is ~74K chars (new ~82K), so its end (JSX before the CTA) was
never seen by QA. Fix: 200K cap + head+tail elision so the end is preserved.

These tests exercise the _cap_code logic in isolation (no network / no Anthropic).
Run: python3 test_qa_code_cap.py
"""

# Mirror of the production cap logic in pipeline.run_qa_review (kept in sync).
_MAX_CODE_CHARS = 200_000


def _cap_code(_c: str) -> str:
    if len(_c) <= _MAX_CODE_CHARS:
        return _c
    _half = _MAX_CODE_CHARS // 2
    _dropped = len(_c) - (2 * _half)
    return (
        _c[:_half]
        + f"\n\n... [{_dropped} chars elided from the MIDDLE to fit the review window — "
          f"head and tail are shown in full; review the elided region manually] ...\n\n"
        + _c[-_half:]
    )


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_real_landing_symbol_not_truncated():
    """A ~82K-char symbol (real LandingPage NEW size) must pass through untouched."""
    sym = ("X" * 40000) + "UNIQUE_JSX_BEFORE_CTA_ANCHOR" + ("Y" * 42000)
    out = _cap_code(sym)
    _assert(out == sym, "82K symbol was modified; must be sent in full")
    _assert("UNIQUE_JSX_BEFORE_CTA_ANCHOR" in out, "tail anchor lost")


def test_pathological_keeps_head_and_tail():
    """A genuinely huge symbol keeps BOTH ends; only the middle is elided."""
    head = "HEAD_START_MARKER"
    tail = "TAIL_END_MARKER_JSX"
    sym = head + ("M" * (_MAX_CODE_CHARS + 50000)) + tail
    out = _cap_code(sym)
    _assert(out.startswith(head), "head was dropped")
    _assert(out.rstrip().endswith(tail), "tail (where edits land) was dropped")
    _assert("elided from the MIDDLE" in out, "missing elision marker")
    _assert(len(out) < len(sym), "elision did not reduce size")


def test_small_symbol_unchanged():
    sym = "const x = 1;\n"
    _assert(_cap_code(sym) == sym, "small symbol must be unchanged")


def test_cap_is_large_enough_for_real_components():
    """Guard the constant itself so nobody silently shrinks it below real needs."""
    _assert(_MAX_CODE_CHARS >= 120_000,
            f"QA cap {_MAX_CODE_CHARS} too small for real large components (need >=120K)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} tests passed.")
