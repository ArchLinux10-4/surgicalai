"""Grok 4.5 / 4.6 / 4.7 + newest Claude / GPT-6 — settings catalog + cost tiers.

Official pricing (docs.x.ai/developers/models, <200k prompt):
  grok-4.5 / grok-4.6 / grok-4.7 are $2.00 input / $6.00 output per 1M tokens.
App cost scale: 1=cheap … 4=Opus premium ($10/$50). All Groks stay at 2.

This test parses ``routers/settings.py`` source so it runs without FastAPI
installed in lightweight venvs (same pattern as pipeline source guards).
"""
from __future__ import annotations

import ast
import os
import re

_SETTINGS = os.path.join(os.path.dirname(__file__), "..", "routers", "settings.py")
_PIPELINE = os.path.join(os.path.dirname(__file__), "..", "services", "pipeline.py")
_SETTINGS_MODAL = os.path.join(
    os.path.dirname(__file__), "..", "..", "frontend", "src", "components", "SettingsModal.tsx",
)


def _settings_src() -> str:
    with open(_SETTINGS, encoding="utf-8") as f:
        return f.read()


def _pipeline_src() -> str:
    with open(_PIPELINE, encoding="utf-8") as f:
        return f.read()


def _extract_model_dicts(src: str) -> list[dict]:
    """Pull ``{"id": ..., "cost": N, ...}`` literals from get_available_models."""
    models: list[dict] = []
    # Match one model dict literal that includes an "id" key.
    for m in re.finditer(
        r'\{\s*"id":\s*"([^"]+)"\s*,\s*"name":\s*"([^"]+)"\s*,'
        r'.*?"provider":\s*"([^"]+)"\s*,\s*"cost":\s*(\d+)\s*\}',
        src,
        flags=re.DOTALL,
    ):
        models.append({
            "id": m.group(1),
            "name": m.group(2),
            "provider": m.group(3),
            "cost": int(m.group(4)),
        })
    return models


def test_settings_lists_all_grok_models_at_cost_tier_2():
    src = _settings_src()
    by_id = {m["id"]: m for m in _extract_model_dicts(src)}
    assert "grok-4.5" in by_id
    assert "grok-4.6" in by_id
    assert "grok-4.7" in by_id
    assert by_id["grok-4.5"]["provider"] == "grok"
    assert by_id["grok-4.6"]["provider"] == "grok"
    assert by_id["grok-4.7"]["provider"] == "grok"
    assert by_id["grok-4.5"]["cost"] == 2
    assert by_id["grok-4.6"]["cost"] == 2
    assert by_id["grok-4.7"]["cost"] == 2
    # Same tier as mid Sonnet / Terra — not Opus premium (4)
    assert by_id["claude-fable-5"]["cost"] == 4
    assert by_id["claude-fable-5-1"]["cost"] == 4
    assert by_id["claude-opus-5-5"]["cost"] == 3
    assert by_id["claude-opus-5"]["cost"] == 4
    assert by_id["claude-opus-4-8"]["cost"] == 4
    assert by_id["claude-sonnet-5"]["cost"] == 2
    assert by_id["claude-haiku-4-5"]["cost"] == 1
    assert "$2/$6" in src
    # Relative: Grok is cheaper than premium Opus (cost 4), same as Sonnet (2),
    # more than Haiku (1) on the in-app dollar-scale.
    assert by_id["grok-4.7"]["cost"] < by_id["claude-fable-5-1"]["cost"]
    assert by_id["grok-4.7"]["cost"] == by_id["claude-sonnet-5"]["cost"]
    assert by_id["grok-4.7"]["cost"] > by_id["claude-haiku-4-5"]["cost"]
    # Newest Grok listed before older ones.
    assert src.index('"grok-4.7"') < src.index('"grok-4.6"')
    assert src.index('"grok-4.6"') < src.index('"grok-4.5"')


