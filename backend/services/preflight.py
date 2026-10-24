"""Preflight gate: check an agent's pending edits before the turn ends.

The checks are the high-precision ones the pipeline already trusts
(tree-sitter syntax, duplicate and redeclared bindings, missing hook
imports, unresolved relative imports, pyflakes on Python). Shrinkage,
plan-completeness and props checks stay in the later QA pass.

A block that cannot be spliced is skipped, never treated as a failure.
``<edit_plan>`` steps have no code yet, so this module does not see them.
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, Iterable, List, Optional, Tuple

MAX_ROUNDS = 2
_SNIPPET = 400
_MAX_PROBLEMS = 6

# Pyflakes also reports unused imports. Those are not worth a gate round.
_PYFLAKES_KEEP = ("undefined name", "syntaxerror", "duplicate argument", "indentation")


def parse_edit_block(raw) -> Optional[dict]:
    """Parse one edit or new-file tag body. None if it is not JSON."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        data = json.loads(text)
    except Exception:
        data = None
        try:
            from services.pipeline import _repair_json
            data = json.loads(_repair_json(text))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def block_key(edit: dict) -> Tuple[str, str, str]:
    """Identity of one edit. Same key means the model re-sent that edit."""
    filename = str(edit.get("filename") or "")
    if "content" in edit and "new_code" not in edit:
        return (filename, "(new file)", "full")
    symbol = str(edit.get("symbol") or "")
    if edit.get("edit_start_line"):
        rng = f"L{edit.get('edit_start_line')}-{edit.get('edit_end_line')}"
    elif edit.get("old_code"):
        rng = "old:" + str(edit.get("old_code"))[:80]
    else:
        rng = "full:" + symbol
    return (filename, symbol, rng)


def _symbol_id(key: Tuple[str, str, str]) -> Tuple[str, str]:
    return (key[0], key[1])


def dedupe_latest(raws: Iterable) -> List:
    """Keep the last copy of each block. Unparsed bodies are all kept."""
    items = []
    for index, raw in enumerate(raws or []):
        edit = parse_edit_block(raw)
        key = block_key(edit) if edit else ("__unparsed__", str(index), "")
        items.append((key, raw))
    last = {}
    for index, (key, _raw) in enumerate(items):
        last[key] = index
    return [raw for index, (key, raw) in enumerate(items) if last[key] == index]


def split_failing(raws: Iterable, failing_keys: Iterable) -> Tuple[List, List]:
    """Return (kept, dropped). Unparsed blocks stay in kept."""
    failing = set(failing_keys or [])
    kept, dropped = [], []
    for raw in raws or []:
        edit = parse_edit_block(raw)
        key = block_key(edit) if edit else None
        if key is not None and key in failing:
            dropped.append(raw)
        else:
            kept.append(raw)
    return kept, dropped


def stash_dropped(stash: Optional[dict], dropped_edits, dropped_files) -> dict:
    """Remember dropped blocks so a later exit can put them back."""
    out = dict(stash or {})
    for raw in dropped_edits or []:
        edit = parse_edit_block(raw)
        if edit:
            out[block_key(edit)] = {"raw": raw, "kind": "edit"}
    for raw in dropped_files or []:
        edit = parse_edit_block(raw)
        if edit:
            out[block_key(edit)] = {"raw": raw, "kind": "file"}
    return out


def forget_superseded(stash: Optional[dict], kept_raws: Iterable) -> dict:
    """A new edit for the same file and symbol replaces every stashed attempt."""
    out = dict(stash or {})
    if not out:
        return out
    replaced = set()
    for raw in kept_raws or []:
        edit = parse_edit_block(raw)
        if edit:
            replaced.add(_symbol_id(block_key(edit)))
    for key in list(out):
        if _symbol_id(key) in replaced:
            out.pop(key, None)
    return out


def restore_stash(edit_raws, file_raws, stash: Optional[dict]):
    """Put stashed blocks back when the model never replaced that symbol.

    A later edit for the same filename and symbol supersedes the stash, so a
    corrected edit is not applied next to the rejected one.
    """
    edits = list(edit_raws or [])
    files = list(file_raws or [])
    if not stash:
        return edits, files
    present = set()
    for raw in edits + files:
        edit = parse_edit_block(raw)
        if edit:
            present.add(_symbol_id(block_key(edit)))
    for key, item in stash.items():
        if _symbol_id(key) in present:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "file":
            files.append(item.get("raw"))
        else:
            edits.append(item.get("raw"))
    return edits, files


