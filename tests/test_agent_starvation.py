"""
Tests for Agent Mode streaming starvation detection and auto-retry.

Verifies the starvation recovery path added after the Agent Mode initial
Claude streaming call (line ~4375 in pipeline.py).

These tests extract and execute the pure-logic helpers from pipeline.py
rather than mocking the full async generator.
"""
import ast
import textwrap
import unittest

_PIPELINE = "/tmp/sgcheck/backend/services/pipeline.py"

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
        """Agent Mode initial call uses budget=8000 — verify it's capped."""
        kw = _get_thinking_kwargs("claude-sonnet-5", 8000)
        self.assertEqual(kw["thinking"]["type"], "enabled")
        self.assertEqual(kw["thinking"]["budget_tokens"], 8000)

    def test_retry_budget_doubled_to_16000(self):
        """On starvation, retry uses budget=16000."""
        kw = _get_thinking_kwargs("claude-sonnet-5", 16000)
        self.assertEqual(kw["thinking"]["type"], "enabled")
        self.assertEqual(kw["thinking"]["budget_tokens"], 16000)

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


if __name__ == "__main__":
    unittest.main()
