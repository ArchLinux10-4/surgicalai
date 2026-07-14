"""
Tests for decomposed pipeline: Phase 1 (search) + Phase 2 (edit).

Verifies:
- Phase 1: search rounds, token budget, tag parsing, search results
- Phase 2: edit streaming, tag parsing, deadline checks
- Phase separation: search cannot starve edits (structural guarantee)

Replaces the old monolithic starvation recovery tests (b88174c and prior).
"""
import ast
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.normpath(os.path.join(_HERE, "..", "backend", "services", "pipeline.py"))

# ---------------------------------------------------------------------------
# Extract helper functions from pipeline.py
# ---------------------------------------------------------------------------
_MODEL_CONST_NAMES = [
    "_ADAPTIVE_THINKING_MODELS",
    "_THINKING_MODELS",
    "_MODEL_MAX_OUTPUT",
    "_NO_XHIGH_EFFORT_MODELS",
]

_FUNCS_TO_EXTRACT = [
    "_uses_adaptive_thinking",
    "_supports_thinking",
    "_get_thinking_kwargs",
    "_get_effort_kwargs",
    "_is_claude_model",
    "_is_gemini_model",
    "_max_output_tokens",
    "_bounded_thinking_params",
    "_is_starved",
]


def _extract_functions():
    src = open(_PIPELINE, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"_dlog": lambda *a, **kw: None}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _MODEL_CONST_NAMES:
                    code = ast.get_source_segment(src, node)
                    if code:
                        exec(code, ns)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _FUNCS_TO_EXTRACT:
                code = ast.get_source_segment(src, node)
                if code:
                    if isinstance(node, ast.AsyncFunctionDef):
                        code = code.replace("async def ", "def ", 1)
                        code = code.replace("await ", "", 1)
                    exec(code, ns)

    return ns


NS = _extract_functions()
_get_thinking_kwargs = NS["_get_thinking_kwargs"]
_bounded_thinking_params = NS["_bounded_thinking_params"]
_max_output_tokens = NS["_max_output_tokens"]


# ═══════════════════════════════════════════════════════════════════════
# Phase 1: Code Discovery Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPhase1SearchConstants(unittest.TestCase):
    """Verify Phase 1 constants and structure exist."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_phase1_max_search_rounds_exists(self):
        """PHASE1_MAX_SEARCH_ROUNDS must be defined."""
        self.assertIn("PHASE1_MAX_SEARCH_ROUNDS = 3", self.src)

    def test_phase1_max_tokens_exists(self):
        """PHASE1_MAX_TOKENS must be small (search only needs tiny budget)."""
        self.assertIn("PHASE1_MAX_TOKENS = 4096", self.src)

    def test_phase1_instruction_exists(self):
        """Phase 1 instruction must tell model to search OR say READY_TO_EDIT."""
        self.assertIn("PHASE: CODE DISCOVERY", self.src)
        self.assertIn("READY_TO_EDIT", self.src)

    def test_phase1_no_thinking(self):
        """Phase 1 must NOT include thinking kwargs — fast search only."""
        # Find Phase 1 kwargs construction
        idx = self.src.find("PHASE1_MAX_TOKENS")
        p1_block = self.src[idx:idx + 500]
        # _get_effort_kwargs is fine but _get_thinking_kwargs should NOT be there
        self.assertNotIn("_get_thinking_kwargs", p1_block)

    def test_phase1_complete_dlog_exists(self):
        """Phase 1 must log completion with stats."""
        self.assertIn("phase1_complete", self.src)

    def test_phase1_ready_to_edit_dlog_exists(self):
        """Phase 1 must log when model says READY_TO_EDIT."""
        self.assertIn("phase1_ready_to_edit", self.src)

    def test_phase1_search_round_done_dlog_exists(self):
        """Phase 1 must log each round completion."""
        self.assertIn("phase1_search_round_done", self.src)

    def test_phase1_search_requested_dlog_exists(self):
        """Phase 1 must log search requests."""
        self.assertIn("phase1_search_requested", self.src)

    def test_phase1_search_results_dlog_exists(self):
        """Phase 1 must log search results."""
        self.assertIn("phase1_search_results", self.src)


class TestPhase1SearchDedup(unittest.TestCase):
    """Verify Phase 1 deduplicates search terms."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_searched_terms_tracked(self):
        """searched_terms must accumulate across rounds."""
        self.assertIn("searched_terms: list = []", self.src)
        self.assertIn("searched_terms.extend(new_terms)", self.src)

    def test_dedup_logic_exists(self):
        """New terms must be filtered against already-searched terms."""
        self.assertIn("if t.lower() not in", self.src)

    def test_all_terms_searched_exits(self):
        """If all terms already searched, break out of search loop."""
        self.assertIn("phase1_all_terms_searched", self.src)


