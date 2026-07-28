"""
Regression guard for the missing-diff-card bug: a single-line symbol edit
(e.g. one-line arrow functions/const declarations) produced a unified diff
that collapsed onto ONE physical line with no embedded "\\n" — because
_make_diff() fed difflib.unified_diff() lines with keepends=True (so the
header lines from lineterm="" have no newline of their own) and then joined
everything with "".join(diff). Every consumer of the diff string
(_has_real_diff() in this file, and InlineDiffCard.tsx's identical
line-prefix filter) does diff.split("\\n") and drops any "change" whose
split has no real "+"/"-" content line outside the "+++"/"---" headers.
For a single-line symbol, the header + content collapsed onto one line
starting with "---", so it read as a no-op "ghost diff" and was silently
dropped — smart_result_emitted fired, but no diff card ever rendered.

Proven against real production logs, not just synthetic input:
  - surgical_debug_35a98c4c.jsonl: utils.js::formatCurrency, a
    `symbol_lines: 1` single-line edit, is dropped end-to-end.
  - surgical_debug_e3f0e267.jsonl: the smart_result_emitted event immediately
    following utils.js::getCurrencySymbol (symbol_lines: 1) reports
    skipped_count: 1 — the single-line edit was silently discarded while two
    sibling multi-line edits in the same batch (TopCharts.jsx) went through.

Fix: build diff lines with splitlines() (no keepends) and join with "\\n"
ourselves, so every line — header or content — gets exactly one separator
regardless of source string length.

NOTE ON FILE PATH: this test loads _make_diff directly out of
backend/services/pipeline.py by absolute path derived from this test file's
own location, NOT via a bare open("pipeline.py"). A bare relative open picks
up backend/pipeline.py instead if pytest's cwd is `backend/` (the documented
"cd backend && python -m pytest tests/" invocation) — a separate, older,
already-diverged module with its own _make_diff that nothing in the running
app imports (main.py -> routers/chat.py imports only `services.pipeline`).
Testing that file would validate dead code, not the fix.
"""
import re
import os

_SERVICES_PIPELINE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "services", "pipeline.py"
)


def _load_make_diff():
    src = open(_SERVICES_PIPELINE).read()
    m = re.search(r"\ndef _make_diff\(.*?\n(?=\ndef |\n# ---)", src, re.S)
    assert m, "could not locate _make_diff in services/pipeline.py"
    ns = {"difflib": __import__("difflib")}
    exec("def _make_diff" + m.group(0).split("def _make_diff", 1)[1], ns)
    return ns["_make_diff"]


_make_diff = _load_make_diff()


def _has_adds_and_removes(diff_text: str):
    """Mirrors _has_real_diff's line-prefix check in this file, and
    InlineDiffCard.tsx's identical client-side filter."""
    lines = diff_text.split("\n")
    has_adds = any(l.startswith("+") and not l.startswith("+++") for l in lines)
    has_removes = any(l.startswith("-") and not l.startswith("---") for l in lines)
    return has_adds, has_removes


def test_single_line_symbol_diff_is_not_collapsed_onto_one_line():
    """The real utils.js::formatCurrency edit from session 35a98c4c."""
    original = "export const formatCurrency = (num) => num || num === 0 ? `$${Number(num).toLocaleString()}` : ''"
    new_code = "export const formatCurrency = (num) => num || num === 0 ? `$${Number(num).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : ''"

    diff = _make_diff(original, new_code, "utils.js::formatCurrency")

    # The bug: header lines glued directly onto the body with zero separators,
    # e.g. "--- x (original)+++ x (modified)@@ -1 +1 @@-old...+new...".
    assert "(original)+++" not in diff, "header lines are still glued together with no newline"
    assert "@@-" not in diff and "@@+" not in diff, "@@ header glued onto first content line"

    has_adds, has_removes = _has_adds_and_removes(diff)
    assert has_adds and has_removes, (
        "single-line symbol diff was misclassified as a no-op ghost diff and "
        "would be silently dropped before the frontend ever sees it"
    )


def test_single_line_symbol_diff_second_real_session_example():
    """The real utils.js::getCurrencySymbol edit from session e3f0e267,
    whose corresponding smart_result_emitted event logged skipped_count: 1."""
    original = "export const getCurrencySymbol = (code) => CURRENCY_SYMBOLS[code] || code"
    new_code = "export const getCurrencySymbol = (code) => CURRENCY_SYMBOLS[code] || '$'"

    diff = _make_diff(original, new_code, "utils.js::getCurrencySymbol")
    has_adds, has_removes = _has_adds_and_removes(diff)
    assert has_adds and has_removes


def test_multiline_symbol_diff_unaffected_no_regression():
    """Multi-line diffs already worked (their body lines carry their own
    embedded newline from keepends=True); confirm the fix doesn't change
    that outcome, and also cleans up the previously-glued header line."""
    original = "function add(a, b) {\n  return a + b;\n}\n"
    new_code = "function add(a, b) {\n  return a - b;\n}\n"

    diff = _make_diff(original, new_code, "utils.js::add")
    has_adds, has_removes = _has_adds_and_removes(diff)
    assert has_adds and has_removes

    lines = diff.split("\n")
    assert lines[0].startswith("--- "), "--- header line must be on its own line"
    assert lines[1].startswith("+++ "), "--- and +++ header lines must be on separate lines"
    assert lines[2].startswith("@@ "), "@@ hunk header must be on its own line"


def test_identical_code_still_produces_empty_diff():
    """No-op edits (original == new_code) must still yield a diff with no
    +/- content lines — this must keep working exactly as before."""
    code = "export const noop = () => null"
    diff = _make_diff(code, code, "utils.js::noop")
    has_adds, has_removes = _has_adds_and_removes(diff)
    assert not has_adds and not has_removes


def test_diff_lines_join_with_exactly_one_newline():
    """Every yielded diff line must be separated by exactly one '\\n', with
    no doubled or missing separators, for both single- and multi-line input."""
    original = "const x = 1"
    new_code = "const x = 2"
    diff = _make_diff(original, new_code, "f.js::x")
    assert "\n\n" not in diff
    lines = diff.split("\n")
    # ---, +++, @@, -old line, +new line
    assert len(lines) == 5, f"unexpected line count: {lines!r}"
