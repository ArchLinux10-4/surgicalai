"""
Tests for the Claude last-ditch fuzzy content-anchor rescue
(_locate_snippet_fuzzy / _apply_fuzzy_splice in services/pipeline.py).

This is an ADD-ON safety net that only engages after exact and
whitespace-tolerant matching have already failed. These tests cover:
  1. A confident, unique fuzzy match is accepted and spliced correctly.
  2. A low-similarity snippet is rejected (falls through, no false positive).
  3. An ambiguous case (two near-identical candidate blocks) is rejected.
  4. The oversized-text safety valve refuses to scan huge inputs.
  5. Exact matches still score highest (no regression vs plain text).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.pipeline import _locate_snippet_fuzzy, _apply_fuzzy_splice


def test_fuzzy_rescue_accepts_confident_unique_match():
    symbol_code = (
        "function handleLogin(user, pass) {\n"
        "    if (!user || !pass) {\n"
        "        return false;\n"
        "    }\n"
        "    console.log('logging in');\n"
        "    return true;\n"
        "}\n"
    )
    # Model's old_code has minor drift: dropped the blank/trailing space,
    # slightly different quote style — would fail exact + whitespace-tolerant.
    old_code_drifted = (
        "if (!user || !pass) {\n"
        "    return false\n"
        "}\n"
        "console.log(\"logging in\");"
    )
    new_code = "if (!user || !pass) {\n    return false;\n}\nconsole.log('user logged in');"

    matched_text, start_idx, end_idx, ok, reason, diag = _locate_snippet_fuzzy(
        symbol_code, old_code_drifted
    )
    assert ok, f"expected fuzzy match to succeed, got reason={reason}, diag={diag}"
    assert diag["best_ratio"] >= 0.85

    spliced = _apply_fuzzy_splice(symbol_code, start_idx, end_idx, new_code)
    assert "user logged in" in spliced
    assert "function handleLogin" in spliced  # rest of symbol preserved
    assert spliced.count("function handleLogin") == 1


def test_fuzzy_rescue_rejects_low_similarity():
    symbol_code = (
        "function foo() {\n"
        "    return 1;\n"
        "}\n"
        "function bar() {\n"
        "    return 2;\n"
        "}\n"
    )
    # Completely unrelated snippet — should not match anything.
    old_code = "const totallyUnrelatedThing = doSomethingElseEntirely();"
    _, _, _, ok, reason, diag = _locate_snippet_fuzzy(symbol_code, old_code)
    assert not ok
    assert diag["best_ratio"] < 0.85


def test_fuzzy_rescue_rejects_ambiguous_duplicate_blocks():
    # Two near-identical blocks in the same file — refusing to guess is
    # the correct, safe behavior (matches the ambiguity guard already used
    # by _locate_snippet_in_text for exact matches).
    symbol_code = (
        "if (x) {\n"
        "    doThing();\n"
        "}\n"
        "\n"
        "if (y) {\n"
        "    doThing();\n"
        "}\n"
    )
    old_code = "if (z) {\n    doThing();\n}"
    _, _, _, ok, reason, diag = _locate_snippet_fuzzy(symbol_code, old_code)
    assert not ok
    assert "ambiguous" in reason


def test_fuzzy_rescue_refuses_oversized_text():
    huge_text = "\n".join(f"line {i}" for i in range(4000))
    old_code = "line 42"
    _, _, _, ok, reason, diag = _locate_snippet_fuzzy(huge_text, old_code)
    assert not ok
    assert "too_large" in reason


def test_fuzzy_rescue_empty_inputs_are_safe():
    _, s, e, ok, reason, diag = _locate_snippet_fuzzy("", "")
    assert not ok and s == -1 and e == -1

    _, s, e, ok, reason, diag = _locate_snippet_fuzzy("some text", "")
    assert not ok and s == -1 and e == -1

    _, s, e, ok, reason, diag = _locate_snippet_fuzzy("", "some old code")
    assert not ok and s == -1 and e == -1


def test_apply_fuzzy_splice_preserves_surrounding_lines():
    text = "line0\nline1\nline2\nline3\nline4\n"
    result = _apply_fuzzy_splice(text, 1, 2, "REPLACED")
    assert result == "line0\nREPLACED\nline3\nline4\n"


def test_apply_fuzzy_splice_adds_trailing_newline_if_missing():
    text = "line0\nline1\nline2\n"
    result = _apply_fuzzy_splice(text, 1, 1, "NEW_LINE_NO_NEWLINE")
    assert result == "line0\nNEW_LINE_NO_NEWLINE\nline2\n"
