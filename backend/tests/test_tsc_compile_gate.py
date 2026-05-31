"""
Regression guard for the natural-pipeline tsc compile gate.

Reproduces the LANDING octal-escape build break (TS1487) that LLM QA passed
at 9/10, and verifies:
  1. real tsc flags the octal escape on the broken content,
  2. the orig-vs-new DELTA algorithm (the core of _tsc_introduced_errors)
     surfaces ONLY the introduced error and filters pre-existing
     isolated-file noise (react/jsx-runtime, JSX.IntrinsicElements, etc.),
  3. the fixed content introduces zero new errors -> ships.

Run: pytest test_tsc_compile_gate.py -v
Requires tsc on PATH (npm i -g typescript).
"""
import os
import sys
import shutil
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend", "services"))
sys.path.insert(0, "/tmp/sai_src/surgicalai-main/backend/services")

try:
    from linter_validator import validate_linters
except Exception:  # pragma: no cover
    validate_linters = None

pytestmark = pytest.mark.skipif(
    validate_linters is None or shutil.which("tsc") is None,
    reason="linter_validator or tsc unavailable",
)

# A const-CSS symbol WITHOUT the octal (the pristine 'original' symbol state).
ORIG = (
    "const CSS = `\n"
    ".check::before { content:''; }\n"
    "`;\n"
    "export default function X() { return <div className=\"check\" />; }\n"
)
# Same symbol AFTER the architect added the checkmark via a CSS unicode escape,
# but emitted it as a single backslash inside a JS template literal -> TS1487.
BROKEN = (
    "const CSS = `\n"
    ".check::before { content:'\\2713'; }\n"
    "`;\n"
    "export default function X() { return <div className=\"check\" />; }\n"
)
# The correct fix: double the backslash so the template literal emits \\2713.
FIXED = (
    "const CSS = `\n"
    ".check::before { content:'\\\\2713'; }\n"
    "`;\n"
    "export default function X() { return <div className=\"check\" />; }\n"
)


def _delta(orig_code, new_code, fname="LandingPage.tsx"):
    """Mirror of _tsc_introduced_errors' delta computation."""
    orig_errs = validate_linters(orig_code, fname)
    new_errs = validate_linters(new_code, fname)
    _sig = lambda e: (e.get("code", ""), (e.get("message", "") or "").strip())
    counts = {}
    for e in orig_errs:
        counts[_sig(e)] = counts.get(_sig(e), 0) + 1
    introduced = []
    for e in new_errs:
        s = _sig(e)
        if counts.get(s, 0) > 0:
            counts[s] -= 1
        else:
            introduced.append(e)
    return introduced


def test_tsc_flags_octal_on_broken():
    errs = validate_linters(BROKEN, "LandingPage.tsx")
    assert any("Octal escape" in (e.get("message") or "") for e in errs), \
        f"tsc must flag the octal escape; got {errs}"


def test_broken_introduces_exactly_the_octal():
    introduced = _delta(ORIG, BROKEN)
    assert len(introduced) == 1, f"expected 1 introduced error, got {introduced}"
    assert "Octal escape" in introduced[0]["message"]


def test_fixed_introduces_nothing():
    # The fix must clear the gate: zero NEW errors vs the original.
    introduced = _delta(ORIG, FIXED)
    assert introduced == [], f"fixed content must introduce no new errors; got {introduced}"


def test_preexisting_noise_is_not_flagged():
    # orig already carries isolated-file react/jsx noise; an unrelated edit that
    # does not add the octal must produce zero introduced errors.
    introduced = _delta(ORIG, ORIG)
    assert introduced == [], f"identical content must yield no delta; got {introduced}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