def test_hyphenated_and_fast_grok_ids_are_not_listed():
    src = _settings_src()
    ids = {m["id"] for m in _extract_model_dicts(src)}
    assert "grok-4-6" not in ids
    assert "grok-4-7" not in ids
    assert "grok-4.7-fast" not in ids
    assert '"grok-4-6"' not in src
    assert '"grok-4-7"' not in src
    assert '"grok-4.7-fast"' not in src


def test_settings_lists_newest_claude_and_gpt6():
    src = _settings_src()
    by_id = {m["id"]: m for m in _extract_model_dicts(src)}
    for mid in (
        "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5",
        "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
    ):
        assert mid in by_id, f"missing {mid}"
    assert by_id["gpt-6-astra"]["cost"] == 4
    assert by_id["gpt-6-sol"]["cost"] == 2
    assert by_id["gpt-6-luna"]["cost"] == 1
    assert by_id["gpt-6-astra"]["provider"] == "openai"
    # Newest Claude first.
    assert src.index('"claude-fable-5-1"') < src.index('"claude-opus-5-5"')
    assert src.index('"claude-opus-5-5"') < src.index('"claude-opus-5"')
    assert src.index('"claude-opus-5"') < src.index('"claude-fable-5"')
    # Newest GPT-6 before GPT-5.x.
    assert src.index('"gpt-6-astra"') < src.index('"gpt-5.5"')


def test_pipeline_adaptive_and_128k_cover_opus5_family():
    src = _pipeline_src()
    assert '"claude-opus-5"' in src
    # Adaptive tuple must include opus-5 (covers opus-5-5 via substring).
    assert re.search(
        r'_ADAPTIVE_THINKING_MODELS\s*=\s*\([^)]*"claude-opus-5"',
        src,
        flags=re.DOTALL,
    )
    assert '"claude-opus-5": 128000' in src or "'claude-opus-5': 128000" in src
    assert "_NO_FORCED_TOOL_CHOICE_MODELS" in src
    assert '"claude-opus-5-5"' in src
    assert '"claude-fable-5-1"' in src
    assert "def _claude_tool_choice_kwargs" in src
    assert "def _rejects_forced_tool_choice" in src


def test_pipeline_gpt6_in_reasoning_sets_and_sol_luna_tools_gate():
    src = _pipeline_src()
    for mid in ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna"):
        assert f'"{mid}"' in src
    assert "_GPT6_TOOLS_REQUIRE_NONE_EFFORT" in src
    assert "gpt6_sol_luna_tools_force_none_effort" in src
    # Both allowlists must include the three GPT-6 ids.
    for block_name in ("NO_TEMPERATURE_MODELS", "REASONING_EFFORT_MODELS"):
        m = re.search(rf"{block_name}\s*=\s*\{{([^}}]+)\}}", src, flags=re.DOTALL)
        assert m, f"{block_name} not found"
        body = m.group(1)
        assert "gpt-6-astra" in body
        assert "gpt-6-sol" in body
        assert "gpt-6-luna" in body


def test_forced_tool_sites_use_helper():
    src = _pipeline_src()
    assert src.count("_claude_tool_choice_kwargs(") >= 2
    # Historical forced tool_choice literals must not remain at those sites.
    assert 'tool_choice={"type": "tool", "name": "submit_file_rewrite"}' not in src
    assert 'tool_choice={"type": "tool", "name": "fix_lint_errors"}' not in src


def test_surgeon_multi_turn_preserves_thinking_blocks():
    src = _pipeline_src()
    start = src.index("_mt_assistant_content = []")
    body = src[start:start + 900]
    assert '_blk.type == "thinking"' in body
    assert '"signature"' in body or "signature" in body


def test_settings_modal_hides_temperature_for_gpt6():
    with open(_SETTINGS_MODAL, encoding="utf-8") as f:
        src = f.read()
    assert "startsWith('gpt-6')" in src
    assert "Grok 4.7" in src


def test_settings_py_parses():
    ast.parse(_settings_src())
