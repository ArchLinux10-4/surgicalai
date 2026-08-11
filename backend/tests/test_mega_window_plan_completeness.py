"""Mega-symbol plan-execute window cap + plan completeness (session 3a6150e9).

Evidence from surgical_debug_3a6150e9:
  - FilterSidebar window dumped 689-line symbol (789L with pad) → duplicated
    signatures / truncated JSX (QA score 1).
  - Country destructured but never written to commonData → blocked score 3.
  - NEW CODE identical to ORIGINAL for "add endpoints" plan → blocked score 2.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import (  # noqa: E402
    PLAN_EXECUTE_MAX_WINDOW,
    compute_plan_execute_window,
)
from services.structural_qa import (  # noqa: E402
    check_plan_completeness,
    run_structural_qa,
)


def _big_file(sym_start=313, sym_end=1001, hit_line=400, hit_text="Country"):
    """Synthetic file approximating MongoRecordManager FilterSidebar span."""
    lines = [f"// line {i+1}" for i in range(1200)]
    # Symbol body markers
    lines[sym_start - 1] = "const FilterSidebar = React.memo(({"
    lines[sym_end - 1] = "});"
    lines[hit_line - 1] = f"      <TextField label={{'{hit_text}'}} />"
    return lines


def test_small_symbol_keeps_full_span_plus_pad():
    lines = [f"L{i}" for i in range(100)]
    # symbol lines 10-20 (11 lines) << 300
    w = compute_plan_execute_window(lines, 10, 20, change_description="add Country")
    assert w["capped"] is False
    assert w["reason"] == "full_symbol"
    assert w["we"] - w["ws"] >= 11
    # includes edge padding
    assert w["ws"] <= 10 - 1  # before symbol start (0-indexed)


def test_mega_symbol_capped_to_max_window():
    lines = _big_file()
    w = compute_plan_execute_window(
        lines, 313, 1001,
        change_description="Add Country text field to Location section",
        max_window=PLAN_EXECUTE_MAX_WINDOW,
    )
    assert w["capped"] is True
    assert w["symbol_lines"] == 1001 - 313 + 1
    assert w["we"] - w["ws"] <= PLAN_EXECUTE_MAX_WINDOW
    assert w["reason"] == "mega_symbol_term_focus"


def test_mega_symbol_centers_near_description_term():
    lines = _big_file(hit_line=450, hit_text="Country")
    w = compute_plan_execute_window(
        lines, 313, 1001,
        change_description="Add Country field to FilterSidebar Location",
        max_window=300,
    )
    assert w["capped"] is True
    # Window should include the Country hit line (1-indexed 450 → 0-index 449)
    assert w["ws"] <= 449 < w["we"], w


def test_plan_noop_identical_new_and_original():
    code = "function route() {\n  return 1;\n}\n"
    issues = check_plan_completeness(
        "Add four new admin-gated Manage Data endpoints after this route",
        code, code,
    )
    assert any(i["check"] == "plan_noop" for i in issues)


def test_plan_half_implement_country_common_data():
    """Exact MarketRates failure shape: Country destructured, not in commonData."""
    orig = (
        "router.post('/api/jobs/add', async (req, res) => {\n"
        "  const { Title, City } = req.body;\n"
        "  const commonData = {\n"
        "    Title,\n"
        "    City,\n"
        "  };\n"
        "});\n"
    )
    half = (
        "router.post('/api/jobs/add', async (req, res) => {\n"
        "  const { Title, City, Country } = req.body;\n"
        "  const commonData = {\n"
        "    Title,\n"
        "    City,\n"
        "  };\n"
        "});\n"
    )
    issues = check_plan_completeness(
        "Add Country to commonData (default United States) on job insert",
        orig, half,
    )
    assert any(i["check"] == "plan_incomplete" for i in issues), issues
    assert any("commonData" in i["message"] for i in issues)


def test_plan_complete_when_field_in_container():
    orig = (
        "const commonData = {\n"
        "  Title,\n"
        "  City,\n"
        "};\n"
    )
    done = (
        "const commonData = {\n"
        "  Title,\n"
        "  City,\n"
        "  Country: Country || 'United States',\n"
        "};\n"
    )
    issues = check_plan_completeness(
        "Add Country to commonData on job insert",
        orig, done,
    )
    assert issues == []


def test_plan_skips_prose_add_x_to_help():
    """False-positive guard: 'Add logging to help with debugging' is not an object plan."""
    orig = "function f(){ console.log('a') }\n"
    new = "function f(){ console.log('a'); console.log('b') }\n"
    issues = check_plan_completeness(
        "Add logging to help with debugging",
        orig, new,
    )
    assert issues == []


def test_plan_skips_when_container_not_object_literal():
    """Without Y = { … }, 'add X to Y' prose must not block JSX/prose edits."""
    orig = "function FilterSidebar(){ return <Box/> }\n"
    new = "function FilterSidebar(){ return <Box><TextField label='Country'/></Box> }\n"
    issues = check_plan_completeness(
        "Add Country text field to Location section of FilterSidebar",
        orig, new,
    )
    assert issues == []


def test_run_structural_qa_wires_change_description():
    code = "export function foo() { return 1 }\n"
    issues = run_structural_qa(
        code, code, "jobManagementRoutes.js",
        change_description="Add Country to commonData",
    )
    assert any(i["check"] == "plan_noop" for i in issues)


def test_pipeline_execute_task_uses_compute_helper():
    import inspect
    from services import pipeline as p
    src = inspect.getsource(p._execute_single_edit)
    assert "compute_plan_execute_window" in src
    assert "capped" in src
    assert "execute_task_mega_window_capped" in src
    assert "plan_completeness_blocked" in inspect.getsource(p)