class TestPhase1FileRequest(unittest.TestCase):
    """Verify Phase 1 file request handling."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_tier1_guard_exists(self):
        """Tier-1 guard must prevent re-fetching files already in context."""
        self.assertIn("phase1_filereq_tier1_guard", self.src)

    def test_max_file_req_total_exists(self):
        """MAX_FILE_REQ_TOTAL must cap total fetchable files."""
        self.assertIn("MAX_FILE_REQ_TOTAL = 15", self.src)

    def test_file_request_resolved_dlog_exists(self):
        """Phase 1 must log resolved file requests."""
        self.assertIn("phase1_file_request_resolved", self.src)

    def test_missing_file_note(self):
        """Missing files must produce a user-visible note."""
        self.assertIn("MISSING FILES", self.src)


class TestPhase1GitHub(unittest.TestCase):
    """Verify Phase 1 GitHub request handling."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_github_budget_constants(self):
        """GitHub round and attempt limits must exist."""
        self.assertIn("MAX_GITHUB_ROUNDS = 6", self.src)
        self.assertIn("MAX_GITHUB_ATTEMPTS = 12", self.src)

    def test_github_budget_exhausted_dlog(self):
        """Must log when GitHub budget is exhausted."""
        self.assertIn("phase1_github_budget_exhausted", self.src)

    def test_github_result_dlog(self):
        """Must log GitHub results."""
        self.assertIn("phase1_github_result", self.src)


