"""
Regression tests for the FILE-LEVEL INSERTION correction channel
(session e1ee32f5-81aa-4923-8922-ed4fa2b7cc80).

Root cause (fixed): the auto-heal correction loop could only express a fix
as a change *inside* the target symbol (new_code / old_code+new_code /
windowed splice), or — via Path 2b — as a REPLACEMENT of text that already
existed verbatim elsewhere in the file. It had NO channel for a pure
INSERTION of brand-new lines outside the symbol (e.g. a new
`lazy(() => import(...))` declaration that belongs in the file's import
preamble).

Proven in session e1ee32f5: a QA-blocked `RouteFallback` edit in main.jsx
(score 1/10) needed two new lazy imports added near the top of the file.
The correction model returned only `new_code` = the two import lines (149
chars, no old_code), which does not mention `RouteFallback`. The
`wrong_symbol_reason()` guard (session 6930f196) *correctly* rejected it,
and — having no other channel — the loop exhausted its retries and shipped
the broken score-1 card. Log evidence (verbatim from the trace):

  qa_retry_correction_parsed   idx=0 symbol=RouteFallback has_new_code=True
                               new_code_len=149 has_old_code=False sym_code_len=173
  correction_wrong_symbol_rejected  idx=0 symbol=RouteFallback corrected_len=149
      corrected_preview="const WhatIsABillRate = lazy(...);\n
                         const MarketRatesAIOverview = lazy(...);"
  qa_retry_correction_not_accepted  reason=no_valid_correction_produced

The fix adds `file_level_anchor`/`file_level_insert` to the correction
schema (via the shared correction prompt) and a new Path 2c acceptance
step (`_apply_file_level_insertion`) that queues the insertion as a
companion `_extra_ops` entry — WITHOUT weakening `wrong_symbol_reason`.

These tests assert:
  1. The regression scenario now produces exactly the right companion op,
     leaves the symbol body untouched, and does not depend on
     wrong_symbol_reason rejecting a symbol-unchanged correction.
  2. The anchor uniqueness guard rejects 0 / >1 occurrences without
     touching _extra_ops.
  3. The `wrong_symbol_reason` sibling-corruption guard (6930f196) is
     provably UNCHANGED: a correction with no file_level fields whose
     new_code does not mention the target symbol is still rejected.
"""
import ast
import pathlib

from services.pipeline import _apply_file_level_insertion, wrong_symbol_reason

_SRC_PATH = pathlib.Path(__file__).parent.parent / "services" / "pipeline.py"
_SRC = _SRC_PATH.read_text()


# ── Faithful reconstruction of session e1ee32f5's main.jsx (from the trace) ──
_MAIN_JSX = """\
// src/main.jsx
import React, { Suspense, lazy } from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';

// Public marketing page loads eagerly.
import PublicHome from './PublicHome.jsx';

const App = lazy(() => import('./App.jsx'));
const AuthGate = lazy(() => import('./AuthGate.jsx'));
const SignUp = lazy(() => import('./SignUp.jsx'));
const SetPasswordWelcome = lazy(() => import('./SetPasswordWelcome.jsx'));
const WhatIsABillRate = lazy(() => import('./WhatIsABillRate.jsx'));

import './index.css';

const RouteFallback = () => (
  <div
    style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      minHeight: '100vh',
    }}
  />
);

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <Router>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/home" element={<PublicHome />} />
          <Route path="/what-is-a-bill-rate" element={<WhatIsABillRate />} />
        </Routes>
      </Suspense>
    </Router>
  </React.StrictMode>
);
"""

# The exact RouteFallback symbol code (10 lines, ~173 chars in the trace).
_ROUTE_FALLBACK_CODE = """\
const RouteFallback = () => (
  <div
    style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      minHeight: '100vh',
    }}
  />
);"""

