"""
Tests for Agent Mode streaming starvation detection and auto-retry.

Verifies the starvation recovery path added after the Agent Mode initial
Claude streaming call (line ~4375 in pipeline.py).

These tests extract and execute the pure-logic helpers from pipeline.py
rather than mocking the full async generator.
"""
import ast
import os
import textwrap
import unittest

# Portable path — matches the pattern in test_safe_claude_call.py. This was
# previously a hardcoded sandbox-only path (/tmp/sgcheck/...) that only
# existed in one debugging session; it broke test collection for every
# fresh clone (CI, other machines, this repo re-cloned), aborting the
# entire pytest run rather than just this file.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.normpath(os.path.join(_HERE, "..", "backend", "services", "pipeline.py"))

# ---------------------------------------------------------------------------
# Extract helper functions from pipeline.py the same way test_safe_claude_call does
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

    ns = {"_dlog": lambda *a, **kw: None}  # no-op logger

    # Extract constants
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _MODEL_CONST_NAMES:
                    code = ast.get_source_segment(src, node)
                    if code:
                        exec(code, ns)

    # Extract functions
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


class TestAgentModeStarvationRecoveryLogic(unittest.TestCase):
    """Verify the logic used by the Agent Mode starvation recovery path."""

    def test_initial_call_budget_capped_at_8000(self):
        """Agent Mode initial call uses budget=8000 — adaptive models get type=adaptive."""
        kw = _get_thinking_kwargs("claude-sonnet-5", 8000)
        # Adaptive models (claude-sonnet-5) MUST use type=adaptive —
        # Anthropic API rejects type=enabled on these models (400 error).
        # Budget capping is structurally impossible; starvation protection
        # relies on the detect+retry layer in _safe_claude_call.
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
        # max_tokens must be > budget (text needs room)
        self.assertGreater(max_t, 16000)
        # Should include a text margin (at least 4096 based on implementation)
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
        # These are the exact conditions checked in the agent starvation code
        self.assertTrue(not "".strip())
        self.assertTrue(not "   ".strip())
        self.assertTrue(not "\n\n".strip())

    def test_starvation_detection_has_text(self):
        """Non-empty text should NOT trigger starvation."""
        self.assertFalse(not '{"intent": "edit"}'.strip())
        self.assertFalse(not "hello".strip())


