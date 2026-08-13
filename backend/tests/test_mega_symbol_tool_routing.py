"""Mega-symbol tool routing (session 3d9da3fd_round7).

Grok/GPT plan-split DashboardAIAssistant → mega-windows → QA 2/1.
Opus skipped plan, file_request + surgical snippets → QA 9.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.mega_symbol_tool_routing import (  # noqa: E402
    MEGA_SYMBOL_PLAN_LINES,
    build_mega_symbol_reroute_instruction,
    classify_mega_symbol_edit_plan,
    prompt_mega_symbol_tool_routing_rules,
)
from services import grok_agent_tools as gat  # noqa: E402


def _lines_dashboard(fname, sym):
    if sym == "DashboardAIAssistant":
        return 1186
    if sym == "dashboardAssistantHandler":
        return 342
    return 0


def test_round7_grok_plan_reroutes_mega_same_symbol():
    """Exact Grok plan shape from surgical_debug_3d9da3fd_round7.jsonl."""
    plan = [
        {"filename": "DashboardAIAssistant.jsx", "symbol": "DashboardAIAssistant",
         "description": "Add researchDeep state next to other research wizard state"},
        {"filename": "DashboardAIAssistant.jsx", "symbol": "DashboardAIAssistant",
         "description": "Reset researchDeep in clearConversation"},
        {"filename": "DashboardAIAssistant.jsx", "symbol": "DashboardAIAssistant",
         "description": "Extend send() with overrideDeep param, deepResearch request body field, and researchDeep in deps"},
        {"filename": "DashboardAIAssistant.jsx", "symbol": "DashboardAIAssistant",
         "description": "handleResearchSubmit: capture deep, pass to send as 4th arg, reset researchDeep"},
        {"filename": "DashboardAIAssistant.jsx", "symbol": "DashboardAIAssistant",
         "description": "Add deep research toggle + limits tooltip UI in research step 3"},
        {"filename": "dashboardAssistant.js", "symbol": "dashboardAssistantHandler",
         "description": "Parse req.body.deepResearch, apply deep rate limit, pass deep to runWebResearch"},
    ]
    d = classify_mega_symbol_edit_plan(plan, _lines_dashboard)
    assert d.should_reroute is True
    assert d.reason == "mega_same_symbol_multi_step"
    assert len(d.groups) == 1
    assert d.groups[0].step_count == 5
    assert d.groups[0].symbol_lines == 1186
    assert len(d.kept_steps) == 1
    assert d.kept_steps[0]["symbol"] == "dashboardAssistantHandler"
    instr = build_mega_symbol_reroute_instruction(d, native_tools=True)
    assert "write_surgical_edit" in instr
    assert "write_edit_plan" in instr
    assert "researchDeep" in instr
    assert "dashboardAssistantHandler" in instr


def test_round7_gpt_plan_all_mega_reroutes():
    plan = [
        {"filename": "DashboardAIAssistant.jsx", "symbol": "DashboardAIAssistant",
         "description": f"site {i}"}
        for i in range(4)
    ]
    d = classify_mega_symbol_edit_plan(plan, _lines_dashboard)
    assert d.should_reroute is True
    assert len(d.rerouted_steps) == 4
    assert d.kept_steps == []


def test_single_step_mega_does_not_reroute():
    plan = [{
        "filename": "DashboardAIAssistant.jsx",
        "symbol": "DashboardAIAssistant",
        "description": "Add researchDeep state",
    }]
    d = classify_mega_symbol_edit_plan(plan, _lines_dashboard)
    assert d.should_reroute is False


def test_small_same_symbol_multi_keeps_merge_path():
    """Session 82eb1056: small symbols still merge — do not reroute."""
    plan = [
        {"filename": "PasteBatchJobsModal.jsx", "symbol": "PasteBatchJobsModal",
         "description": f"step {i}"}
        for i in range(4)
    ]
    d = classify_mega_symbol_edit_plan(
        plan, lambda f, s: 200, mega_lines=MEGA_SYMBOL_PLAN_LINES,
    )
    assert d.should_reroute is False
    assert len(d.kept_steps) == 4


def test_distinct_mega_symbols_no_reroute():
    plan = [
        {"filename": "a.js", "symbol": "A", "description": "x"},
        {"filename": "b.js", "symbol": "B", "description": "y"},
        {"filename": "c.js", "symbol": "C", "description": "z"},
    ]
    d = classify_mega_symbol_edit_plan(plan, lambda f, s: 900)
    assert d.should_reroute is False


def test_prompt_rules_present_in_grok_suffix():
    suffix = gat.build_grok_system_suffix(mode="edit", github_enabled=False)
    assert "MEGA-SYMBOL RULE" in suffix
    assert "write_edit_plan" in suffix
    assert "request_file" in suffix
    schema = gat._schema_write_edit_plan()
    desc = schema["function"]["description"]
    assert "DISTINCT" in desc or "distinct" in desc.lower()
    assert "write_surgical_edit" in desc


def test_xml_prompt_rules_helper():
    text = prompt_mega_symbol_tool_routing_rules(native_tools=False)
    assert "<edit_plan>" in text or "edit_plan" in text
    assert "surgical_edit" in text


def test_pipeline_wires_mega_reroute_and_force_merge():
    import inspect
    from services import pipeline as p
    src = inspect.getsource(p)
    assert "mega_symbol_plan_reroute" in src
    assert "plan_items_mega_symbol_force_merge_snippets" in src
    assert "MEGA-SYMBOL EXCEPTION" in src
    assert "expand_plan_execute_window_for_declarations" in src