def decide_preflight(
    report: Optional[dict],
    *,
    mode: str,
    rounds: int,
    over_budget: bool,
    wants_edit: bool,
    edit_blocks,
    new_files,
    stash: Optional[dict],
) -> dict:
    """Choose whether to reject the turn, log only, or let it through.

    ``restore`` is not done here. The caller restores the stash on every
    loop exit, including the budget and round-cap exits.
    """
    mode = (mode or "off").strip().lower()
    base = {
        "action": "pass",
        "edit_blocks": list(edit_blocks or []),
        "new_files": list(new_files or []),
        "stash": dict(stash or {}),
        "message": "",
        "dropped": [],
        "problems": 0,
    }
    if not wants_edit or mode == "off" or over_budget or not report:
        return base
    problems = report.get("problems") or []
    base["problems"] = len(problems)
    base["message"] = report.get("text") or ""
    if mode == "shadow":
        base["action"] = "shadow"
        return base
    if not problems or rounds >= MAX_ROUNDS:
        return base
    failing = report.get("failing_keys") or []
    keep_edits, drop_edits = split_failing(base["edit_blocks"], failing)
    keep_files, drop_files = split_failing(base["new_files"], failing)
    if not drop_edits and not drop_files:
        # The composed file is worse, but no single block owned the error.
        # Do not drop the whole turn. QA still runs.
        return base
    new_stash = stash_dropped(base["stash"], drop_edits, drop_files)
    new_stash = forget_superseded(new_stash, keep_edits + keep_files)
    base.update({
        "action": "reject",
        "edit_blocks": keep_edits,
        "new_files": keep_files,
        "stash": new_stash,
        "dropped": list(drop_edits) + list(drop_files),
    })
    return base


def stamp_grok_preflight(native_turn, dropped_raws, message: str) -> None:
    """Write the gate message into the Grok tool results for the dropped calls.

    The next turn rewrites the echo pair into native tool messages, and those
    messages read ``results_by_id``. A plain user message is not enough.
    """
    if not native_turn or not message:
        return
    failing = set()
    for raw in dropped_raws or []:
        edit = parse_edit_block(raw)
        if edit:
            failing.add(block_key(edit))
    results = native_turn.get("results_by_id")
    if not isinstance(results, dict):
        return
    for call in native_turn.get("calls") or []:
        if not isinstance(call, dict):
            continue
        name = call.get("name") or ""
        if name not in ("write_surgical_edit", "write_new_file"):
            continue
        try:
            args = json.loads(call.get("arguments") or "{}")
        except Exception:
            continue
        if not isinstance(args, dict):
            continue
        if name == "write_new_file":
            shaped = {
                "filename": args.get("filename"),
                "content": args.get("content"),
            }
        else:
            shaped = args
        key = block_key(shaped)
        call_id = call.get("id")
        if call_id and key in failing:
            results[call_id] = message


# ── Splice ───────────────────────────────────────────────────────────────────

def _lookup_symbol(symbol_maps, filename: str, symbol_name: str):
    if not symbol_maps or not symbol_name:
        return None
    entry = symbol_maps.get(filename)
    smap = entry[0] if isinstance(entry, tuple) else entry
    if smap is None:
        return None
    try:
        from services.pipeline import _fuzzy_find_symbol
        symbol, _method = _fuzzy_find_symbol(smap, symbol_name)
        return symbol
    except Exception:
        return None


def _splice_lines(text: str, start: int, end: int, new_code: str) -> Optional[str]:
    lines = text.splitlines(keepends=True)
    start_index = int(start) - 1
    end_index = int(end)  # inclusive 1-based end -> exclusive index
    if not (0 <= start_index < len(lines) and start_index < end_index <= len(lines)):
        return None
    replacement = new_code or ""
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    return "".join(lines[:start_index]) + replacement + "".join(lines[end_index:])


def _span_text(text: str, start: int, end: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[int(start) - 1:int(end)])


