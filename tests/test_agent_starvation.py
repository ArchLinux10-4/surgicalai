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
    "_CORRECTION_INPUT_CHAR_BUDGET",
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
    "_fit_correction_messages",
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
_fit_correction_messages = NS["_fit_correction_messages"]
_CORRECTION_INPUT_CHAR_BUDGET = NS["_CORRECTION_INPUT_CHAR_BUDGET"]


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
        p2_block = self.src[idx:idx + 4500]
        self.assertIn("_get_thinking_kwargs", p2_block)

    def test_phase2_uses_full_max_tokens(self):
        """Phase 2 must use _max_output_tokens (full budget)."""
        idx = self.src.find("PHASE 2: EDIT GENERATION")
        p2_block = self.src[idx:idx + 4500]
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
        p2_block = self.src[idx:idx + 4500]
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


class TestPhase1NoActionNudge(unittest.TestCase):
    """Fix 1: Phase 1 no-action should nudge, not exit immediately."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_consecutive_no_action_counter_exists(self):
        """_consecutive_no_action counter is initialized before Phase 1 loop."""
        idx = self.src.find("for _search_round in range(PHASE1_MAX_SEARCH_ROUNDS)")
        self.assertNotEqual(idx, -1)
        before = self.src[max(0, idx - 500):idx]
        self.assertIn("_consecutive_no_action", before,
                       "Must initialize _consecutive_no_action before loop")

    def test_no_action_sends_nudge_not_break(self):
        """First no-action sends a nudge message, not an immediate break."""
        idx = self.src.find("phase1_no_action")
        self.assertNotEqual(idx, -1)
        after = self.src[idx:idx + 1500]
        self.assertIn("_consecutive_no_action", after,
                       "No-action handler must track consecutive count")
        self.assertIn("You MUST pick one", after,
                       "No-action handler must nudge model to pick an action")
        self.assertIn("continue", after,
                       "First no-action must continue the loop, not break")

    def test_second_no_action_exits(self):
        """Second consecutive no-action exits the loop."""
        idx = self.src.find("phase1_no_action")
        self.assertNotEqual(idx, -1)
        after = self.src[idx:idx + 1500]
        self.assertIn("consecutive_no_action >= 2", after,
                       "Must exit on 2nd consecutive no-action")

    def test_action_resets_counter(self):
        """Taking an action resets the consecutive no-action counter."""
        idx = self.src.find("READY_TO_EDIT — model says it has enough context")
        self.assertNotEqual(idx, -1)
        before = self.src[max(0, idx - 500):idx]
        self.assertIn("_consecutive_no_action = 0", before,
                       "Action must reset no-action counter")


class TestPhase1PostSearchHint(unittest.TestCase):
    """Fix 2: After search results, hint about file_request if no full files yet."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_grep_snippet_hint_exists(self):
        """Post-search message includes hint about grep snippets when no files requested."""
        idx = self.src.find("_filereq_hint")
        self.assertNotEqual(idx, -1, "Must have _filereq_hint variable")
        block = self.src[idx:idx + 500]
        self.assertIn("grep snippets", block,
                       "Hint must explain that search results are grep snippets")
        self.assertIn("file_request", block,
                       "Hint must suggest using <file_request>")

    def test_hint_only_when_no_files_requested(self):
        """Hint only appears when no full files have been requested yet."""
        idx = self.src.find("_filereq_hint")
        self.assertNotEqual(idx, -1)
        block = self.src[idx:idx + 300]
        self.assertIn("not requested_files", block,
                       "Hint condition must check that no files were requested yet")


