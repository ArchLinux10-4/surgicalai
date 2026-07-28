"""
Regression tests for the SILENT-EVAPORATION bug in the symbol-correction
loop (session e3f0e267-d18a-497b-8049-195615e291ab).

Root cause (proven from the real debug trace,
``surgical_debug_e3f0e267.jsonl``, 2026-07-28 08:09 run): the plan queued
two edits to ``topPaidJobsRoutes.js::module.exports`` that failed to
anchor. The correction loop asked Claude to fix them; Claude replied with
a ``<search_request>``, the search ran, and the follow-up call replied
with a ``<file_request>`` instead of an edit block (verbatim from the
trace):

    "I still don't have the actual route handler code inside
    `topPaidJobsRoutes.js` itself — my search only returned matches from
    other files. Since it's a small file, let me pull it directly by name
    rather than guess its anchor text.

    <file_request>
    topPaidJobsRoutes.js
    </file_request>"

Before the fix, NOTHING handled a `<file_request>` reply at this point in
the loop (only `<search_request>` and real edit blocks were handled). The
request was silently dropped, `pending_edits` stayed empty for the next
round, and because `still_unresolved` is rebuilt each round strictly by
iterating over the previous round's `pending_edits`, an empty
`pending_edits` produced an empty `still_unresolved` — which the loop's
`if not still_unresolved` check reads as "everything is resolved". The
trace confirms the exact outcome:

    resolution_summary   resolved_count=0 skipped_count=0
                          resolved_symbols=[] skipped_details=[]
    qa_phase_start        change_count=0
    degenerate_drop       edit_blocks=2 skipped_changes=[]
    sse_stream_done

Two originally-planned edits vanished with zero skip messages, zero
errors, and zero trace for the user — the user's session ended with an
incomplete result and no explanation.

The fix has two independent, defense-in-depth layers:

  1. Give the model a REAL chance to finish the job: a `<file_request>`
     reply (at every point in the correction round where it can appear —
     the initial `correction_response`, the first search follow-up, and
     the second follow-up) now fetches the actual file content via
     `_fetch_requested_files_context` and makes one more Claude call
     with that content, extracting edit blocks via `_extract_edit_blocks`.

  2. A safety net: even if every retry still produces no edit, the round
     no longer silently launders unresolved items into "nothing left to
     do". If `still_unresolved` is non-empty and the round produced no
     new `pending_edits`, those items are explicitly finalized into
     `skipped_messages` / `skipped_changes_struct` with a
     `correction_no_edit_produced` reason and a `correction_round_produced_no_edit`
     debug event — visible to the user instead of vanishing.

These tests exercise the two new helper functions with the real captured
follow-up text from the trace, and assert (via source inspection) that
the safety-net guard is unconditionally reachable — not just added inside
a branch that can be skipped the same way the original bug was.
"""
import pathlib
import re

from services.pipeline import _extract_edit_blocks, _fetch_requested_files_context

_SRC_PATH = pathlib.Path(__file__).parent.parent / "services" / "pipeline.py"
_SRC = _SRC_PATH.read_text()

# ── Verbatim follow-up response text from the real trace ────────────────────
_REAL_FILEREQ_FOLLOWUP = (
    "I still don't have the actual route handler code inside "
    "`topPaidJobsRoutes.js` itself — my search only returned matches from "
    "other files. Since it's a small file, let me pull it directly by name "
    "rather than guess its anchor text.\n\n"
    "<file_request>\ntopPaidJobsRoutes.js\n</file_request>"
)

# A small but faithful stand-in for topPaidJobsRoutes.js's real shape (the
# original file's exact bytes weren't captured in the trace beyond grep
# snippets, but the module.exports tail — the symbol both queued edits
# targeted — is the load-bearing part for this test).
_TOP_PAID_JOBS_ROUTES_JS = """\
const express = require('express');
const router = express.Router();

router.get('/api/top-paid-jobs', async (req, res) => {
  const { Type } = req.query;
  res.json({ ok: true, type: Type });
});

module.exports = router;
"""


class TestExtractEditBlocks:
    def test_no_edit_blocks_in_file_request_reply(self):
        # This is the exact reply that triggered the bug: no <surgical_edit>
        # block at all, only a <file_request>.
        assert _extract_edit_blocks(_REAL_FILEREQ_FOLLOWUP) == []

    def test_extracts_single_edit_block(self):
        text = (
            "here you go\n<surgical_edit>{\"a\": 1}</surgical_edit>\ndone"
        )
        assert _extract_edit_blocks(text) == ['{"a": 1}']

    def test_extracts_multiple_edit_blocks_in_order(self):
        text = (
            "<surgical_edit>first</surgical_edit>"
            "middle text"
            "<surgical_edit>second</surgical_edit>"
        )
        assert _extract_edit_blocks(text) == ["first", "second"]