def _apply_old_code(text: str, old_code: str, new_code: str, symbol) -> Optional[str]:
    """Splice old_code using the pipeline helper when the symbol span is intact."""
    from services.pipeline import _apply_snippet_to_symbol

    if symbol is not None and symbol.code and _span_text(text, symbol.start_line, symbol.end_line).strip() == (symbol.code or "").strip():
        updated, ok, _reason = _apply_snippet_to_symbol(symbol.code, old_code, new_code or "")
        if ok and updated is not None:
            return _splice_lines(text, symbol.start_line, symbol.end_line, updated)
    if old_code and text.count(old_code) == 1:
        return text.replace(old_code, new_code or "", 1)
    return None


def apply_edits_to_text(original: str, edits: List[dict], symbol_maps, filename: str):
    """Apply one file's edits. Returns (composed, applied_keys, skipped).

    Line-number edits run bottom-to-top so later coordinates stay valid.
    A block that does not land is listed in ``skipped`` and is not a failure.
    """
    from services.pipeline import _plan_chain_splice_method

    text = original or ""
    applied = []
    skipped = []
    line_edits = []
    other_edits = []
    for edit in edits:
        if edit.get("edit_start_line") and edit.get("edit_end_line") and edit.get("new_code") is not None:
            line_edits.append(edit)
        else:
            other_edits.append(edit)
    line_edits.sort(key=lambda item: int(item.get("edit_start_line") or 0), reverse=True)

    for edit in line_edits:
        start = int(edit["edit_start_line"])
        end = int(edit["edit_end_line"])
        new_code = edit.get("new_code") or ""
        span = end - start + 1
        method = _plan_chain_splice_method(span, new_code, edit.get("old_code"), text)
        nxt = None
        if method == "line_splice":
            nxt = _splice_lines(text, start, end, new_code)
        elif method == "old_code_fallback" and edit.get("old_code"):
            symbol = _lookup_symbol(symbol_maps, filename, str(edit.get("symbol") or ""))
            nxt = _apply_old_code(text, edit.get("old_code") or "", new_code, symbol)
        if nxt is None or nxt == text:
            skipped.append({"key": block_key(edit), "reason": "line splice did not apply"})
            continue
        text = nxt
        applied.append(block_key(edit))

    for edit in other_edits:
        symbol = _lookup_symbol(symbol_maps, filename, str(edit.get("symbol") or ""))
        new_code = edit.get("new_code")
        old_code = edit.get("old_code")
        nxt = None
        if old_code and new_code is not None:
            nxt = _apply_old_code(text, old_code, new_code, symbol)
        elif new_code is not None and symbol is not None:
            current = _span_text(text, symbol.start_line, symbol.end_line).strip()
            if current == (symbol.code or "").strip():
                nxt = _splice_lines(text, symbol.start_line, symbol.end_line, new_code)
        if nxt is None or nxt == text:
            skipped.append({"key": block_key(edit), "reason": "edit did not apply"})
            continue
        text = nxt
        applied.append(block_key(edit))
    return text, applied, skipped


def compose_pending_files(edit_raws, new_file_raws, symbol_maps, file_contents) -> dict:
    """Return {filename: composed_text} for every file an edit landed in.

    New files are included under their filename. Callers that only need the
    post-edit session map can overlay this on the original file map.
    """
    grouped: Dict[str, List[dict]] = {}
    order: List[str] = []
    for raw in edit_raws or []:
        edit = parse_edit_block(raw)
        if not edit or not edit.get("filename"):
            continue
        filename = str(edit["filename"])
        if filename not in grouped:
            grouped[filename] = []
            order.append(filename)
        grouped[filename].append(edit)
    composed = {}
    for filename in order:
        original = (file_contents or {}).get(filename, "")
        if original is None:
            original = ""
        text, applied, _skipped = apply_edits_to_text(
            original, grouped[filename], symbol_maps, filename)
        if applied:
            composed[filename] = text
    for raw in new_file_raws or []:
        edit = parse_edit_block(raw)
        if not edit:
            continue
        filename = str(edit.get("filename") or "")
        content = edit.get("content")
        if filename and isinstance(content, str):
            composed[filename] = content
    return composed


# ── Checks ───────────────────────────────────────────────────────────────────

def _issue(check: str, message: str, line: int = 0, filename: str = "") -> dict:
    return {
        "check": check,
        "severity": "error",
        "message": message,
        "line": int(line or 0),
        "filename": filename,
    }