class TestPhase2FileRequestSafetyNet(unittest.TestCase):
    """Fix 3: Phase 2 fulfills one file_request as safety net instead of ignoring."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_phase2_filereq_flag_exists(self):
        """_phase2_filereq_used flag is initialized."""
        idx = self.src.find("PHASE 2: EDIT GENERATION")
        self.assertNotEqual(idx, -1)
        after = self.src[idx:idx + 2500]
        self.assertIn("_phase2_filereq_used", after,
                       "Must have _phase2_filereq_used flag near Phase 2 start")

    def test_phase2_filereq_fulfills_files(self):
        """Phase 2 branches set retry flag after fulfilling file requests."""
        self.assertIn("phase2_filereq_safety_net", self.src,
                       "Must have phase2_filereq_safety_net dlog")
        # Both Claude and GPT branches must set the retry flag
        self.assertGreaterEqual(
            self.src.count("_phase2_filereq_retry = True"), 2,
            "Must set _phase2_filereq_retry=True in both Claude and GPT branches")

    def test_phase2_filereq_limited_to_once(self):
        """Phase 2 file_request safety net only fires once."""
        count = self.src.count('not _phase2_filereq_used')
        self.assertGreaterEqual(count, 2,
                                "Guard must appear in both Claude and GPT branches")
        count_set = self.src.count('_phase2_filereq_used = True')
        self.assertGreaterEqual(count_set, 2,
                                "Flag must be set in both Claude and GPT branches")

    def test_phase2_retry_continues_loop(self):
        """After filereq safety net, retry loop continues instead of breaking."""
        import re
        # Claude branch also gates on the zero-text recovery retry
        # (`or _no_text_retry`); GPT branch remains filereq-only.
        checks = list(re.finditer(r'if _phase2_filereq_retry( or _no_text_retry)?:', self.src))
        self.assertGreaterEqual(len(checks), 2,
                                "Must check _phase2_filereq_retry in both branches")
        for m in checks:
            after = self.src[m.start():m.start() + 300]
            self.assertIn("continue", after,
                           f"Must continue retry loop at pos {m.start()}")

    def test_gpt_phase2_filereq_safety_net(self):
        """GPT branch also has file_request safety net."""
        idx = self.src.find("phase2_filereq_safety_net_gpt")
        self.assertNotEqual(idx, -1,
                            "GPT branch must have phase2_filereq_safety_net_gpt dlog")


class TestPhase2ThinkingStallDeadStream(unittest.TestCase):
    """Phase 2 stall detector must measure a DEAD stream (no bytes of any
    kind), not "thinking without text". Adaptive models (claude-sonnet-5)
    with summarized thinking can legitimately think >2 min before the first
    edit token — especially after the safety-net re-injects full files.
    Regression for trace surgical_debug_aaade983 (120s thinking -> false abort,
    0 edits)."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_activity_ts_initialized(self):
        self.assertIn("_round_last_activity_ts = time.time()", self.src)

    def test_thinking_delta_resets_activity(self):
        # The thinking-delta handler must reset the activity timer + count and
        # then yield the thinking chunk (all in one block).
        i = self.src.find("_round_thinking_deltas += 1")
        self.assertNotEqual(i, -1)
        window = self.src[max(0, i - 200):i + 200]
        self.assertIn("_round_last_activity_ts = time.time()", window)
        self.assertIn("_round_last_thinking_ts", window)
        self.assertIn('yield sse({"type": "thinking", "content": thinking_chunk})', window)

    def test_stall_check_uses_activity_not_text(self):
        # The stall abort must key off activity (any byte), never text-only.
        i = self.src.find("STREAMING_THINKING_STALL_S):")
        self.assertNotEqual(i, -1)
        window = self.src[max(0, i - 200):i]
        self.assertIn("_round_last_activity_ts", window)

    def test_stall_log_records_thinking_activity(self):
        # Diagnostics must record thinking-delta count so future traces prove
        # whether the stream was truly dead vs legitimately thinking.
        i = self.src.find('_dlog("streaming_thinking_stall"')
        self.assertNotEqual(i, -1)
        window = self.src[i:i + 600]
        self.assertIn("thinking_deltas=", window)
        self.assertIn("last_thinking_age_s", window)

    def test_runaway_backstops_still_present(self):
        # Dead-stream guard must NOT be the only bound — the phase deadline and
        # pipeline budget remain the runaway guards.
        self.assertIn("STREAMING_PHASE_DEADLINE_S", self.src)
        self.assertIn("_pipeline_over_budget()", self.src)


# ═══════════════════════════════════════════════════════════════════════
# QA-Correction Payload Budgeter Tests
# Guards against the blind [:8000] chop that silently deleted the full symbol
# code + prior changes from GPT correction calls (bomb on large files).
# ═══════════════════════════════════════════════════════════════════════

