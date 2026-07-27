"""
Regression tests for verifying `<cannot_anchor>` claims before trusting them
(session e58cdb54-c7b6-4352-9fc1-daeb4a42ef8e).

Root cause (fixed): when a correction model replies `<cannot_anchor
reason="..."/>`, the pipeline treated it as an honest, unverifiable "clean
abort" and gave up on the symbol for the round — with NO check against the
real file. This let a false claim of "the code already exists" permanently
block a fix QA had correctly flagged as missing, and (separately) meant the
next retry round had zero memory that this dead end was already tried.

Proven verbatim in the trace (BRAND symbol, StatutoryCostsModal.jsx):
  qa_agent_parsed        symbol=BRAND verdict=blocked qa_score=2
      summary="...NEW CODE is identical to ORIGINAL CODE with no imports
               added."
  qa_retry_correction_search_executed  terms=["import", "@mui/icons-material"]
      result_chars=7557   (search results DID show the real, current import
                           list, which does not contain PrintIcon /
                           RequestQuoteIcon / SwapHorizIcon)
  qa_retry_correction_clean_abort  response_preview="Good news — the imports
      already exist in the file (lines 52-54). No change is actually needed
      here.\\n\\n<cannot_anchor reason=\"PrintIcon, RequestQuoteIcon, and
      SwapHorizIcon imports already exist in the file at lines 52-54...\"/>"

Independently verified against the uploaded StatutoryCostsModal.jsx: none of
PrintIcon / RequestQuoteIcon / SwapHorizIcon appear anywhere in the file —
the claim was false. The next retry round (round 1) then re-submitted
IDENTICAL code and was rejected again, because `_correction_history` had
zero entries for round 0 (the `continue` on the no-edit path skipped the
history-recording block entirely) — so the model had no memory it had
already gone down this dead end.

This fix adds:
  1. `_verify_cannot_anchor_claim()` — greps the file's real current text for
     any identifier the model's reason and the QA issue text both mention,
     and reports ones with zero real occurrences.
  2. A rejection + one bounded corrective re-prompt when the claim is
     provably false, with hard grep-count proof handed back to the model.
  3. A correction-history entry recorded for EVERY non-edit round (clean
     abort or no-edit), not just accepted/rejected code attempts, so later
     rounds are never blind to what was already tried.
"""
import ast
import pathlib
import re

from services.pipeline import _verify_cannot_anchor_claim

_SRC_PATH = pathlib.Path(__file__).parent.parent / "services" / "pipeline.py"
_SRC = _SRC_PATH.read_text()

# ── Faithful reconstruction of the real StatutoryCostsModal.jsx import block ──
_REAL_FILE_TEXT = """\
import React, { useState, useEffect, useCallback, useRef } from 'react';
import AccountBalanceIcon  from '@mui/icons-material/AccountBalance';
import InfoOutlinedIcon    from '@mui/icons-material/InfoOutlined';
import EditOutlinedIcon    from '@mui/icons-material/EditOutlined';
import WarningAmberIcon    from '@mui/icons-material/WarningAmber';
import CloseIcon           from '@mui/icons-material/Close';
import WorkIcon            from '@mui/icons-material/Work';
import MonetizationOnIcon  from '@mui/icons-material/MonetizationOn';
import LockOutlinedIcon    from '@mui/icons-material/LockOutlined';
import BusinessCenterIcon  from '@mui/icons-material/BusinessCenter';
import PublicIcon          from '@mui/icons-material/Public';
import LockIcon            from '@mui/icons-material/Lock';
import SearchIcon          from '@mui/icons-material/Search';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import FlagIcon            from '@mui/icons-material/Flag';

const BRAND   = '#29c0db';
"""

