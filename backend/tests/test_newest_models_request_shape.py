"""Unit tests for newest-model request-shape helpers in pipeline.py.

Imports only the lightweight helpers we added for Opus 5.5 / Fable 5.1
forced-tool downgrade and GPT-6 Sol/Luna tools effort — avoiding a full
pipeline run.
"""
from __future__ import annotations

import pathlib
import sys

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))


def _load_helpers():
    """Import just the helper functions without pulling the whole pipeline graph.

    pipeline.py is huge and imports many optional deps; source-exec the
    helper bodies is brittle. Prefer importing from services.pipeline when
    the env has deps; otherwise fall back to re-defining from source via
    a narrow exec of the helper section is not worth it — skip if import
    fails for missing deps and rely on the source-level tests.
    """
    try:
        from services.pipeline import (  # noqa: WPS433
            _rejects_forced_tool_choice,
            _claude_tool_choice_kwargs,
            _uses_adaptive_thinking,
            _max_output_tokens,
            NO_TEMPERATURE_MODELS,
            REASONING_EFFORT_MODELS,
            _GPT6_TOOLS_REQUIRE_NONE_EFFORT,
        )
        return {
            "rejects": _rejects_forced_tool_choice,
            "tool_kw": _claude_tool_choice_kwargs,
            "adaptive": _uses_adaptive_thinking,
            "max_out": _max_output_tokens,
            "no_temp": NO_TEMPERATURE_MODELS,
            "effort": REASONING_EFFORT_MODELS,
            "sol_luna": _GPT6_TOOLS_REQUIRE_NONE_EFFORT,
        }
    except Exception as exc:  # pragma: no cover - env without full deps
        import pytest
        pytest.skip(f"pipeline import unavailable: {exc}")


def test_rejects_forced_tool_choice_only_for_55_and_fable_51():
    h = _load_helpers()
    assert h["rejects"]("claude-opus-5-5") is True
    assert h["rejects"]("claude-fable-5-1") is True
    assert h["rejects"]("claude-opus-5") is False
    assert h["rejects"]("claude-opus-4-8") is False
    assert h["rejects"]("claude-fable-5") is False
    assert h["rejects"]("claude-sonnet-5") is False


def test_claude_tool_choice_kwargs_downgrades_to_auto_strict():
    h = _load_helpers()
    tools = [{
        "name": "submit_file_rewrite",
        "description": "rewrite",
        "input_schema": {
            "type": "object",
            "properties": {
                "new_file_content": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["new_file_content", "confidence"],
        },
    }]
    kw = h["tool_kw"]("claude-opus-5-5", "submit_file_rewrite", tools)
    assert kw["tool_choice"] == {"type": "auto"}
    assert kw["tools"][0]["strict"] is True
    assert kw["tools"][0]["input_schema"]["additionalProperties"] is False

    kw_forced = h["tool_kw"]("claude-opus-5", "submit_file_rewrite", tools)
    assert kw_forced["tool_choice"] == {"type": "tool", "name": "submit_file_rewrite"}
    assert "strict" not in kw_forced["tools"][0]


def test_adaptive_thinking_and_128k_for_opus5_family():
    h = _load_helpers()
    assert h["adaptive"]("claude-opus-5") is True
    assert h["adaptive"]("claude-opus-5-5") is True
    assert h["adaptive"]("claude-fable-5-1") is True
    assert h["max_out"]("claude-opus-5") == 128000
    assert h["max_out"]("claude-opus-5-5") == 128000
    assert h["max_out"]("claude-fable-5-1") == 128000


def test_gpt6_in_reasoning_allowlists():
    h = _load_helpers()
    for mid in ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna"):
        assert mid in h["no_temp"]
        assert mid in h["effort"]
    assert "gpt-6-sol" in h["sol_luna"]
    assert "gpt-6-luna" in h["sol_luna"]
    assert "gpt-6-astra" not in h["sol_luna"]