def collect_issues(code: str, filename: str, session_map: Optional[dict] = None) -> List[dict]:
    """High-precision issues for one full file. Empty list means clean."""
    issues: List[dict] = []
    text = code or ""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""

    if ext in (".ts", ".tsx", ".js", ".jsx", ".py"):
        try:
            from services.syntax_validator import detect_redeclarations, validate_syntax
            for err in validate_syntax(text, filename) or []:
                issues.append(_issue("syntax", err.get("message") or "syntax error", err.get("line") or 0, filename))
            for err in detect_redeclarations(text, filename) or []:
                issues.append(_issue(
                    "redeclaration", err.get("message") or "redeclaration", err.get("line") or 0, filename))
        except Exception:
            pass
        try:
            from services.structural_qa import (
                _check_duplicate_definitions,
                _check_missing_imports,
            )
            for item in _check_duplicate_definitions(text, filename):
                if item.get("severity") == "error":
                    issues.append(_issue("duplicate_definition", item.get("message") or "", 0, filename))
            if ext in (".ts", ".tsx", ".js", ".jsx"):
                # Pass the full file as file_content so a top-of-file import
                # counts (session 52802d58). The gate never sees a bare snippet.
                for item in _check_missing_imports(text, filename, file_content=text):
                    if item.get("severity") == "error":
                        issues.append(_issue("missing_import", item.get("message") or "", 0, filename))
        except Exception:
            pass

    if ext == ".py":
        try:
            from services.linter_validator import validate_linters
            for err in validate_linters(text, filename) or []:
                message = err.get("message") or ""
                if not any(token in message.lower() for token in _PYFLAKES_KEEP):
                    continue
                issues.append(_issue("pyflakes", message, err.get("line") or 0, filename))
        except Exception:
            pass

    if ext in (".ts", ".tsx", ".js", ".jsx") and session_map is not None:
        try:
            from services.preview_bundle import build_bundle
            bundle = build_bundle(filename, text, dict(session_map))
            for spec in bundle.get("unresolved") or []:
                if isinstance(spec, str) and spec.startswith("."):
                    issues.append(_issue("unresolved_import", f"Unresolved relative import '{spec}'", 0, filename))
        except Exception:
            pass
    return issues


def _only_new(before: List[dict], after: List[dict]) -> List[dict]:
    """Issues whose (check, message) count went up. Line numbers are ignored.

    A pre-existing error that shifts down because lines were inserted keeps
    the same message, so it is not reported as new (SWE-agent #1479).
    """
    from collections import Counter
    before_counts = Counter((item["check"], item["message"]) for item in before)
    seen = Counter()
    fresh = []
    for item in after:
        key = (item["check"], item["message"])
        seen[key] += 1
        if seen[key] > before_counts[key]:
            fresh.append(item)
    return fresh


def _clip(text: str, limit: int = _SNIPPET) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit] + "…"


def format_preflight_report(problems: List[dict]) -> str:
    if not problems:
        return ""
    shown = problems[:_MAX_PROBLEMS]
    lines = [
        f"[PREFLIGHT] {len(problems)} problem(s). The edit(s) below were NOT applied.",
        "Do not resend the same edit. Fix the problem and send a corrected edit.",
        "Other edits from this turn were kept.",
        "",
    ]
    for index, problem in enumerate(shown, start=1):
        where = problem.get("filename") or "?"
        symbol = problem.get("symbol") or ""
        if symbol:
            where += f" · {symbol}"
        line_no = problem.get("line") or 0
        line_bit = f" (line {line_no})" if line_no else ""
        lines.append(f"{index}. {where}{line_bit}")
        lines.append(f"   {problem.get('check')}: {problem.get('message')}")
        if problem.get("proposed"):
            lines.append("   proposed:")
            lines.append(_clip(problem["proposed"]))
        if problem.get("original"):
            lines.append("   original:")
            lines.append(_clip(problem["original"]))
        lines.append("")
    if len(problems) > len(shown):
        lines.append(f"Showing {len(shown)} of {len(problems)}.")
    return "\n".join(lines).strip()


def _baseline_for(cache: dict, filename: str, original: str, session_map: dict) -> List[dict]:
    if filename not in cache:
        cache[filename] = collect_issues(original or "", filename, session_map)
    return cache[filename]