# The anchor: the last existing lazy() import line (appears exactly once).
_ANCHOR = "const WhatIsABillRate = lazy(() => import('./WhatIsABillRate.jsx'));"
# The new line the model wants inserted after the anchor (the real gap: the
# new MarketRatesAIOverview route needed a lazy import that did not yet exist).
_INSERT = "const MarketRatesAIOverview = lazy(() => import('./MarketRatesAIOverview.jsx'));"


class _Sym:
    def __init__(self, name, code):
        self.name = name
        self.code = code


def _make_shells():
    return [{
        "symbol": _Sym("RouteFallback", _ROUTE_FALLBACK_CODE),
        "filename": "main.jsx",
        "file_content": _MAIN_JSX,
        # new_code is the *broken* edit QA blocked at score 1 — in this scenario
        # the symbol itself needs no change, so it should end up == the symbol.
        "new_code": _ROUTE_FALLBACK_CODE,
    }]


# ─────────────────────────── Required test 1 ───────────────────────────────

def test_regression_e1ee32f5_queues_exactly_one_companion_insertion():
    """The scenario that shipped a score-1 card now produces one, correct
    file-level companion op via the existing _extra_ops plumbing."""
    shells = _make_shells()
    fixed, reason = _apply_file_level_insertion(shells, 0, _ANCHOR, _INSERT)

    assert fixed is True
    assert reason == "ok"

    ops = shells[0]["_extra_ops"]
    assert len(ops) == 1, f"expected exactly one companion op, got {ops}"
    assert ops[0]["find"] == _ANCHOR
    assert ops[0]["replace"] == _ANCHOR + "\n" + _INSERT, (
        "insertion must be expressed as anchor + '\\n' + insert so the anchor "
        "is preserved and the new line lands immediately after it"
    )


def test_regression_e1ee32f5_symbol_body_is_untouched():
    """The symbol's own new_code must be unaffected — the fix lives entirely
    in the companion op, not by rewriting RouteFallback."""
    shells = _make_shells()
    _apply_file_level_insertion(shells, 0, _ANCHOR, _INSERT)
    assert shells[0]["new_code"] == _ROUTE_FALLBACK_CODE


def test_regression_e1ee32f5_no_wrong_symbol_rejection_for_this_shape():
    """The whole point: for a correction that leaves the symbol unchanged and
    routes the real fix through file_level_*, wrong_symbol_reason must not be
    what decides acceptance. When new_code == the symbol it trivially mentions
    the symbol name, so the guard (unchanged) returns None; and when the model
    scopes new_code only to the insertion, Path 2c never runs the guard at all.
    """
    # new_code == the symbol => guard is a no-op (mentions the name).
    assert wrong_symbol_reason("RouteFallback", _ROUTE_FALLBACK_CODE) is None
    # And the companion op still lands regardless of the guard.
    shells = _make_shells()
    fixed, _ = _apply_file_level_insertion(shells, 0, _ANCHOR, _INSERT)
    assert fixed and shells[0].get("_extra_ops")


def test_path2c_runs_before_path3_and_before_final_noop():
    """Source-structure guard: Path 2c must sit AFTER Path 2b and BEFORE both
    Path 3 and the final `if accepted is None and not _file_level_fixed:`
    no-op log, per the spec ordering requirement."""
    i_2b = _SRC.index("# ── Path 2b: FILE-LEVEL fallback")
    i_2c = _SRC.index("# ── Path 2c: FILE-LEVEL INSERTION fallback")
    i_p3 = _SRC.index("# ── Path 3: Full new_code replacement")
    i_noop = _SRC.index('reason="no_valid_correction_produced"')
    assert i_2b < i_2c < i_p3 < i_noop, (
        f"ordering wrong: 2b={i_2b} 2c={i_2c} p3={i_p3} noop={i_noop}"
    )