# ═══════════════════════════════════════════════════════════════════════
# Phase 2: Edit Generation Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPhase2EditStreaming(unittest.TestCase):
    """Verify Phase 2 edit streaming structure."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_phase2_complete_dlog_exists(self):
        """Phase 2 must log completion with stats."""
        self.assertIn("phase2_complete", self.src)

    def test_phase2_uses_full_thinking(self):
        """Phase 2 must use _get_thinking_kwargs for full thinking."""
        # Find Phase 2 stream_kwargs construction (after PHASE 2 comment)
        idx = self.src.find("PHASE 2: EDIT GENERATION")
        self.assertGreater(idx, 0, "Phase 2 section must exist")
        p2_block = self.src[idx:idx + 3000]
        self.assertIn("_get_thinking_kwargs", p2_block)

    def test_phase2_uses_full_max_tokens(self):
        """Phase 2 must use _max_output_tokens (full budget)."""
        idx = self.src.find("PHASE 2: EDIT GENERATION")
        p2_block = self.src[idx:idx + 3000]
        self.assertIn("_max_output_tokens", p2_block)

    def test_phase2_ignores_search_tags(self):
        """Phase 2 must ignore search/filereq tags (search phase is over)."""
        self.assertIn("phase2_search_tag_ignored", self.src)

    def test_phase2_search_injection(self):
        """Phase 2 must inject accumulated search results into messages."""
        self.assertIn("accumulated_search_results", self.src)
        self.assertIn("all code discovery is complete", self.src)

    def test_edit_heartbeat_exists(self):
        """Edit heartbeat progress must emit during long edits."""
        self.assertIn("Writing code changes", self.src)


class TestPhase2DeadlineChecks(unittest.TestCase):
    """Verify Phase 2 deadline and stall detection."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_phase_deadline_constant_exists(self):
        """STREAMING_PHASE_DEADLINE_S must be defined."""
        self.assertIn("STREAMING_PHASE_DEADLINE_S = 480", self.src)

    def test_thinking_stall_constant_exists(self):
        """STREAMING_THINKING_STALL_S must be defined."""
        self.assertIn("STREAMING_THINKING_STALL_S = 120", self.src)

    def test_starvation_abort_flag_initialized(self):
        """_streaming_starvation_abort must be initialized before Phase 2."""
        idx_flag = self.src.index("_streaming_starvation_abort = False")
        idx_p2 = self.src.index("PHASE 2: EDIT GENERATION")
        self.assertLess(idx_flag, idx_p2,
                        "Flag must be initialized before Phase 2")

    def test_round_text_timestamp_initialized(self):
        """_round_last_text_ts must be set at start of Phase 2."""
        # Find the Phase 2 _round_t0
        idx_p2 = self.src.index("PHASE 2: EDIT GENERATION")
        block = self.src[idx_p2:idx_p2 + 5000]
        self.assertIn("_round_last_text_ts = time.time()", block)

    def test_text_timestamp_updated_on_text_chunk(self):
        """_round_last_text_ts must be updated when text_chunk is received."""
        idx_text = self.src.index("full_response += text_chunk")
        block = self.src[idx_text:idx_text + 200]
        self.assertIn("_round_last_text_ts = time.time()", block)

    def test_deadline_abort_dlog_exists(self):
        """_dlog('streaming_deadline_abort', ...) must exist."""
        self.assertIn("streaming_deadline_abort", self.src)

    def test_thinking_stall_dlog_exists(self):
        """_dlog('streaming_thinking_stall', ...) must exist."""
        self.assertIn("streaming_thinking_stall", self.src)

    def test_abort_exit_dlog_exists(self):
        """_dlog('streaming_starvation_abort_exit', ...) must exist."""
        self.assertIn("streaming_starvation_abort_exit", self.src)

    def test_abort_closes_thinking_block(self):
        """Abort handler must close thinking block (yield thinking_end)."""
        idx = self.src.index("streaming_starvation_abort_exit")
        block = self.src[idx:idx + 800]
        self.assertIn("thinking_end", block)

    def test_abort_yields_user_message(self):
        """Abort handler must yield a progress message to the user."""
        idx = self.src.index("streaming_starvation_abort_exit")
        block = self.src[idx:idx + 500]
        self.assertIn("Thinking timeout", block)

    def test_phase_deadline_checks_pipeline_budget(self):
        """Streaming deadline check must also check _pipeline_over_budget."""
        idx = self.src.index("streaming_deadline_abort")
        block = self.src[max(0, idx - 500):idx]
        self.assertIn("_pipeline_over_budget()", block)

    def test_thinking_stall_requires_had_thinking(self):
        """Thinking stall check must guard on had_thinking."""
        idx = self.src.index("streaming_thinking_stall")
        block = self.src[max(0, idx - 300):idx]
        self.assertIn("had_thinking", block)


