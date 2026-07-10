"""
Test the centralized _safe_claude_call wrapper and supporting helpers.

Extracts real functions from pipeline.py via AST to test the exact shipped code.
"""
import ast
import os
import sys
import types
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.normpath(os.path.join(_HERE, "..", "backend", "services", "pipeline.py"))

# --- Extract functions from pipeline.py without importing the whole module ---

_FUNCS_TO_EXTRACT = [
    "_uses_adaptive_thinking",
    "_supports_thinking",
    "_is_claude_model",
    "_is_gemini_model",
    "_max_output_tokens",
    "_get_thinking_kwargs",
    "_get_effort_kwargs",
    "_bounded_thinking_params",
    "_is_starved",
]

def _extract_functions():
    src = open(_PIPELINE, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}

    # Provide stubs for dependencies not under test
    ns["_dlog"] = lambda *a, **kw: None  # no-op logger

    # Extract module-level constants/sets needed by the functions
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        segment = ast.get_source_segment(src, node)
                        if segment:
                            exec(segment, ns)
                    except Exception:
                        pass

    # Extract the functions
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _FUNCS_TO_EXTRACT:
                segment = ast.get_source_segment(src, node)
                if segment:
                    exec(segment, ns)

    return ns

_NS = _extract_functions()

# Verify all functions were extracted
for fn_name in _FUNCS_TO_EXTRACT:
    assert fn_name in _NS, f"Failed to extract {fn_name} from pipeline.py"


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestBoundedThinkingParams:
    """_bounded_thinking_params must cap thinking so text can't be starved."""

    def test_adaptive_model_gets_enabled_with_budget(self):
        """Adaptive models should get 'enabled' type, NOT 'adaptive'."""
        params = _NS["_bounded_thinking_params"]("claude-sonnet-5", 8192, 10000)
        think = params.get("thinking", {})
        assert think.get("type") == "enabled", \
            f"Expected 'enabled' thinking for adaptive model, got {think.get('type')}"
        assert "budget_tokens" in think, "Missing budget_tokens for adaptive model"

    def test_adaptive_model_budget_capped(self):
        """Budget must not exceed max_tokens - desired_text."""
        params = _NS["_bounded_thinking_params"]("claude-sonnet-5", 8192, 200000)
        think = params.get("thinking", {})
        max_tok = params.get("max_tokens", 0)
        budget = think.get("budget_tokens", 0)
        # text headroom = max_tokens - budget must be >= desired_text_tokens
        headroom = max_tok - budget
        assert headroom >= 8192, \
            f"Text headroom {headroom} < desired 8192. max_tokens={max_tok}, budget={budget}"

    def test_effort_included(self):
        """Adaptive models should get effort config."""
        params = _NS["_bounded_thinking_params"]("claude-sonnet-5", 8192, 10000)
        assert "output_config" in params, "Missing output_config (effort)"

    def test_non_thinking_model_no_thinking(self):
        """Non-thinking models should get no thinking params."""
        params = _NS["_bounded_thinking_params"]("gpt-4o", 4096, 4000)
        assert "thinking" not in params


class TestGetThinkingKwargs:
    """_get_thinking_kwargs must return capped thinking for adaptive models."""

    def test_adaptive_model_returns_enabled(self):
        """After fix: adaptive models should get 'enabled', not 'adaptive'."""
        kw = _NS["_get_thinking_kwargs"]("claude-sonnet-5", 10000)
        think = kw.get("thinking", {})
        assert think.get("type") == "enabled", \
            f"Expected 'enabled' for adaptive model, got {think.get('type')}. " \
            f"If 'adaptive', the starvation bug is NOT fixed!"
        assert think.get("budget_tokens") == 10000

    def test_adaptive_model_honors_budget(self):
        """Budget parameter must be respected."""
        kw = _NS["_get_thinking_kwargs"]("claude-sonnet-5", 4000)
        think = kw.get("thinking", {})
        assert think.get("budget_tokens") == 4000

    def test_manual_thinking_model(self):
        """Manual thinking models (e.g. claude-4.6) should also get 'enabled'."""
        if _NS["_supports_thinking"]("claude-4.6"):
            kw = _NS["_get_thinking_kwargs"]("claude-4.6", 5000)
            think = kw.get("thinking", {})
            assert think.get("type") == "enabled"
            assert think.get("budget_tokens") == 5000

    def test_non_claude_model_empty(self):
        """Non-Claude models should return empty dict."""
        kw = _NS["_get_thinking_kwargs"]("gpt-4.1", 10000)
        assert kw == {}


class TestIsStarved:
    """_is_starved detects when model spent all tokens thinking."""

    def test_starved_end_turn_empty_text(self):
        """stop_reason=end_turn with no text blocks → starved."""
        msg = types.SimpleNamespace(
            stop_reason="end_turn",
            content=[
                types.SimpleNamespace(type="thinking", thinking="long chain of thought..."),
            ],
            usage=types.SimpleNamespace(output_tokens=15000),
        )
        assert _NS["_is_starved"](msg) is True

    def test_not_starved_with_text(self):
        """Has text block → not starved."""
        msg = types.SimpleNamespace(
            stop_reason="end_turn",
            content=[
                types.SimpleNamespace(type="thinking", thinking="thinking..."),
                types.SimpleNamespace(type="text", text="Here is my answer"),
            ],
            usage=types.SimpleNamespace(output_tokens=8000),
        )
        assert _NS["_is_starved"](msg) is False

    def test_tool_use_no_text_is_starved(self):
        """tool_use with no text → _is_starved returns True.
        
        This is correct: _is_starved checks for text presence only.
        The wrapper checks stop_reason separately before retrying.
        """
        msg = types.SimpleNamespace(
            stop_reason="tool_use",
            content=[
                types.SimpleNamespace(type="tool_use", name="submit_fix", input={}),
            ],
            usage=types.SimpleNamespace(output_tokens=5000),
        )
        assert _NS["_is_starved"](msg) is True

    def test_none_message(self):
        """None message → not starved (shouldn't crash)."""
        assert _NS["_is_starved"](None) is False


class TestGetEffortKwargs:
    """_get_effort_kwargs returns correct effort config."""

    def test_adaptive_model_gets_effort(self):
        kw = _NS["_get_effort_kwargs"]("claude-sonnet-5")
        assert "output_config" in kw
        assert kw["output_config"]["effort"] in ("xhigh", "high")

    def test_non_adaptive_model_empty(self):
        kw = _NS["_get_effort_kwargs"]("gpt-4.1")
        assert kw == {}