class TestFetchRequestedFilesContext:
    def test_fulfills_real_file_request_from_trace_exact_match(self):
        lookup = {"topPaidJobsRoutes.js": _TOP_PAID_JOBS_ROUTES_JS}
        ctx = _fetch_requested_files_context(_REAL_FILEREQ_FOLLOWUP, lookup)
        assert "topPaidJobsRoutes.js" in ctx
        assert "module.exports = router;" in ctx
        assert "FILE NOT FOUND" not in ctx

    def test_fuzzy_filename_fallback(self):
        # Lookup key has a path prefix the model's bare filename doesn't —
        # must still resolve via the substring fallback.
        lookup = {"backend/routers/topPaidJobsRoutes.js": _TOP_PAID_JOBS_ROUTES_JS}
        ctx = _fetch_requested_files_context(_REAL_FILEREQ_FOLLOWUP, lookup)
        assert "module.exports = router;" in ctx
        assert "FILE NOT FOUND" not in ctx

    def test_file_not_found_reports_clearly_instead_of_empty_string(self):
        lookup = {"someOtherFile.js": "content"}
        ctx = _fetch_requested_files_context(_REAL_FILEREQ_FOLLOWUP, lookup)
        assert "FILE NOT FOUND: 'topPaidJobsRoutes.js'" in ctx

    def test_no_file_request_tag_returns_empty(self):
        assert _fetch_requested_files_context("no tags here", {"a": "b"}) == ""


class TestSourceHandlesFileRequestAtEveryReplyPoint:
    """
    Before the fix, only <search_request> and real edit blocks were
    handled at each of these three reply points. Assert the
    <file_request> branch now exists at each one, using the exact
    fulfillment helper (not a copy-pasted reimplementation).
    """

    def test_initial_correction_response_handles_file_request(self):
        assert 'elif "<file_request>" in corr_text:' in _SRC

    def test_first_followup_handles_file_request(self):
        assert '"<file_request>" in _fu_text' in _SRC

    def test_all_three_reply_points_use_the_shared_fetch_helper(self):
        assert _SRC.count("_fetch_requested_files_context(") >= 3


class TestSilentEvaporationSafetyNet:
    """
    Assert the safety-net guard exists, fires unconditionally after a
    correction round (not nested inside the try/except that the original
    bug's failure path could skip), and produces a distinct, user-visible
    skip reason instead of silently zeroing out `still_unresolved`.
    """

    def _find_guard_block(self) -> str:
        marker = "Silent-evaporation guard"
        idx = _SRC.index(marker)
        # Grab a generous window after the marker covering the whole guard.
        return _SRC[idx: idx + 2200]

    def test_guard_exists(self):
        assert "Silent-evaporation guard" in _SRC

    def test_guard_checks_unresolved_items_with_no_new_pending_edits(self):
        block = self._find_guard_block()
        assert "if still_unresolved and not pending_edits:" in block

    def test_guard_produces_distinct_skip_reason(self):
        block = self._find_guard_block()
        assert '"reason": "correction_no_edit_produced"' in block

    def test_guard_emits_a_debug_event_for_observability(self):
        block = self._find_guard_block()
        assert '_dlog("correction_round_produced_no_edit"' in block

    def test_guard_is_not_nested_inside_the_except_handler(self):
        """
        The pre-existing `except Exception as _corr_fail:` handler already
        finalized still_unresolved items on a hard exception. The bug was
        that a *non-exceptional* empty-pending-edits outcome had no
        equivalent handling. Assert the new guard sits at the same
        indentation as (i.e. as a sibling of, not nested under) that
        except block, so it fires on the normal/no-exception path too.
        """
        except_idx = _SRC.index("except Exception as _corr_fail:")
        guard_idx = _SRC.index("Silent-evaporation guard")
        assert guard_idx > except_idx

        except_line = _SRC[:except_idx].rsplit("\n", 1)[-1]
        guard_marker_line_start = _SRC.rfind("\n", 0, guard_idx) + 1
        guard_indent_line = _SRC[guard_marker_line_start:guard_idx]

        except_indent = len(except_line) - len(except_line.lstrip(" "))
        guard_indent = len(guard_indent_line) - len(guard_indent_line.lstrip(" "))
        assert guard_indent == except_indent, (
            f"guard indent ({guard_indent}) must match the except block's "
            f"indent ({except_indent}) so it runs as a sibling statement, "
            "not accidentally nested where an early return/continue could "
            "skip it again"
        )

    def test_guard_appears_before_surgicalchange_assembly(self):
        assembly_idx = _SRC.index("Build SurgicalChange objects with parallel QA")
        guard_idx = _SRC.index("Silent-evaporation guard")
        assert guard_idx < assembly_idx


def test_pipeline_module_still_parses():
    import ast
    ast.parse(_SRC)
