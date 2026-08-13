"""Grok 4.6 selectable alongside 4.5 — settings list + cost tier.

Official pricing (docs.x.ai/developers/models, <200k prompt):
  grok-4.5 / grok-4.6 both $2.00 input / $6.00 output per 1M tokens.
App cost scale: 1=cheap … 4=Opus premium ($10/$50). Both Groks stay at 2.

This test parses ``routers/settings.py`` source so it runs without FastAPI
installed in lightweight venvs (same pattern as pipeline source guards).
"""
from __future__ import annotations

import ast
import os
import re

_SETTINGS = os.path.join(os.path.dirname(__file__), "..", "routers", "settings.py")


def _settings_src() -> str:
    with open(_SETTINGS, encoding="utf-8") as f:
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


def test_settings_lists_both_grok_models_at_cost_tier_2():
    src = _settings_src()
    by_id = {m["id"]: m for m in _extract_model_dicts(src)}
    assert "grok-4.5" in by_id
    assert "grok-4.6" in by_id
    assert by_id["grok-4.5"]["provider"] == "grok"
    assert by_id["grok-4.6"]["provider"] == "grok"
    assert by_id["grok-4.5"]["cost"] == 2
    assert by_id["grok-4.6"]["cost"] == 2
    # Same tier as mid Sonnet / Terra — not Opus premium (4)
    assert by_id["claude-fable-5"]["cost"] == 4
    assert by_id["claude-opus-4-8"]["cost"] == 4
    assert by_id["claude-sonnet-5"]["cost"] == 2
    assert by_id["claude-haiku-4-5"]["cost"] == 1
    assert "$2/$6" in src
    # Relative: Grok is cheaper than premium Opus (cost 4), same as Sonnet (2),
    # more than Haiku (1) on the in-app dollar-scale.
    assert by_id["grok-4.6"]["cost"] < by_id["claude-fable-5"]["cost"]
    assert by_id["grok-4.6"]["cost"] == by_id["claude-sonnet-5"]["cost"]
    assert by_id["grok-4.6"]["cost"] > by_id["claude-haiku-4-5"]["cost"]


def test_hyphenated_grok_4_6_is_not_listed():
    src = _settings_src()
    ids = {m["id"] for m in _extract_model_dicts(src)}
    assert "grok-4-6" not in ids
    # Rejected id must not appear as a shipping model string either.
    assert '"grok-4-6"' not in src


def test_settings_py_parses():
    ast.parse(_settings_src())