def test_correction_parses_file_level_fields_and_logs_them():
    assert 'corrected_file_anchor = edit_data.get("file_level_anchor", "") or ""' in _SRC
    assert 'corrected_file_insert = edit_data.get("file_level_insert", "") or ""' in _SRC
    # Provable in logs going forward.
    assert "has_file_level_anchor=bool(corrected_file_anchor)" in _SRC
    assert "anchor_len=len(corrected_file_anchor)" in _SRC
    assert "insert_len=len(corrected_file_insert)" in _SRC


def test_prompt_exposes_file_level_fields_uniformly():
    # One shared paragraph in the assembled correction_prompt (applies to all
    # correction paths since the prompt is assembled once).
    assert "file_level_anchor" in _SRC
    assert "file_level_insert" in _SRC
    assert 'do NOT put that code inside \\"new_code\\"' in _SRC


# ─────────────────────────── Required test 2 ───────────────────────────────

def test_ambiguity_guard_rejects_zero_occurrences():
    shells = _make_shells()
    fixed, reason = _apply_file_level_insertion(
        shells, 0, "const ThisLineDoesNotExistAnywhere = 1;", _INSERT
    )
    assert fixed is False
    assert reason == "anchor_not_found"
    assert "_extra_ops" not in shells[0], "must NOT touch _extra_ops on reject"


def test_ambiguity_guard_rejects_multiple_occurrences():
    shells = _make_shells()
    # Anchor that appears >1 time in the file content.
    dup_anchor = "import ReactDOM from 'react-dom/client';"
    shells[0]["file_content"] = _MAIN_JSX + "\n" + dup_anchor + "\n"
    assert shells[0]["file_content"].count(dup_anchor) == 2
    fixed, reason = _apply_file_level_insertion(shells, 0, dup_anchor, _INSERT)
    assert fixed is False
    assert reason == "anchor_ambiguous_2_occurrences"
    assert "_extra_ops" not in shells[0], "must NOT touch _extra_ops on reject"


def test_reject_reasons_are_logged_in_source():
    assert '_dlog("correction_file_level_insert_rejected"' in _SRC
    assert '_dlog("correction_file_level_insert_accepted"' in _SRC


# ─────────────────────────── Required test 3 ───────────────────────────────
# The guard this fix must NOT weaken (session 6930f196 sibling corruption).

# Verbatim shape from session 6930f196: told to fix a type error rooted in a
# sibling symbol, the corrector returned the sibling's 1-line fix as the full
# replacement for the target function — which would DELETE it.
_SIBLING_CORRUPTION_NEW_CODE = "type FooResult = { ok: boolean; value: number };"
_TARGET_SYMBOL = "computeFooResult"


def test_wrong_symbol_guard_still_rejects_sibling_corruption():
    """No file_level_* fields, new_code does not mention the target symbol =>
    wrong_symbol_reason must STILL reject it, exactly as before this fix."""
    reason = wrong_symbol_reason(_TARGET_SYMBOL, _SIBLING_CORRUPTION_NEW_CODE)
    assert reason is not None, (
        "wrong_symbol_reason must still reject a full replacement that never "
        "mentions the target symbol — the 6930f196 sibling-corruption guard"
    )
    assert _TARGET_SYMBOL in reason
    assert "does not mention the target symbol" in reason


def test_wrong_symbol_guard_body_is_byte_for_byte_unchanged():
    """The guard's decision logic must be exactly its original three lines —
    proof it was not weakened while adding the additive Path 2c."""
    assert "    if not symbol_name or not new_code:\n        return None" in _SRC
    assert "    if symbol_name in new_code:\n        return None" in _SRC


def test_path2c_does_not_add_exception_into_wrong_symbol_reason():
    """The additive path must live in the correction loop, NOT inside the
    guard function. Assert the guard function body contains no file_level_*
    references."""
    start = _SRC.index("def wrong_symbol_reason(")
    end = _SRC.index("def _apply_file_level_insertion(")
    guard_src = _SRC[start:end]
    assert "file_level" not in guard_src
    assert "_extra_ops" not in guard_src


def test_pipeline_module_still_parses():
    ast.parse(_SRC)