# Verbatim (truncated to the model's actual reason attribute) from the trace.
_REAL_REASON = (
    "PrintIcon, RequestQuoteIcon, and SwapHorizIcon imports already exist "
    "in the file at lines 52-54, and the BRAND constant is already correct. "
    "There is nothing left to change for this request."
)
_REAL_QA_SUMMARY = (
    "The plan asked to add PrintIcon, RequestQuoteIcon, and SwapHorizIcon "
    "imports above the BRAND constant, but NEW CODE is identical to ORIGINAL "
    "CODE\u2014no imports were added."
)


def test_regression_e58cdb54_detects_the_real_hallucination():
    """The exact claim from the trace must be provably rejected: none of the
    three named icons are actually anywhere in the real file."""
    hallucinated = _verify_cannot_anchor_claim(
        _REAL_REASON, _REAL_QA_SUMMARY, _REAL_FILE_TEXT
    )
    assert set(hallucinated) == {"PrintIcon", "RequestQuoteIcon", "SwapHorizIcon"}, (
        f"expected all three missing icons flagged, got {hallucinated}"
    )
    # Sanity: independently confirm via plain grep, not just via the helper.
    for tok in ("PrintIcon", "RequestQuoteIcon", "SwapHorizIcon"):
        assert not re.search(r'\b' + tok + r'\b', _REAL_FILE_TEXT), (
            f"{tok} must not be in the reconstructed file for this test to be valid"
        )
    # BRAND itself really is in the file — must NOT be falsely flagged.
    assert "BRAND" not in hallucinated


def test_genuine_cannot_anchor_is_not_flagged():
    """When the claimed-existing identifiers genuinely ARE in the file, the
    verifier must not manufacture a false hallucination report."""
    reason = "FlagIcon already exists in the file, no change needed."
    qa_summary = "Add a FlagIcon import."
    hallucinated = _verify_cannot_anchor_claim(reason, qa_summary, _REAL_FILE_TEXT)
    assert hallucinated == []


def test_only_tokens_shared_between_reason_and_qa_text_are_checked():
    """A random capitalized word in the model's reason that QA never
    mentioned must not be flagged — avoids false positives on prose."""
    reason = "GoodNewsHere, nothing else to do."
    qa_summary = "Add PrintIcon import."
    hallucinated = _verify_cannot_anchor_claim(reason, qa_summary, _REAL_FILE_TEXT)
    assert hallucinated == []


def test_no_reason_or_no_file_text_returns_empty():
    assert _verify_cannot_anchor_claim("", "PrintIcon missing", _REAL_FILE_TEXT) == []
    assert _verify_cannot_anchor_claim("PrintIcon exists", "PrintIcon missing", "") == []


# ── Structural proof the fix is wired into the correction loop ──────────────

def test_hallucination_check_runs_before_clean_abort_is_trusted():
    i_cannot_anchor = _SRC.index('if "<cannot_anchor" in corr_text:')
    i_verify_call = _SRC.index("_hallucinated = _verify_cannot_anchor_claim(")
    i_clean_abort = _SRC.index('_dlog("qa_retry_correction_clean_abort"')
    assert i_cannot_anchor < i_verify_call < i_clean_abort, (
        "verification must run before the clean_abort path is taken"
    )


def test_hallucination_rejection_is_logged():
    assert '_dlog("correction_cannot_anchor_hallucination_rejected"' in _SRC


def test_every_non_edit_round_now_records_correction_history():
    """The historical gap: `if not _got_edit: ... continue` used to skip
    straight past history recording. Assert the continue now happens AFTER
    a history append, not before."""
    marker = 'if not _got_edit:'
    i_block = _SRC.index(marker)
    # Slice out this specific if-block (bounded by the next top-level
    # `if ei != -1 and ec != -1:` that follows it in source order).
    i_next = _SRC.index("if ei != -1 and ec != -1:", i_block)
    block = _SRC[i_block:i_next]
    assert '_correction_history.setdefault(idx, []).append(' in block, (
        "a non-edit round (clean_abort / no_edit_block) must still be "
        "recorded in _correction_history so later retry rounds aren't blind "
        "to what was already tried"
    )
    assert "continue" in block


def test_pipeline_module_still_parses():
    ast.parse(_SRC)
