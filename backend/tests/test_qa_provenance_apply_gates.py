"""QA provenance + Option A apply-gate policy (session 3a6150e9).

Prove:
  • tsc / structural / plan_* stamp machine sources
  • LLM-only blocked finalize → ``llm`` (ack_required)
  • Inference only recognizes pipeline prefixes (not free-text LLM TS prose)
  • Wire QAResult forwards block_sources / machine_verified
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.schemas import QAResult  # noqa: E402
from services.qa_provenance import (  # noqa: E402
    append_block_sources,
    append_structural_block_sources,
    apply_policy_for_sources,
    finalize_block_provenance,
)


def test_tsc_stamp_is_machine_hard_stop():
    qa = {"verdict": "blocked", "qa_score": 3, "summary": "", "type_errors": []}
    append_block_sources(qa, "tsc")
    finalize_block_provenance(qa)
    assert qa["block_sources"] == ["tsc"]
    assert qa["machine_verified"] is True
    assert apply_policy_for_sources(qa["block_sources"]) == "hard_stop"


def test_structural_plan_incomplete_stamps_both():
    qa = {"verdict": "safe", "qa_score": 9}
    append_structural_block_sources(qa, [
        {"check": "plan_incomplete", "severity": "error", "message": "Plan requires `Country`"},
        {"check": "syntax_error", "severity": "error", "message": "x"},
    ])
    qa["verdict"] = "blocked"
    finalize_block_provenance(qa)
    assert "structural" in qa["block_sources"]
    assert "plan_incomplete" in qa["block_sources"]
    assert qa["machine_verified"] is True
    assert apply_policy_for_sources(qa["block_sources"]) == "hard_stop"


def test_llm_only_finalize_tags_llm_ack_required():
    qa = {
        "verdict": "blocked",
        "qa_score": 3,
        "summary": "Looks risky to the reviewer",
        "import_issues": [],
        "type_errors": ["might be a type problem"],  # free text — NOT machine shape
    }
    finalize_block_provenance(qa)
    assert qa["block_sources"] == ["llm"]
    assert qa["machine_verified"] is False
    assert apply_policy_for_sources(qa["block_sources"]) == "ack_required"


def test_infer_tsc_from_summary_prefix_only():
    qa = {
        "verdict": "blocked",
        "summary": "tsc: 2 compile error(s) remain after auto-fix. ",
        "type_errors": ["TS1005 (line 12): ';' expected."],
        "import_issues": [],
    }
    finalize_block_provenance(qa)
    assert "tsc" in qa["block_sources"]
    assert qa["machine_verified"] is True


def test_free_text_ts_mention_does_not_become_machine():
    """LLM type_errors that mention TS without force_block shape stay llm-only."""
    qa = {
        "verdict": "blocked",
        "summary": "Possible type issue around Country",
        "type_errors": ["There may be a TS2322 elsewhere"],
        "import_issues": [],
    }
    finalize_block_provenance(qa)
    assert qa["block_sources"] == ["llm"]
    assert qa["machine_verified"] is False


def test_safe_verdict_clears_stale_sources():
    qa = {"verdict": "safe", "qa_score": 9, "block_sources": ["tsc"], "machine_verified": True}
    finalize_block_provenance(qa)
    assert qa["block_sources"] == []
    assert qa["machine_verified"] is False


def test_qaresult_schema_forwards_provenance_fields():
    obj = QAResult(
        verdict="blocked",
        qa_score=3,
        summary="tsc: 1 compile error(s).",
        block_sources=["tsc"],
        machine_verified=True,
        hard_blocked=True,
    )
    dumped = obj.model_dump()
    assert dumped["block_sources"] == ["tsc"]
    assert dumped["machine_verified"] is True


def test_pipeline_wires_provenance_helpers():
    import inspect
    from services import pipeline as p
    src = inspect.getsource(p)
    assert "append_block_sources(qa_results[_idx], \"tsc\")" in src or (
        "append_block_sources" in src and '"tsc"' in src
    )
    assert "append_structural_block_sources" in src
    assert "finalize_block_provenance" in src
    assert "block_sources=list(qa_dict.get(\"block_sources\")" in src
    assert "machine_verified=bool(qa_dict.get(\"machine_verified\")" in src


def test_frontend_policy_module_exists():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src", "lib", "qaApplyPolicy.ts")
    text = open(root).read()
    assert "hard_stop" in text
    assert "ack_required" in text
    assert "inferBlockSources" in text
    assert "TS\\d+\\s+\\(line\\s+" in text or r"TS\d+\s+\(line\s+" in text
