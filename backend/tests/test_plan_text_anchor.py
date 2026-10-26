"""Plan steps that name text, not a parsed symbol, still reach the model."""
from __future__ import annotations

import asyncio
import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services.plan_artifact import compute_plan_coverage, text_anchor_window  # noqa: E402

HTML = "\n".join(
    ["<html>"] * 2477
    + ['      <select id="gptModel">', '        <option value="gpt-4.1">GPT-4.1</option>', "      </select>"]
    + ["<p>filler</p>"] * 800
    + ["  const modelSel = document.getElementById('gptModel');"]
)
LOGIN = "\n".join(["<div>"] * 392 + ["      Title Matcher · Powered by GPT-4.1 + all-MiniLM-L6-v2"] + ["</div>"] * 140)
HANDOFF = "\n".join(["# Notes"] * 79 + ["**Use `gpt-4.1`** (non-reasoning model) as the default for all matching."] + ["x"] * 50)


def test_html_id_is_found_and_other_uses_are_listed():
    win = text_anchor_window(HTML, "gptModel", "Populate the sidebar picker from /api/models")
    assert win["line"] == 2478
    assert win["anchor"] == "gptModel"
    assert win["start"] <= 2478 <= win["end"]
    assert '<select id="gptModel">' in win["text"]
    assert win["other_lines"] == [3281]


def test_phrase_symbol_falls_back_to_a_versioned_token_in_the_step():
    login = text_anchor_window(LOGIN, "login credit", "Update the GPT-4.1 credit line to GPT-5.6 Luna.")
    assert login["line"] == 393
    assert login["anchor"] == "GPT-4.1"
    handoff = text_anchor_window(
        HANDOFF, "default model note",
        "Replace the gpt-4.1-only default note with the 5.6 default and the still-supported gpt-4.1 window.",
    )
    assert handoff["line"] == 80


def test_nothing_named_in_the_file_returns_none():
    assert text_anchor_window("VALUE = 1\n", "MISSING", "set it") is None


def test_text_anchor_step_calls_the_model_with_the_window():
    from services.pipeline import _execute_single_edit

    seen = {}

    class _Stream:
        def __init__(self, kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @property
        def text_stream(self):
            async def _gen():
                yield '<surgical_edit>{"filename": "templates/login.html", "symbol": "login credit", "new_code": "x", "old_code": "y"}</surgical_edit>'
            return _gen()

    class _Client:
        class messages:
            @staticmethod
            def stream(**kwargs):
                return _Stream(kwargs)

    raw = asyncio.run(_execute_single_edit(
        _Client(), "claude-sonnet-5", "templates/login.html", "login credit",
        "Update the GPT-4.1 credit line to GPT-5.6 Luna.", LOGIN, None, "update models",
    ))
    assert raw and '"login credit"' in raw
    prompt = seen["messages"][0]["content"]
    assert "Powered by GPT-4.1" in prompt
    assert "line 393" in prompt


def test_build3_checklist_counts_the_edits_each_step_returned():
    planned = [
        {"filename": "api_governor.py", "symbol": "governed_chat_completion", "description": "Sanitize create kwargs"},
        {"filename": "templates/index.html", "symbol": "gptModel", "description": "Populate the picker"},
        {"filename": "test_openai_models.py", "symbol": "test_luna_omits_temperature_and_uses_completion_tokens", "description": "Unit-test 5.6"},
        {"filename": "mapping_project_handoff.md", "symbol": "default model note", "description": "Replace the note"},
        {"filename": "config.py", "symbol": "MODEL_CATALOG", "description": "Add the catalog"},
    ]
    answered = [{"filename": p["filename"], "symbol": p["symbol"]} for p in planned if p["filename"] not in ("test_openai_models.py", "config.py")]
    cov = compute_plan_coverage(planned, {
        "changes_by_file": {
            "api_governor.py": {"changes": [{"symbol": {"name": "governed_chat_completion_with_headers", "full_path": "governed_chat_completion_with_headers"}}]},
            "templates/index.html": {"changes": [{"symbol": {"name": "select#gptModel", "full_path": "aside.select#gptModel"}}]},
            "mapping_project_handoff.md": {"changes": [{"symbol": {"name": "AI Model Recommendation", "full_path": "Handoff.AI Model Recommendation"}}]},
        },
        "new_files": [{"filename": "test_openai_models.py", "content": "def test_ok():\n    pass\n"}],
        "plan_answered_steps": answered,
        "plan_created_steps": [{"filename": "test_openai_models.py", "symbol": "test_luna_omits_temperature_and_uses_completion_tokens"}],
        "skipped_changes": [{"filename": "config.py", "symbol": "MODEL_CATALOG", "reason": "symbol not found"}],
    })
    covered = {row["symbol"] for row in cov["covered"]}
    assert covered == {
        "governed_chat_completion", "gptModel",
        "test_luna_omits_temperature_and_uses_completion_tokens", "default model note",
    }
    assert [row["symbol"] for row in cov["pending"]] == ["MODEL_CATALOG"]


def test_an_unanswered_step_is_not_covered_by_another_symbol():
    cov = compute_plan_coverage(
        [{"filename": "a.py", "symbol": "Widget.render"}],
        {"changes_by_file": {"a.py": {"changes": [{"symbol": "Header.render"}]}}, "skipped_changes": []},
    )
    assert cov["covered"] == []
    assert cov["pending"][0]["reason"] == "Edited `Header.render` instead of `Widget.render`."


def test_plan_lint_reports_pyflakes_findings():
    from services.linter_validator import validate_linters

    errors = validate_linters("def f():\n    return missing_name\n", "a.py")
    assert any("missing_name" in (e.get("message") or "") for e in errors)


def test_promoted_text_region_covers_the_symbol_it_targeted():
    planned = [{"filename": "api_governor.py", "symbol": "governed_chat_completion"}]
    cov = compute_plan_coverage(planned, {
        "changes_by_file": {"api_governor.py": {"changes": [{"symbol": {
            "name": "_text_region_L274_364",
            "signature": "promoted text region L274-364 (model targeted 'governed_chat_completion' at L350-364; edit span L274-364; anchor=old_code)",
        }}]}},
        "skipped_changes": [],
    })
    assert cov["ok"] is True
    assert cov["pending"] == []
