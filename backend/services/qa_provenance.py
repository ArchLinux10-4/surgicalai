"""QA block provenance — classifies *why* a change is blocked so Apply can gate
by error class instead of a single opaque ``verdict=blocked``.

Evidence (session 3a6150e9): false tsc blanketing and half-done plans mixed with
LLM opinion under one score. Provenance lets the UI hard-stop only on
machine-verified classes (tsc / structural / plan_*) and offer an explicit
acknowledgment Apply path for LLM-only blocks.

Sources are additive strings stamped at the exact force-block sites in
``pipeline.py``. ``finalize_block_provenance`` runs once before the wire
``QAResult`` is built — never invents machine sources from free-text LLM prose.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

# Machine-verified apply hard-stops (Option A).
MACHINE_BLOCK_SOURCES = frozenset({
    "tsc",
    "structural",
    "plan_incomplete",
    "plan_noop",
})


def _as_list(qa: Dict[str, Any]) -> List[str]:
    cur = qa.get("block_sources")
    if not isinstance(cur, list):
        return []
    out: List[str] = []
    for x in cur:
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def is_machine_source(source: str) -> bool:
    s = (source or "").strip()
    return s in MACHINE_BLOCK_SOURCES or s.startswith("plan_")


def append_block_sources(qa: Dict[str, Any], *sources: str) -> None:
    """Stamp one or more provenance sources onto a mutable QA dict."""
    cur = _as_list(qa)
    for raw in sources:
        s = str(raw or "").strip()
        if s and s not in cur:
            cur.append(s)
    qa["block_sources"] = cur
    qa["machine_verified"] = any(is_machine_source(s) for s in cur)


def append_structural_block_sources(
    qa: Dict[str, Any],
    issues: Iterable[Dict[str, Any]],
) -> None:
    """Stamp structural (+ plan_* check names) from structural_qa issue dicts."""
    append_block_sources(qa, "structural")
    for si in issues or []:
        if (si.get("severity") or "") != "error":
            continue
        check = str(si.get("check") or "").strip()
        if check.startswith("plan_"):
            append_block_sources(qa, check)


def _infer_from_existing_prefixes(qa: Dict[str, Any]) -> List[str]:
    """Defensive inference from message shapes already emitted by pipeline.

    Only recognizes patterns stamped by our own force-block / structural merge
    paths — not free-form LLM ``type_errors`` prose (avoids promoting LLM
    guesses to machine hard-stops — the 3a6150e9 bottleneck failure mode).
    """
    sources: List[str] = []
    summary = str(qa.get("summary") or "")
    if summary.startswith("tsc:"):
        sources.append("tsc")

    # Force-block type_errors look like: "TS1005 (line 42): ..."
    for te in qa.get("type_errors") or []:
        s = str(te or "")
        if s.startswith("TS") and " (line " in s and "):" in s:
            if "tsc" not in sources:
                sources.append("tsc")
            break

    imports = [str(i or "") for i in (qa.get("import_issues") or [])]
    struct = [i for i in imports if i.startswith("[STRUCTURAL]")]
    if struct:
        sources.append("structural")
        joined = "\n".join(struct)
        if "identical to ORIGINAL" in joined or "change plan was not" in joined:
            if "plan_noop" not in sources:
                sources.append("plan_noop")
        elif "Plan requires" in joined or "half-implemented" in joined:
            if "plan_incomplete" not in sources:
                sources.append("plan_incomplete")
    return sources


def finalize_block_provenance(qa: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize provenance on a QA dict right before shipping ``QAResult``.

    Rules:
      • non-blocked → empty sources, machine_verified False
      • blocked + already stamped → recompute machine_verified
      • blocked + empty → infer from our prefixes; else tag ``llm``
    """
    if not isinstance(qa, dict):
        return qa
    verdict = str(qa.get("verdict") or "")
    if verdict != "blocked":
        # Provenance is only meaningful for blocked edits — clear so safe/warning
        # rows never carry a stale hard-stop that could starve Apply.
        qa["block_sources"] = []
        qa["machine_verified"] = False
        return qa

    sources = _as_list(qa)
    if not sources:
        sources = _infer_from_existing_prefixes(qa)
    if not sources:
        sources = ["llm"]

    qa["block_sources"] = sources
    qa["machine_verified"] = any(is_machine_source(s) for s in sources)
    return qa


def apply_policy_for_sources(sources: Iterable[str]) -> str:
    """Return ``hard_stop`` | ``ack_required`` | ``allowed`` for Apply gating.

    Option A: any machine source → hard_stop; llm-only → ack_required; else allowed.
    """
    src = [str(s).strip() for s in (sources or []) if str(s).strip()]
    if any(is_machine_source(s) for s in src):
        return "hard_stop"
    if "llm" in src or src:
        # Non-empty non-machine (today only ``llm``) requires acknowledgment.
        return "ack_required"
    return "allowed"
