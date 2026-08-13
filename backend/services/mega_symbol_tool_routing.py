"""Mega-symbol tool routing (session 3d9da3fd_round7).

Evidence (surgical_debug_3d9da3fd_round7.jsonl):
  * Grok 4.5 / GPT-5.6 terra chose ``write_edit_plan`` / ``<edit_plan>`` with
    4–5 steps all targeting ``DashboardAIAssistant`` (~1186 lines).
  * Pipeline skipped same-symbol merge (``plan_items_large_symbol_skip_merge``)
    and ran each step under a 300-line mega-window → incomplete / conflicting
    edits → QA 2 and 1.
  * Opus 4.8 skipped the plan tool, ``file_request``'d the component, then
    emitted 9 coordinated ``old_code``/``new_code`` snippets → QA 9 / Apply.

This module:
  1. Detects plans that split one mega-symbol into multiple steps.
  2. Builds the instruction that steers the model onto Opus's winning path
     (request file → multiple surgical edits; do not plan-split).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# Match pipeline's large-symbol skip-merge threshold (session 336ac3bf).
MEGA_SYMBOL_PLAN_LINES = 400
# Two or more steps on the same mega symbol → reroute (round7 smoking gun).
MEGA_SAME_SYMBOL_MIN_STEPS = 2


def symbol_line_count_from_maps(symbol_maps_by_name: dict, filename: str, symbol: str) -> int:
    """Return 1-indexed span length for ``symbol`` in ``filename``, else 0."""
    if not filename or not symbol or not symbol_maps_by_name:
        return 0
    entry = symbol_maps_by_name.get(filename)
    if not entry:
        # Try basename match
        base = filename.split("/")[-1]
        for k, v in symbol_maps_by_name.items():
            if k == base or k.endswith("/" + base) or k.endswith("\\" + base):
                entry = v
                break
    if not entry:
        return 0
    smap = entry[0] if isinstance(entry, (tuple, list)) else entry
    if smap is None:
        return 0
    for s in getattr(smap, "symbols", []) or []:
        if getattr(s, "full_path", "") == symbol or getattr(s, "name", "") == symbol:
            try:
                return max(0, int(s.end_line) - int(s.start_line) + 1)
            except (TypeError, ValueError):
                return 0
    return 0


@dataclass
class MegaSameSymbolGroup:
    filename: str
    symbol: str
    symbol_lines: int
    steps: list = field(default_factory=list)

    @property
    def step_count(self) -> int:
        return len(self.steps)


@dataclass
class MegaPlanRerouteDecision:
    should_reroute: bool
    groups: list = field(default_factory=list)
    kept_steps: list = field(default_factory=list)
    rerouted_steps: list = field(default_factory=list)
    reason: str = ""


def classify_mega_symbol_edit_plan(
    plan_steps: list,
    line_count_fn: Callable[[str, str], int],
    mega_lines: int = MEGA_SYMBOL_PLAN_LINES,
    min_steps: int = MEGA_SAME_SYMBOL_MIN_STEPS,
) -> MegaPlanRerouteDecision:
    """Decide whether an edit plan must be rerouted off plan-exec.

    Reroute when ≥ ``min_steps`` plan items share the same (filename, symbol)
    AND that symbol spans more than ``mega_lines`` lines.
    """
    steps = [s for s in (plan_steps or []) if isinstance(s, dict)]
    if not steps:
        return MegaPlanRerouteDecision(should_reroute=False, reason="empty_plan")

    buckets: dict[tuple, list] = {}
    order: list[tuple] = []
    for s in steps:
        key = (str(s.get("filename") or ""), str(s.get("symbol") or ""))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(s)

    groups: list[MegaSameSymbolGroup] = []
    kept: list = []
    rerouted: list = []
    for key in order:
        items = buckets[key]
        fname, sym = key
        n_lines = int(line_count_fn(fname, sym) or 0)
        if len(items) >= min_steps and n_lines > mega_lines:
            groups.append(MegaSameSymbolGroup(
                filename=fname, symbol=sym, symbol_lines=n_lines, steps=list(items),
            ))
            rerouted.extend(items)
        else:
            kept.extend(items)

    if not groups:
        return MegaPlanRerouteDecision(
            should_reroute=False,
            kept_steps=steps,
            reason="no_mega_same_symbol_group",
        )

    return MegaPlanRerouteDecision(
        should_reroute=True,
        groups=groups,
        kept_steps=kept,
        rerouted_steps=rerouted,
        reason="mega_same_symbol_multi_step",
    )


def build_mega_symbol_reroute_instruction(
    decision: MegaPlanRerouteDecision,
    *,
    native_tools: bool = False,
) -> str:
    """User-turn instruction that steers onto Opus's winning round7 path."""
    groups = decision.groups or []
    lines = [
        "TOOL ROUTING OVERRIDE — do not use a multi-step edit plan for this.",
        "",
        "Your previous plan split coordinated changes inside one LARGE component "
        "into separate plan steps. That path windows each step to ~300 lines and "
        "causes incomplete edits (missing state, undeclared params, duplicate decls).",
        "",
        "Required path instead:",
    ]
    if native_tools:
        lines += [
            "1. Call request_file for the target file(s) if you need a fresh view.",
            "2. Then emit ALL coordinated changes as multiple write_surgical_edit "
            "calls (one focused old_code→new_code snippet per site) in this turn.",
            "3. Do NOT call write_edit_plan for multiple sites inside the same "
            "mega-symbol. Use write_edit_plan ONLY when steps target different "
            "files or different symbols.",
        ]
    else:
        lines += [
            "1. Emit <file_request>[\"ExactFile.jsx\"]</file_request> if you need "
            "a fresh view of the component.",
            "2. Then emit ALL coordinated changes as multiple <surgical_edit> "
            "blocks (old_code/new_code per site) in one response.",
            "3. Do NOT emit <edit_plan> that lists multiple steps for the same "
            "large symbol. <edit_plan> is only for distinct files/symbols.",
        ]
    lines.append("")
    lines.append("Sites that must be covered now:")
    for g in groups:
        lines.append(
            f"- {g.filename} :: {g.symbol} ({g.symbol_lines} lines, "
            f"{g.step_count} planned sites):"
        )
        for i, step in enumerate(g.steps, 1):
            desc = (step.get("description") or "").strip() or "(no description)"
            lines.append(f"  {i}. {desc}")
    # Also list kept steps so the model doesn't drop cross-file work.
    if decision.kept_steps:
        lines.append("")
        lines.append("Also complete these other plan steps in the same response "
                     "(different file/symbol — surgical edits are fine):")
        for i, step in enumerate(decision.kept_steps, 1):
            lines.append(
                f"  {i}. {step.get('filename')} :: {step.get('symbol')} — "
                f"{(step.get('description') or '')[:200]}"
            )
    lines.append("")
    lines.append("Produce the edits now. No new multi-step plan for the mega-symbol.")
    return "\n".join(lines)


def prompt_mega_symbol_tool_routing_rules(*, native_tools: bool = False) -> str:
    """Short rules block for system / tool-schema guidance."""
    if native_tools:
        return (
            "MEGA-SYMBOL RULE: When several coordinated changes land inside ONE "
            "large component/function (hundreds of lines), do NOT call "
            "write_edit_plan with multiple steps for that same symbol. Call "
            "request_file, then multiple write_surgical_edit snippets "
            "(old_code/new_code) covering every site. write_edit_plan is only "
            "for distinct files or distinct symbols (one step per symbol)."
        )
    return (
        "MEGA-SYMBOL RULE: When several coordinated changes land inside ONE "
        "large component/function (hundreds of lines), do NOT emit an "
        "<edit_plan> with multiple steps for that same symbol. Emit "
        "<file_request> if needed, then multiple <surgical_edit> blocks "
        "(old_code/new_code) covering every site. <edit_plan> is only for "
        "distinct files or distinct symbols (one step per symbol)."
    )