def run_preflight(
    edit_raws,
    new_file_raws,
    symbol_maps,
    file_contents,
    baseline_cache: Optional[dict] = None,
) -> dict:
    """Check the composed result of the pending blocks.

    ``baseline_cache`` is owned by the caller and reused across gate rounds
    so the original file is not re-parsed every time.
    """
    started = time.time()
    cache = baseline_cache if isinstance(baseline_cache, dict) else {}
    contents = dict(file_contents or {})
    problems = []
    failing_keys = []
    skipped = []

    grouped: Dict[str, List[dict]] = {}
    for raw in dedupe_latest(edit_raws or []):
        edit = parse_edit_block(raw)
        if not edit or not edit.get("filename") or "content" in edit and "new_code" not in edit:
            continue
        if "new_code" not in edit and "old_code" not in edit:
            continue
        filename = str(edit["filename"])
        grouped.setdefault(filename, []).append(edit)

    for filename, edits in grouped.items():
        original = contents.get(filename, "") or ""
        session_map = dict(contents)
        session_map[filename] = original
        before = _baseline_for(cache, filename, original, session_map)
        composed, applied, file_skipped = apply_edits_to_text(
            original, edits, symbol_maps, filename)
        skipped.extend({"filename": filename, "key": item["key"], "reason": item["reason"]} for item in file_skipped)
        if not applied:
            continue
        session_map[filename] = composed
        fresh = _only_new(before, collect_issues(composed, filename, session_map))
        if not fresh:
            continue
        owners = list(applied)
        for issue in fresh:
            owner = owners[0] if len(owners) == 1 else _owner_for_line(edits, issue.get("line") or 0, owners)
            edit = next((item for item in edits if block_key(item) == owner), edits[0])
            failing_keys.append(owner)
            problems.append({
                "filename": filename,
                "symbol": edit.get("symbol") or "",
                "line": issue.get("line") or 0,
                "check": issue["check"],
                "message": issue["message"],
                "block_key": owner,
                "proposed": edit.get("new_code") or "",
                "original": _original_snippet(original, edit, symbol_maps, filename),
            })

    for raw in dedupe_latest(new_file_raws or []):
        edit = parse_edit_block(raw)
        if not edit or not isinstance(edit.get("content"), str):
            continue
        filename = str(edit.get("filename") or "new_file.txt")
        fresh = collect_issues(edit["content"], filename, dict(contents))
        # A brand-new file has no baseline. Every issue is new.
        key = block_key({"filename": filename, "content": edit["content"]})
        for issue in fresh:
            failing_keys.append(key)
            problems.append({
                "filename": filename,
                "symbol": "(new file)",
                "line": issue.get("line") or 0,
                "check": issue["check"],
                "message": issue["message"],
                "block_key": key,
                "proposed": edit.get("content") or "",
                "original": "",
            })

    # One block can own several issues. The report lists issues; the drop
    # set is the unique keys.
    unique_keys = []
    seen = set()
    for key in failing_keys:
        if key not in seen:
            seen.add(key)
            unique_keys.append(key)
    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "problems": problems,
        "failing_keys": unique_keys,
        "text": format_preflight_report(problems),
        "skipped": skipped,
        "elapsed_ms": elapsed_ms,
    }


def _owner_for_line(edits: List[dict], line: int, applied_keys: List) -> tuple:
    if line:
        for edit in edits:
            start = edit.get("edit_start_line")
            end = edit.get("edit_end_line")
            key = block_key(edit)
            if key not in applied_keys:
                continue
            if start and end and int(start) <= int(line) <= int(end):
                return key
    return applied_keys[0]


def _original_snippet(original: str, edit: dict, symbol_maps, filename: str) -> str:
    if edit.get("old_code"):
        return str(edit.get("old_code"))
    if edit.get("edit_start_line") and edit.get("edit_end_line"):
        return _span_text(original, int(edit["edit_start_line"]), int(edit["edit_end_line"]))
    symbol = _lookup_symbol(symbol_maps, filename, str(edit.get("symbol") or ""))
    if symbol is not None:
        return symbol.code or ""
    return ""


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_control_chars(text: str) -> str:
    return _CTRL.sub("", text or "")