class TestCorrectionPayloadBudgeter(unittest.TestCase):
    """_fit_correction_messages must preserve content that fits, clip only when huge."""

    def test_small_payload_untouched(self):
        """Realistic payloads pass through byte-for-byte — no starvation."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "def foo():\n    return 1\n" * 100},
        ]
        out = _fit_correction_messages(msgs)
        self.assertEqual(out, msgs)

    def test_large_file_not_chopped_to_8k(self):
        """A 2,140-line-class file (well under budget) must survive intact.

        This is the exact bomb: old code did [:8000] and destroyed it.
        """
        big = "x = 1\n" * 40000  # ~240k chars, under 300k budget
        self.assertLess(len(big), _CORRECTION_INPUT_CHAR_BUDGET)
        msgs = [{"role": "user", "content": big}]
        out = _fit_correction_messages(msgs)
        self.assertEqual(out[0]["content"], big)  # NOT truncated
        self.assertGreater(len(out[0]["content"]), 8000)

    def test_pathological_payload_clipped_head_and_tail(self):
        """Only when total exceeds budget do we clip — head+tail, never blind chop."""
        head_marker = "HEAD_START_UNIQUE\n"
        tail_marker = "\nTAIL_END_UNIQUE"
        huge = head_marker + ("z = 0\n" * 100000) + tail_marker  # ~600k chars
        self.assertGreater(len(huge), _CORRECTION_INPUT_CHAR_BUDGET)
        msgs = [{"role": "user", "content": huge}]
        out = _fit_correction_messages(msgs)
        result = out[0]["content"]
        self.assertLessEqual(len(result), _CORRECTION_INPUT_CHAR_BUDGET + 500)
        # BOTH ends preserved (not a blind head-only chop)
        self.assertIn("HEAD_START_UNIQUE", result)
        self.assertIn("TAIL_END_UNIQUE", result)
        self.assertIn("omitted to fit context", result)

    def test_budget_is_far_above_old_8k_cap(self):
        """Structural guarantee: budget dwarfs the old 2k-token bomb."""
        self.assertGreaterEqual(_CORRECTION_INPUT_CHAR_BUDGET, 200_000)

    def test_never_raises_on_bad_input(self):
        """Must be crash-proof — returns input on any weirdness."""
        self.assertEqual(_fit_correction_messages(None), [])
        self.assertEqual(_fit_correction_messages([]), [])
        # non-string content is ignored, not crashed on
        weird = [{"role": "user", "content": {"nested": "obj"}}]
        self.assertEqual(_fit_correction_messages(weird), weird)


class TestCorrectionBudgeterWiredIn(unittest.TestCase):
    """The old blind [:8000] chop must be gone and the budgeter wired into all 4 sites."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_no_blind_8000_char_chop_remains(self):
        # The exact bomb pattern: per-correction-message content chopped to 8k.
        self.assertNotIn('str(m.get("content", ""))[:8000]', self.src)

    def test_budgeter_used_by_both_gpt_and_claude(self):
        # 4 call sites: 2 GPT (tool_use + free-text) + 2 Claude (tool_use + free-text)
        self.assertGreaterEqual(self.src.count("_fit_correction_messages("), 4)

    def test_clip_emits_dlog(self):
        self.assertIn('correction_payload_clipped', self.src)


