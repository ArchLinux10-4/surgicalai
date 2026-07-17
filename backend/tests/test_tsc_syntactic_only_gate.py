"""
Regression tests for the SYNTACTIC-ONLY tsc block gate
(session 6930f196 round 2).

PROVEN ROOT CAUSE (surgical_debug_6930f196 (4).jsonl):
  Two correct edits to fileClassify.tsx (widen FileFilter union to add
  'edited'; make isEditedFile read f.origin) scored 9/safe on their own merits
  but were force-blocked to 3/10 by the tsc_introduced_errors gate. The single
  "introduced" error was:

      {"code": "", "line": 63, "message":
       "Property 'length' does not exist on type '{}'."}

  This is a SEMANTIC diagnostic at files.length — a line the edit never touched
  and OUTSIDE the edited symbols. It exists only because the isolated single-file
  compile (no node_modules / sibling modules) collapses an inferred type to '{}'.
  The correction loop cannot fix an error outside the edited symbol, so the run
  blocked forever. Reproduced with the EXACT Vercel harness: original vs composed
  compile to identical error lists — the edit introduces ZERO real errors.

FIX: only SYNTACTIC diagnostics are a pure function of the single file and safe
to block on. Semantic introduced errors are advisory-only. _tsc_error_kind()
classifies; the gate blocks only on kind == 'syntactic'.

Run: pytest backend/tests/test_tsc_syntactic_only_gate.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.linter_validator import _tsc_error_kind, _make_err  # noqa: E402


# ── _tsc_error_kind classification ────────────────────────────────────────
def test_explicit_kind_hint_wins():
    assert _tsc_error_kind("TS2339", "syntactic") == "syntactic"
    assert _tsc_error_kind("TS1005", "semantic") == "semantic"


def test_code_range_1xxx_is_syntactic():
    # Parser-level diagnostics: ';' expected, '}' expected, unexpected token.
    for c in ("TS1005", "TS1109", "TS1128", "1005", 1434, "TS1002"):
        assert _tsc_error_kind(c) == "syntactic", c


def test_code_range_2xxx_7xxx_is_semantic():
    for c in ("TS2339", "TS2307", "TS7026", "TS7006", "TS2367", "TS2551"):
        assert _tsc_error_kind(c) == "semantic", c


def test_the_exact_live_phantom_error_is_semantic():
    # The literal error dict that blocked a correct edit at 3/10 in the log.
    live = {"code": "", "line": 63,
            "message": "Property 'length' does not exist on type '{}'."}
    assert _tsc_error_kind(live["code"], live.get("kind")) == "semantic"


def test_empty_or_unknown_code_defaults_semantic_failsafe():
    # Fail-safe: never block on a diagnostic we cannot positively classify.
    assert _tsc_error_kind("") == "semantic"
    assert _tsc_error_kind(None) == "semantic"
    assert _tsc_error_kind("garbage") == "semantic"
    assert _tsc_error_kind(True) == "semantic"  # bool guard


# ── The block-gate filter (exact pipeline logic) ──────────────────────────
def _block_set(introduced_all):
    """Mirror _tsc_file_introduced_errors: block only on syntactic."""
    return [e for e in introduced_all
            if _tsc_error_kind(e.get("code", ""), e.get("kind")) == "syntactic"]


def test_live_scenario_no_longer_blocks():
    # Exactly what the gate saw in the (4) log: one semantic phantom error.
    introduced_all = [{"code": "", "line": 63,
                       "message": "Property 'length' does not exist on type '{}'."}]
    assert _block_set(introduced_all) == []  # advisory only -> no block


def test_vercel_semantic_tagged_error_no_longer_blocks():
    # Post-redeploy: service tags kind explicitly.
    introduced_all = [{"code": "TS2339", "kind": "semantic", "line": 63,
                       "message": "Property 'length' does not exist on type '{}'."}]
    assert _block_set(introduced_all) == []


def test_genuine_syntactic_error_still_blocks():
    # A real parse break the edit introduced MUST still block.
    introduced_all = [{"code": "TS1005", "kind": "syntactic", "line": 10,
                       "message": "';' expected."}]
    assert len(_block_set(introduced_all)) == 1


def test_mixed_blocks_only_on_syntactic():
    introduced_all = [
        {"code": "TS1128", "kind": "syntactic", "message": "Declaration or statement expected."},
        {"code": "", "message": "Property 'length' does not exist on type '{}'."},
        {"code": "TS2307", "kind": "semantic", "message": "Cannot find module '../types'."},
    ]
    blk = _block_set(introduced_all)
    assert len(blk) == 1
    assert blk[0]["code"] == "TS1128"


# ── _make_err carries code + derived kind ─────────────────────────────────
def test_make_err_populates_kind_from_code():
    e = _make_err(1, 1, "';' expected.", "d", code="TS1005")
    assert e["code"] == "TS1005" and e["kind"] == "syntactic"
    e2 = _make_err(1, 1, "Cannot find module.", "d", code="TS2307")
    assert e2["kind"] == "semantic"
    e3 = _make_err(1, 1, "pyflakes error", "d")  # no code (python path)
    assert e3["kind"] == "semantic"