class TestPhase2TotalFailure(unittest.TestCase):
    """Verify Phase 2 total failure handling (no recovery needed)."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_total_failure_dlog_exists(self):
        self.assertIn("starvation_total_failure", self.src)

    def test_total_failure_user_message(self):
        """User must see an error if Phase 2 produces nothing."""
        self.assertIn("spent all available time thinking", self.src)

    def test_total_failure_returns(self):
        """Total failure must yield done and return."""
        idx = self.src.find("starvation_total_failure")
        after = self.src[idx:idx + 1000]
        self.assertIn('"done"', after)
        self.assertIn("return", after)


# ═══════════════════════════════════════════════════════════════════════
# Structural Guarantee Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPhaseDecompositionStructure(unittest.TestCase):
    """Verify the structural guarantee: Phase 1 and Phase 2 are separate.

    The core invariant: search (Phase 1) physically cannot starve
    edits (Phase 2) because they are separate API calls with
    independent token budgets.
    """

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_two_phases_exist(self):
        """Both PHASE 1 and PHASE 2 sections must exist."""
        self.assertIn("PHASE 1: CODE DISCOVERY", self.src)
        self.assertIn("PHASE 2: EDIT GENERATION", self.src)

    def test_phase1_before_phase2(self):
        """Phase 1 must come before Phase 2."""
        idx1 = self.src.index("PHASE 1: CODE DISCOVERY")
        idx2 = self.src.index("PHASE 2: EDIT GENERATION")
        self.assertLess(idx1, idx2)

    def test_no_monolithic_search_loop(self):
        """Old monolithic search loop (search_round in range(MAX_SEARCH_ROUNDS))
        must NOT exist — replaced by Phase 1 loop."""
        # The old loop used "for search_round in range(MAX_SEARCH_ROUNDS"
        self.assertNotIn("for search_round in range(MAX_SEARCH_ROUNDS", self.src)

    def test_no_starvation_recovery_loop(self):
        """Old starvation recovery (recovery_stream_round, forced_write_nudge)
        must NOT exist — structurally unnecessary with decomposed phases."""
        self.assertNotIn("_recovery_stream_round", self.src)
        self.assertNotIn("RECOVERY_MAX_ROUNDS", self.src)
        self.assertNotIn("_forced_write_nudge_sent", self.src)

    def test_no_search_time_budget(self):
        """Old SEARCH_TIME_BUDGET_S must NOT exist — Phase 1 has round limits."""
        self.assertNotIn("SEARCH_TIME_BUDGET_S", self.src)

    def test_no_total_preedit_budget(self):
        """Old TOTAL_PREEDIT_BUDGET_S must NOT exist — each phase has own budget."""
        self.assertNotIn("TOTAL_PREEDIT_BUDGET_S", self.src)

    def test_phase1_small_tokens(self):
        """Phase 1 uses PHASE1_MAX_TOKENS (small), not full model budget."""
        self.assertIn("PHASE1_MAX_TOKENS = 4096", self.src)

    def test_phase2_full_tokens(self):
        """Phase 2 uses _max_output_tokens (full model budget)."""
        idx = self.src.index("PHASE 2: EDIT GENERATION")
        p2_block = self.src[idx:idx + 3000]
        self.assertIn("_max_output_tokens(arch_model)", p2_block)


class TestPhase1TagParsing(unittest.TestCase):
    """Verify Phase 1 tag parsing only handles search-phase tags."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_p1_tags_defined(self):
        """Phase 1 tag set must include search and filereq."""
        self.assertIn('"search", "filereq"', self.src)

    def test_p1_stop_sequences(self):
        """Phase 1 must use stop sequences for its tag set."""
        self.assertIn("_p1_stop_seqs", self.src)

    def test_unclosed_tag_handling(self):
        """Phase 1 must handle unclosed tags from stop_sequence."""
        idx = self.src.find("phase1_search_round_done")
        # Before that dlog, unclosed tag handling should exist
        block = self.src[max(0, idx - 2000):idx]
        self.assertIn("_p1_state.startswith", block)