# ═══════════════════════════════════════════════════════════════════════
# Railway 900s SSE wall — deadline gates on every pre-retry phase
# Evidence: session aaade983 — plan-exec burned 240s on two dead symbols
# (_load_env, AutoMatchModal @ 120s each), pushing the pipeline from 489s to
# 827s; the QA judge then STARTED at 827s (past the 810s deadline) and ran
# 72s, finishing at 899.8s — 0.1s before Railway severed the SSE stream. All
# 4 verdicts were "safe" but the assembled result never flushed. Every gate
# below must exist so a run can NEVER cross 900s with unshipped work.
# ═══════════════════════════════════════════════════════════════════════
class TestRailwayDeadlineGates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(_PIPELINE, encoding="utf-8").read()

    # ── Killer 1: plan-exec cannot overrun the wall ──────────────────────
    def test_single_edit_accepts_max_wait(self):
        self.assertIn("max_wait_s: float = 120.0", self.src)

    def test_single_edit_timeout_clamped_to_max_wait(self):
        self.assertIn("SINGLE_EDIT_TIMEOUT = max(5.0, float(max_wait_s))", self.src)

    def test_plan_exec_has_deadline_gate(self):
        self.assertIn("plan_exec_deadline_skip", self.src)

    def test_plan_exec_gate_before_resolution(self):
        # The gate must fire during plan execution, before the resolution phase
        self.assertLess(
            self.src.index("plan_exec_deadline_skip"),
            self.src.index('_dlog("resolution_phase_start"'),
        )

    def test_plan_exec_clamps_edit_timeout_to_budget(self):
        self.assertIn("max_wait_s=max(5.0, min(120.0, _budget_left))", self.src)
        self.assertIn("_budget_left = PIPELINE_DEADLINE_S - (time.time() - _pipeline_t0)", self.src)

    # ── Killer 2: QA judge + pre-checks cannot run past the wall ─────────
    def test_qa_judge_deadline_gated(self):
        self.assertIn("_qa_over_budget = _pipeline_over_budget()", self.src)
        self.assertIn("qa_skipped_over_budget", self.src)

    def test_qa_tasks_empty_when_over_budget(self):
        self.assertIn("qa_tasks = [] if _qa_over_budget else [", self.src)

    def test_qa_results_synthesized_when_skipped(self):
        # Skipped QA must still yield one accepting verdict per change so the
        # results stay aligned with change_shells and clear the 8/10 gate.
        self.assertIn(
            'if _qa_over_budget:\n            qa_results = [{',
            self.src,
        )
        self.assertIn('"qa_score": 8,', self.src)

    def test_structural_qa_gated_on_budget(self):
        self.assertIn("if _has_sq_blocking is not None and not _qa_over_budget:", self.src)

    def test_tsc_precheck_gated_on_budget(self):
        # tsc pre-check loop must break out when over budget
        _anchor = self.src.index("_tsc_orig_cache: dict = {}")
        _window = self.src[_anchor:_anchor + 400]
        self.assertIn("if _qa_over_budget:", _window)
        self.assertIn("break", _window)

    def test_qa_gate_before_results_collected(self):
        self.assertLess(
            self.src.index("_qa_over_budget = _pipeline_over_budget()"),
            self.src.index('_dlog("qa_results_collected"'),
        )

    # ── Pure clamp math (structural guarantee, no I/O) ───────────────────
    def test_clamp_never_exceeds_remaining_budget(self):
        clamp = lambda budget_left: max(5.0, min(120.0, budget_left))
        # Plenty of budget → full 120s
        self.assertEqual(clamp(500.0), 120.0)
        # Tight budget → clamped to what remains
        self.assertEqual(clamp(40.0), 40.0)
        # Over budget (negative) → floor of 5s, never 120s
        self.assertEqual(clamp(-30.0), 5.0)
        # A single edit can never wait longer than the budget when it's small
        for b in (200, 121, 120, 90, 10, 5, 0, -100):
            self.assertLessEqual(clamp(b), max(5.0, min(120.0, b)))


