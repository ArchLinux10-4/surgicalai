"""
Regression tests for the LF2 large-file fix (PR #99).

Covers the two bugs found live:
  Bug 1 — QA-fix loop degraded an 850-line symbol into a 2-line fragment because it
          (a) demanded the COMPLETE symbol and (b) blindly stored whatever the model
          returned, with no targeted-edit splice and no fragment guard.
  Bug 2 — QA penalised file-level imports/exports that live OUTSIDE the symbol.

These tests exercise the REAL pure helpers (_apply_snippet_to_symbol, _fragment_reason,
_compute_target_element) extracted verbatim from pipeline.py, plus a faithful replica of
the QA-fix-loop handler decision so the accept/reject policy is locked in.
"""
import ast
import os
import types

# ── Extract real helpers from pipeline.py via AST ────────────────────────────
# Previously this loaded a generated file, tests/_helpers_extracted.py, that
# was never committed to git — broken on every fresh clone (confirmed via
# `git ls-files`: the test file is tracked, the helper file it loads is not).
# Fixed to use the same in-repo AST-extraction pattern as
# tests/test_safe_claude_call.py: pull the exact shipped functions straight
# out of pipeline.py, so there's no separate artifact to go stale or get lost.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.normpath(os.path.join(_HERE, "..", "services", "pipeline.py"))
_FUNCS_TO_EXTRACT = [
    "_apply_snippet_to_symbol",
    "_fragment_reason",
    "_compute_target_element",
    # aa1584f4 structural invariant — pulled with apply so isolated extracts
    # exercise the same gate as production (globals().get fallback otherwise).
    "_finalize_snippet_apply",
    "_delimiter_parity_reason",
    "_superseded_tail_reason",
    "_post_splice_structure_reason",
    "_html_jsx_tag_nets",
    "_html_jsx_closer_counts",
    "_iter_html_jsx_tag_events",
    "_bracket_balance_reason",
]


def _extract_helpers():
    src = open(_PIPELINE, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {
        "_dlog": lambda *a, **kw: None,
        "_VOID_HTML_TAGS": frozenset({
            "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr",
        }),
        "Optional": object,  # type hint only; unused at runtime in extracted bodies
    }
    # Pull in top-level stdlib imports (e.g. `re`) the extracted functions
    # rely on but don't carry with them as AST source segments.
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            segment = ast.get_source_segment(src, node)
            if segment:
                try:
                    exec(segment, ns)
                except Exception:
                    pass
    # typing.Optional is referenced in _bracket_balance_reason annotations;
    # provide a real alias when the typing import was skipped/failed.
    try:
        from typing import Optional as _Opt
        ns["Optional"] = _Opt
    except Exception:
        pass
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _FUNCS_TO_EXTRACT:
            segment = ast.get_source_segment(src, node)
            if segment:
                exec(segment, ns)
    for fn_name in _FUNCS_TO_EXTRACT:
        assert fn_name in ns, f"Failed to extract {fn_name} from pipeline.py"
    return types.SimpleNamespace(**{k: ns[k] for k in _FUNCS_TO_EXTRACT})


H = _extract_helpers()


# Build a realistic 850-line component symbol.
def _big_symbol():
    lines = ["export function LandingPage() {",
             "  const [open, setOpen] = useState(false);",
             "  const ref = useRef(null);"]
    for i in range(1, 840):
        lines.append(f"  const [field{i}, setField{i}] = useState('');  // state {i}")
    lines += [
        "  return (",
        "    <div>",
        '      <section className="hero">',
        '        <h1 className="hero-title">Welcome to our platform</h1>',
        '        <p className="hero-sub">The old subtitle text goes here.</p>',
        '        <button className="hero-cta">Get Started</button>',
        "      </section>",
        "    </div>",
        "  );",
        "}",
    ]
    return "\n".join(lines)


SYM = _big_symbol()


# ── Faithful replica of the QA-fix-loop accept/reject decision (pipeline.py) ──
def fix_loop_decision(symbol_code, corrected_code, corrected_old, prior_new_code):
    accepted = None
    if corrected_code:
        if corrected_old:
            full, ok, _ = H._apply_snippet_to_symbol(symbol_code, corrected_old, corrected_code)
            if ok:
                accepted = full
        else:
            if H._fragment_reason(symbol_code, corrected_code) is None:
                accepted = corrected_code
    if accepted is not None and accepted != prior_new_code:
        return accepted, True
    return prior_new_code, False


# ── Bug 1 tests ──────────────────────────────────────────────────────────────
def test_degenerate_fragment_without_old_code_is_rejected():
    """The exact LF2 failure: 2-line new_code, no old_code -> must NOT be stored."""
    frag = ('        <p className="hero-sub">Build faster with confidence</p>\n'
            '        <button className="hero-cta">Start Free Trial</button>')
    new, changed = fix_loop_decision(SYM, frag, "", prior_new_code=SYM)
    assert changed is False, "degenerate fragment must be rejected"
    assert new == SYM, "prior change must be preserved when fragment is rejected"


def test_targeted_edit_splices_to_full_symbol():
    """old_code + new_code is spliced into the FULL symbol -> QA sees full symbol."""
    old = '        <p className="hero-sub">The old subtitle text goes here.</p>\n        <button className="hero-cta">Get Started</button>'
    new = '        <p className="hero-sub">Build faster with confidence</p>\n        <button className="hero-cta">Start Free Trial</button>'
    result, changed = fix_loop_decision(SYM, new, old, prior_new_code=SYM)
    assert changed is True
    assert len(result.splitlines()) == len(SYM.splitlines()), "must be the full symbol, not a fragment"
    assert "Build faster with confidence" in result
    assert "Start Free Trial" in result
    assert "export function LandingPage()" in result
    assert result.count("useState") == SYM.count("useState"), "no state lines dropped"


def test_targeted_edit_failed_splice_keeps_prior():
    """If old_code does not match, keep the prior change (never store a fragment)."""
    new, changed = fix_loop_decision(SYM, "whatever", "THIS TEXT IS NOT IN THE SYMBOL", prior_new_code=SYM)
    assert changed is False
    assert new == SYM


def test_full_symbol_rewrite_still_accepted():
    """A genuine full-symbol rewrite (keeps declaration line) is still accepted."""
    full = SYM.replace("Get Started", "Start Free Trial")
    new, changed = fix_loop_decision(SYM, full, "", prior_new_code=SYM)
    assert changed is True
    assert "Start Free Trial" in new
    assert "export function LandingPage()" in new


def test_tgt_repl_recompute_is_minimal_after_splice():
    """After splice, recomputed target_element/replacement are a minimal region, not the whole symbol."""
    old = '        <p className="hero-sub">The old subtitle text goes here.</p>\n        <button className="hero-cta">Get Started</button>'
    new = '        <p className="hero-sub">Build faster with confidence</p>\n        <button className="hero-cta">Start Free Trial</button>'
    full, _, _ = H._apply_snippet_to_symbol(SYM, old, new)
    tgt, repl = H._compute_target_element(SYM, full)
    assert tgt is not None and repl is not None
    assert len(tgt.splitlines()) < 20, "target_element must be a minimal region, not the whole 850-line symbol"
    assert "Build faster" in repl


# ── Bug 2 sanity: fragment_reason boundaries ─────────────────────────────────
def test_small_symbol_full_rewrite_not_flagged():
    small = "def add(a, b):\n    return a + b"
    assert H._fragment_reason(small, "def add(a, b):\n    return a + b + 0") is None


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