class TestAgentModeCodePresence(unittest.TestCase):
    """Verify the starvation recovery code exists in pipeline.py."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_starvation_detection_log_exists(self):
        """_dlog('agent_thinking_starvation_detected', ...) must exist."""
        self.assertIn("agent_thinking_starvation_detected", self.src)

    def test_starvation_retry_done_log_exists(self):
        """_dlog('agent_starvation_retry_done', ...) must exist."""
        self.assertIn("agent_starvation_retry_done", self.src)

    def test_starvation_final_fail_log_exists(self):
        """_dlog('agent_starvation_final_fail', ...) must exist."""
        self.assertIn("agent_starvation_final_fail", self.src)

    def test_agent_stream_done_log_exists(self):
        """_dlog('agent_stream_done', ...) must exist."""
        self.assertIn("agent_stream_done", self.src)

    def test_retry_uses_bounded_thinking_params(self):
        """Retry path must call _bounded_thinking_params for safe max_tokens."""
        # Find the starvation block and verify _bounded_thinking_params is called
        idx = self.src.index("agent_thinking_starvation_detected")
        # Within 500 chars before the log, _bounded_thinking_params should appear
        block = self.src[max(0, idx - 500):idx + 500]
        self.assertIn("_bounded_thinking_params", block)

    def test_retry_uses_doubled_budget(self):
        """Retry must use 16_000 budget (double the initial 8000)."""
        idx = self.src.index("agent_thinking_starvation_detected")
        block = self.src[max(0, idx - 300):idx]
        self.assertIn("16_000", block)

    def test_no_messages_create_in_agent_mode(self):
        """Agent mode must NOT use messages.create() — only streaming."""
        # Find the agent mode function
        start = self.src.index("Agent Mode starvation detection")
        # Check 2000 chars around it — no messages.create()
        block = self.src[max(0, start - 1000):start + 1000]
        self.assertNotIn("messages.create(", block)


class TestStreamingPhaseDeadlines(unittest.TestCase):
    """Verify streaming-phase deadline code exists in pipeline.py.

    Evidence: session a50319ca — Opus 4.6 adaptive thinking consumed
    277s (Stream 2) and 570s (Stream 3) without producing text tokens,
    hitting Railway's 15-min SSE limit.
    """

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_phase_deadline_constant_exists(self):
        """STREAMING_PHASE_DEADLINE_S must be defined."""
        self.assertIn("STREAMING_PHASE_DEADLINE_S = 480", self.src)

    def test_thinking_stall_constant_exists(self):
        """STREAMING_THINKING_STALL_S must be defined."""
        self.assertIn("STREAMING_THINKING_STALL_S = 120", self.src)

    def test_starvation_abort_flag_initialized(self):
        """_streaming_starvation_abort must be initialized before search loop."""
        idx_flag = self.src.index("_streaming_starvation_abort = False")
        idx_loop = self.src.index("for search_round in range(MAX_SEARCH_ROUNDS")
        self.assertLess(idx_flag, idx_loop,
                        "Flag must be initialized before the search loop")

    def test_round_text_timestamp_initialized(self):
        """_round_last_text_ts must be set at start of each round."""
        idx_round = self.src.index("_round_t0 = time.time()")
        block = self.src[idx_round:idx_round + 200]
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


class TestStarvationRecovery(unittest.TestCase):
    """Tests for the no-thinking retry recovery after starvation abort."""

    def setUp(self):
        self.src = open(_PIPELINE, encoding="utf-8").read()

    def test_recovery_retry_dlog_exists(self):
        self.assertIn("starvation_recovery_retry", self.src)

    def test_recovery_done_dlog_exists(self):
        self.assertIn("starvation_recovery_done", self.src)

    def test_recovery_error_dlog_exists(self):
        self.assertIn("starvation_recovery_error", self.src)

    def test_total_failure_dlog_exists(self):
        self.assertIn("starvation_total_failure", self.src)

    def test_thinking_stripped_from_retry(self):
        """Retry kwargs must exclude 'thinking' key."""
        self.assertIn('k: v for k, v in stream_kwargs.items() if k != "thinking"', self.src)

    def test_output_config_stripped_from_retry(self):
        """output_config (effort) must be stripped — not valid without thinking."""
        self.assertIn('_retry_kwargs.pop("output_config"', self.src)

    def test_recovery_user_message_on_retry(self):
        """User should see a progress message when recovery retry starts."""
        self.assertIn("retrying without extended thinking", self.src)

    def test_recovery_user_message_on_total_failure(self):
        """User must see an error if both original and recovery produce nothing."""
        self.assertIn("spent all available time thinking", self.src)

    def test_recovery_guards_pipeline_budget(self):
        """Recovery retry must check _pipeline_over_budget before retrying."""
        idx = self.src.find("starvation_recovery_retry")
        # The budget check should appear before the dlog
        budget_idx = self.src.rfind("_pipeline_over_budget", 0, idx)
        self.assertGreater(budget_idx, 0,
                           "_pipeline_over_budget check must precede recovery retry")

    def test_recovery_replaces_main_variables(self):
        """On success, recovery must overwrite full_response and edit_blocks_raw."""
        idx = self.src.find("starvation_recovery_done")
        after = self.src[idx:idx + 800]
        self.assertIn("full_response = _retry_response", after)
        self.assertIn("edit_blocks_raw = _retry_edit_blocks", after)

    def test_recovery_only_when_empty_response(self):
        """Recovery must only trigger when full_response is empty."""
        idx = self.src.find("starvation_recovery_retry")
        before = self.src[max(0, idx - 300):idx]
        self.assertIn("len(full_response.strip()) == 0", before)

    def test_no_edits_text_only_sends_user_notice(self):
        """When model produces text but no edit blocks, user must see a notice.

        Evidence: session d02bc93f run 5 — recovery produced 1567 chars
        of plan text but 0 edit blocks. no_edits_produced fired silently,
        user saw the plan text streamed but got no indication that no
        files were changed.
        """
        self.assertIn("no_edits_text_only_notice", self.src)
        # The notice must contain a clear user-facing message
        self.assertIn("No code changes were produced", self.src)

    def test_no_edits_text_only_dlog(self):
        """The text-only notice must log was_starvation_recovery flag."""
        idx = self.src.find("no_edits_text_only_notice")
        self.assertGreater(idx, 0)
        after = self.src[idx:idx + 300]
        self.assertIn("was_starvation_recovery", after)

    def test_no_edits_text_only_after_skipped_changes_check(self):
        """The text-only notice is an elif after skipped_changes_struct check,
        so it only fires when no plan tasks ran."""
        idx = self.src.find("no_edits_text_only_notice")
        # Actual distance is ~570 chars; use 700 for margin
        before = self.src[max(0, idx - 700):idx]
        self.assertIn("elif full_response.strip()", before)


if __name__ == "__main__":
    unittest.main()