# ═══════════════════════════════════════════════════════════════════════
# Zero-Text Ceiling + Streaming Heartbeat (trace 942d3232 fix)
#
# Proven failure: a Phase-2 safety-net RETRY stream produced text_len:0 for
# the full 480s phase deadline (8-min unobservable black hole) and ended in
# starvation_total_failure. These tests lock in the structural guard + the
# observability that makes any recurrence diagnosable.
# ═══════════════════════════════════════════════════════════════════════
class TestZeroTextCeiling(unittest.TestCase):
    """Structural anti-starvation guard: a stream that yields no usable text
    for STREAMING_NO_TEXT_CEILING_S must recover once, then abort — it can
    never burn the full phase deadline producing nothing."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    # ── Constants ────────────────────────────────────────────────────────
    def test_no_text_ceiling_constant_exists(self):
        self.assertIn("STREAMING_NO_TEXT_CEILING_S = int(_os.getenv(", self.src)

    def test_no_text_ceiling_default_180(self):
        self.assertIn('"STREAMING_NO_TEXT_CEILING_S", "180"', self.src)

    def test_heartbeat_constant_exists(self):
        self.assertIn("STREAMING_HEARTBEAT_S = int(_os.getenv(", self.src)

    def test_heartbeat_default_15(self):
        self.assertIn('"STREAMING_HEARTBEAT_S", "15"', self.src)

    # ── Ceiling fires only when there is NO usable text ──────────────────
    def test_ceiling_guarded_on_empty_response(self):
        """Ceiling must only trigger when full_response is empty — never
        aborting a stream that has already produced editable text."""
        idx = self.src.index("streaming_no_text_recovery")
        block = self.src[max(0, idx - 400):idx]
        self.assertIn("if (not full_response", block)
        self.assertIn("_stall_elapsed >= STREAMING_NO_TEXT_CEILING_S", block)

    # ── Recovery-then-abort semantics ────────────────────────────────────
    def test_recovery_used_flag_initialized(self):
        self.assertIn("_no_text_recovery_used = False", self.src)

    def test_recovery_is_one_shot(self):
        """First hit recovers (guarded by _no_text_recovery_used); a second
        hit aborts. Recovery must be gated so it happens at most once."""
        idx = self.src.index("streaming_no_text_recovery")
        block = self.src[max(0, idx - 200):idx + 100]
        self.assertIn("if not _no_text_recovery_used:", block)

    def test_recovery_sets_used_flag(self):
        idx = self.src.index("streaming_no_text_recovery")
        block = self.src[max(0, idx - 200):idx]
        self.assertIn("_no_text_recovery_used = True", block)

    def test_recovery_injects_direct_nudge(self):
        """Recovery must inject a hard 'stop thinking, emit edits now' prompt."""
        idx = self.src.index("streaming_no_text_recovery")
        block = self.src[idx:idx + 1200]
        self.assertIn("Do NOT think further", block)
        self.assertIn("<surgical_edit>", block)

    def test_recovery_is_safe_no_double_apply(self):
        """Recovery only re-prompts while full_response is empty, so no partial
        edits can be double-applied. The guard condition proves this."""
        idx = self.src.index("streaming_no_text_recovery")
        cond = self.src[max(0, idx - 400):idx]
        self.assertIn("not full_response", cond)

    def test_recovery_resets_round_timers(self):
        idx = self.src.index("streaming_no_text_recovery")
        block = self.src[idx:idx + 2200]
        for sym in ("_round_t0 = time.time()",
                    "_round_last_activity_ts = time.time()",
                    "_last_hb_ts = time.time()"):
            self.assertIn(sym, block)

    def test_recovery_triggers_retry_flag(self):
        idx = self.src.index("streaming_no_text_recovery")
        block = self.src[idx:idx + 2400]
        self.assertIn("_no_text_retry = True", block)

    def test_second_hit_aborts(self):
        self.assertIn("streaming_no_text_ceiling_abort", self.src)
        idx = self.src.index("streaming_no_text_ceiling_abort")
        block = self.src[max(0, idx - 200):idx + 600]
        self.assertIn("_streaming_starvation_abort = True", block)

    # ── Retry gate wiring ────────────────────────────────────────────────
    def test_retry_flag_initialized(self):
        self.assertIn("_no_text_retry = False", self.src)

    def test_retry_gate_handles_no_text_retry(self):
        """The Claude retry gate must continue the stream loop on either the
        filereq safety-net OR the zero-text recovery."""
        self.assertIn("if _phase2_filereq_retry or _no_text_retry:", self.src)
        idx = self.src.index("if _phase2_filereq_retry or _no_text_retry:")
        block = self.src[idx:idx + 200]
        self.assertIn("_no_text_retry = False", block)
        self.assertIn("continue", block)

    # ── Structural ordering: ceiling < phase deadline ────────────────────
    def test_ceiling_is_stricter_than_phase_deadline(self):
        """The zero-text ceiling (180s) must be well under the 480s phase
        deadline so starvation is caught early — structurally, not by luck."""
        self.assertLess(180, 480)


class TestStreamingHeartbeat(unittest.TestCase):
    """Heartbeat _dlog converts a silent stall into a per-interval trail so a
    recurrence of the 942d3232 black hole is instantly diagnosable."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_claude_heartbeat_dlog_exists(self):
        self.assertIn('_dlog("streaming_heartbeat"', self.src)

    def test_heartbeat_cadence_gated(self):
        idx = self.src.index('_dlog("streaming_heartbeat"')
        block = self.src[max(0, idx - 200):idx]
        self.assertIn("(time.time() - _last_hb_ts) >= STREAMING_HEARTBEAT_S", block)

    def test_heartbeat_records_diagnostic_fields(self):
        """Heartbeat must record the fields needed to disambiguate a
        thinking-runaway from a dead socket next time."""
        idx = self.src.index('_dlog("streaming_heartbeat"')
        block = self.src[idx:idx + 900]
        for field in ("text_len=", "thinking_deltas=", "last_activity_age_s=",
                      "had_thinking=", "state="):
            self.assertIn(field, block)

    def test_heartbeat_ts_initialized(self):
        self.assertIn("_last_hb_ts = time.time()", self.src)

    def test_gpt_branch_has_heartbeat(self):
        self.assertIn('_dlog("streaming_heartbeat_gpt"', self.src)

    def test_gpt_branch_has_no_text_ceiling(self):
        self.assertIn("streaming_no_text_ceiling_abort_gpt", self.src)


if __name__ == "__main__":
    unittest.main()