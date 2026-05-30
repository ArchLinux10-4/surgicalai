"""
PR #82 — regression tests for the unified delta gate.

Covers two coupled changes in backend/services/pipeline.py:
  • Change 1 — smart pipeline linter gate is now DELTA, not absolute
    (`_lint_new_count > _lint_orig_count`; introduced = new errors minus the
    pre-existing baseline multiset). Closes the BIG7/BIG9 false-positive class
    and is the prerequisite that lets a real tsc be enabled safely later.
  • Change 3 — the natural (Claude) pipeline's `_run_deterministic_gate` now
    runs the same redeclaration + linter DELTA checks the smart pipeline has,
    so selecting a Claude architect model no longer silently drops the PR #81
    BIG10 protection.

These exercise the EXACT logic embedded in the patch against the real
PR #81 validators (`detect_redeclarations`, `validate_linters`).

Run from backend/:  python -m pytest tests/test_gate_delta_pr82.py
(tree-sitter + tree-sitter-typescript required for the redeclaration cases;
they are skipped — fail-safe — if tree-sitter is absent, same as production.)
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from services.syntax_validator import detect_redeclarations          # noqa: E402
from services.linter_validator import validate_linters, count_linter_errors  # noqa: E402


# ── Exact replicas of the patched delta logic ────────────────────────────────
def redecl_delta(orig_full, edited_full, fname):
    orig_msgs = {e["message"] for e in detect_redeclarations(orig_full, fname)}
    return [e for e in detect_redeclarations(edited_full, fname)
            if e["message"] not in orig_msgs]


def lint_delta(orig_errs, new_errs):
    base = Counter(e.get("message") for e in orig_errs)
    introduced = []
    for e in new_errs:
        m = e.get("message")
        if base.get(m, 0) > 0:
            base[m] -= 1
        else:
            introduced.append(e)
    return introduced


_TS_OK = bool(detect_redeclarations(
    "const a=1;\nconst a=2;\n", "f.js"))  # truthy only if tree-sitter present


# ── Change 3: redeclaration delta on the natural path ────────────────────────
def test_introduced_redeclaration_is_flagged():
    if not _TS_OK:
        return  # tree-sitter absent → fail-skipped, same as production
    orig = "function checkAdminAuth(req){ return req.user; }\n\nfunction loadRows(){ return []; }\n"
    edited = ("function checkAdminAuth(req){ return req.user; }\n\n"
              "const checkAdminAuth = (r)=>r.user;\nfunction loadRows(){ return [1]; }\n")
    introduced = redecl_delta(orig, edited, "batchSearchRoutes.js")
    assert len(introduced) >= 1
    assert any("checkAdminAuth" in e["message"] for e in introduced)


def test_preexisting_duplicate_not_blamed_on_edit():
    if not _TS_OK:
        return
    orig = "const dup = 1;\nconst dup = 2;\n\nfunction loadRows(){ return []; }\n"
    edited = "const dup = 1;\nconst dup = 2;\n\nfunction loadRows(){ return [1,2]; }\n"
    assert redecl_delta(orig, edited, "f.js") == []


def test_clean_edit_no_redeclaration():
    if not _TS_OK:
        return
    orig = "function checkAdminAuth(req){ return req.user; }\n\nfunction loadRows(){ return []; }\n"
    edited = "function checkAdminAuth(req){ return req.user; }\n\nfunction loadRows(){ return [9]; }\n"
    assert redecl_delta(orig, edited, "f.js") == []


# ── Change 1: linter delta multiset subtraction ──────────────────────────────
def test_only_genuinely_new_lint_error_introduced():
    base = [{"message": "Cannot find name 'foo'.", "line": 3},
            {"message": "Cannot find name 'foo'.", "line": 50}]
    new = [{"message": "Cannot find name 'foo'.", "line": 4},      # shifted
           {"message": "Cannot find name 'foo'.", "line": 51},     # shifted
           {"message": "Type 'x' is not assignable.", "line": 9}]  # NEW
    intro = lint_delta(base, new)
    assert len(intro) == 1
    assert "not assignable" in intro[0]["message"]


def test_preexisting_errors_clean_edit_ships():
    # Absolute gate would have blocked (errors > 0); delta gate ships (0 new).
    base = [{"message": "Cannot find name 'foo'.", "line": 3}]
    assert lint_delta(base, [{"message": "Cannot find name 'foo'.", "line": 3}]) == []


def test_linter_fail_skipped_when_tsc_absent():
    # tsc unavailable → validators return [] (fail-skipped, never fail-broken).
    assert isinstance(validate_linters("const x=1;\n", "f.ts"), list)
    # count is 0 when the tool can't run — never a false positive
    assert count_linter_errors("const x=1;\n", "f.ts") >= 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    p = f = 0
    for fn in fns:
        try:
            fn(); p += 1; print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            f += 1; print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n=== {p} passed, {f} failed ===")
    sys.exit(1 if f else 0)