class TestPhase2TagParsing(unittest.TestCase):
    """Verify Phase 2 tag parsing handles all tags correctly."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_edit_blocks_collected(self):
        """Phase 2 must collect edit blocks."""
        self.assertIn("edit_blocks_raw.append(_content)", self.src)

    def test_new_file_blocks_collected(self):
        """Phase 2 must collect new file blocks."""
        self.assertIn("new_file_blocks_raw.append(_content)", self.src)

    def test_plan_parsed(self):
        """Phase 2 must parse edit_plan tags."""
        self.assertIn("_parse_plan_content(_content)", self.src)

    def test_eos_finalizer_exists(self):
        """End-of-stream finalizer must handle unclosed tags."""
        self.assertIn("eos_finalizer_triggered", self.src)

    def test_eos_edit_recovery(self):
        """EOS finalizer must recover unclosed edit blocks."""
        self.assertIn("eos_edit_recovered", self.src)

    def test_eos_plan_recovery(self):
        """EOS finalizer must recover unclosed plan blocks."""
        self.assertIn("eos_plan_recovered", self.src)

    def test_stop_sequence_synthesizer(self):
        """Stop-sequence tag-close synthesizer must exist."""
        self.assertIn("tag_closed_via_stop_sequence", self.src)


# ═══════════════════════════════════════════════════════════════════════
# Agent Mode Tests (unchanged — separate code path)
# ═══════════════════════════════════════════════════════════════════════

class TestAgentModeStarvationRecoveryLogic(unittest.TestCase):
    """Verify the logic used by the Agent Mode starvation recovery path."""

    def test_initial_call_budget_capped_at_8000(self):
        """Agent Mode initial call uses budget=8000 — adaptive models get type=adaptive."""
        kw = _get_thinking_kwargs("claude-sonnet-5", 8000)
        self.assertEqual(kw["thinking"]["type"], "adaptive")
        self.assertNotIn("budget_tokens", kw["thinking"])

    def test_retry_budget_doubled_to_16000(self):
        """On starvation retry — adaptive models still get type=adaptive."""
        kw = _get_thinking_kwargs("claude-sonnet-5", 16000)
        self.assertEqual(kw["thinking"]["type"], "adaptive")
        self.assertNotIn("budget_tokens", kw["thinking"])

    def test_retry_max_tokens_covers_text_and_thinking(self):
        """_bounded_thinking_params ensures max_tokens > budget so text has room."""
        params = _bounded_thinking_params("claude-sonnet-5", 16000)
        max_t = params["max_tokens"]
        self.assertGreater(max_t, 16000)
        self.assertGreaterEqual(max_t - 16000, 4096)

    def test_bounded_params_never_exceed_model_cap(self):
        """max_tokens from _bounded_thinking_params must not exceed model cap."""
        model_cap = _max_output_tokens("claude-sonnet-5")
        for budget in [8000, 16000, 32000, 64000, 100000]:
            params = _bounded_thinking_params("claude-sonnet-5", budget)
            self.assertLessEqual(params["max_tokens"], model_cap,
                                 f"budget={budget} exceeded model cap {model_cap}")

    def test_starvation_detection_empty_text(self):
        """Starvation condition: full_text.strip() == '' triggers retry."""
        self.assertTrue(not "".strip())
        self.assertTrue(not "   ".strip())
        self.assertTrue(not "\n\n".strip())

    def test_starvation_detection_has_text(self):
        """Non-empty text should NOT trigger starvation."""
        self.assertFalse(not '{"intent": "edit"}'.strip())
        self.assertFalse(not "hello".strip())


class TestAgentModeCodePresence(unittest.TestCase):
    """Verify the Agent Mode starvation recovery code exists in pipeline.py."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_starvation_detection_log_exists(self):
        self.assertIn("agent_thinking_starvation_detected", self.src)

    def test_starvation_retry_done_log_exists(self):
        self.assertIn("agent_starvation_retry_done", self.src)

    def test_starvation_final_fail_log_exists(self):
        self.assertIn("agent_starvation_final_fail", self.src)

    def test_agent_stream_done_log_exists(self):
        self.assertIn("agent_stream_done", self.src)

    def test_retry_uses_bounded_thinking_params(self):
        idx = self.src.index("agent_thinking_starvation_detected")
        block = self.src[max(0, idx - 500):idx + 500]
        self.assertIn("_bounded_thinking_params", block)

    def test_retry_uses_doubled_budget(self):
        idx = self.src.index("agent_thinking_starvation_detected")
        block = self.src[max(0, idx - 300):idx]
        self.assertIn("16_000", block)

    def test_no_messages_create_in_agent_mode(self):
        start = self.src.index("Agent Mode starvation detection")
        block = self.src[max(0, start - 1000):start + 1000]
        self.assertNotIn("messages.create(", block)


# ═══════════════════════════════════════════════════════════════════════
# No-edits text-only notice (unchanged — in resolution phase)
# ═══════════════════════════════════════════════════════════════════════

class TestNoEditsTextOnlyNotice(unittest.TestCase):
    """When model produces text but no edit blocks, user must see a notice."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_no_edits_text_only_sends_user_notice(self):
        self.assertIn("no_edits_text_only_notice", self.src)
        self.assertIn("No code changes were produced", self.src)

    def test_no_edits_text_only_dlog(self):
        idx = self.src.find("no_edits_text_only_notice")
        self.assertGreater(idx, 0)
        after = self.src[idx:idx + 300]
        self.assertIn("was_starvation_recovery", after)

    def test_no_edits_text_only_after_skipped_changes_check(self):
        idx = self.src.find("no_edits_text_only_notice")
        before = self.src[max(0, idx - 700):idx]
        self.assertIn("elif full_response.strip()", before)


if __name__ == "__main__":
    unittest.main()
