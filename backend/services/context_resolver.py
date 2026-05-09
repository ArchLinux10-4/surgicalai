"""
context_resolver.py — AST-aware context resolution for the Surgeon.

When the Surgeon signals it needs more context before it can write correct
search-and-replace operations, this service resolves those requests into
formatted, line-numbered code blocks ready for prompt injection.

Five request types the Surgeon can issue:

  {"type": "symbol",  "name": "handleSubmit"}
      Fetch a specific symbol (function/class/method/hook) by name.
      Uses the AST symbol map — returns exact code boundaries, no windowing.
      Optional "file" key to narrow to a specific file.

  {"type": "symbol",  "name": "PaymentFlow.submit", "file": "checkout.ts"}
      Fetch a method from a specific class in a specific file.

  {"type": "grep",    "pattern": "validateCart", "file": "utils.ts"}
      Search for a pattern. Unlike the simple keyword grep, this expands
      each match to the ENCLOSING SYMBOL boundary via AST — so the Surgeon
      gets a complete, compilable function instead of a raw line-window.
      "file" is optional; if omitted, searches all session files.

  {"type": "lines",   "file": "auth.py", "start": 140, "end": 180}
      Exact line-range slice. Use when you know the precise location.

  {"type": "callers", "name": "processOrder"}
      Find every place this function is called across all session files.
      Returns the enclosing function for each unique call site — gives the
      Surgeon a complete picture of how the function is consumed.

  {"type": "usages",  "name": "PaymentSchema"}
      Find every place this type/interface/const is referenced.
      Returns the enclosing symbol for each unique reference.
      Ideal for understanding contracts before changing a shared type.

All resolvers:
  - Expand grep/caller hits to enclosing AST symbol boundaries
  - Deduplicate (same symbol returned only once)
  - Cap total output to MAX_TOTAL_LINES to protect context windows
  - Fall back gracefully if a symbol isn't found (returns empty string, never raises)
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# Matches symbol_maps_by_name structure: fname → (SymbolMap | None, sf_dict)
SymbolMapsByName = Dict[str, Tuple]

MAX_TOTAL_LINES = 400   # hard cap across all resolved sections
MAX_SYMBOLS_PER_REQUEST = 4  # max symbols returned for grep/callers/usages
MAX_CALLERS = 5          # max unique call sites for callers/usages


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def resolve_context_requests(
    requests: List[Dict],
    symbol_maps_by_name: SymbolMapsByName,
    requesting_file: str = "",
) -> str:
    """
    Resolve a list of context requests from the Surgeon into formatted code.

    Returns a single formatted string with all resolved sections, ready to
    inject into the Surgeon prompt under an ADDITIONAL CONTEXT header.
    Returns "" if nothing could be resolved.
    """
    if not requests:
        return ""

    resolved: List[str] = []
    total_lines = 0

    for req in requests:
        if total_lines >= MAX_TOTAL_LINES:
            break

        req_type = req.get("type", "")
        remaining = MAX_TOTAL_LINES - total_lines

        if req_type == "symbol":
            section = _resolve_symbol(
                name=req.get("name", ""),
                file_hint=req.get("file"),
                symbol_maps_by_name=symbol_maps_by_name,
                line_budget=remaining,
            )
        elif req_type == "grep":
            section = _resolve_grep(
                pattern=req.get("pattern", ""),
                file_hint=req.get("file"),
                symbol_maps_by_name=symbol_maps_by_name,
                line_budget=remaining,
            )
        elif req_type == "lines":
            section = _resolve_lines(
                file_hint=req.get("file", requesting_file),
                start=int(req.get("start", 1)),
                end=int(req.get("end", 50)),
                symbol_maps_by_name=symbol_maps_by_name,
                line_budget=remaining,
            )
        elif req_type == "callers":
            section = _resolve_callers(
                name=req.get("name", ""),
                symbol_maps_by_name=symbol_maps_by_name,
                line_budget=remaining,
            )
        elif req_type == "usages":
            section = _resolve_usages(
                name=req.get("name", ""),
                symbol_maps_by_name=symbol_maps_by_name,
                line_budget=remaining,
            )
        else:
            continue

        if section:
            resolved.append(section)
            total_lines += section.count("\n") + 1

    if not resolved:
        return ""

    header = (
        "━━━ ADDITIONAL CONTEXT (resolved from your needs_context request) ━━━\n"
        "Reference only — do NOT include these lines in your find/replace operations\n"
        "unless you are explicitly modifying them.\n"
    )
    return header + "\n\n".join(resolved)


def describe_requests(requests: List[Dict]) -> str:
    """One-line summary for progress SSE messages."""
    parts = []
    for r in requests[:4]:
        t = r.get("type", "?")
        n = r.get("name") or r.get("pattern") or f"L{r.get('start')}-{r.get('end')}"
        parts.append(f"{t}:{n}")
    suffix = f" +{len(requests) - 4} more" if len(requests) > 4 else ""
    return ", ".join(parts) + suffix


# ─────────────────────────────────────────────────────────────────────────────
# Symbol resolution  (AST-exact)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_symbol(
    name: str,
    file_hint: Optional[str],
    symbol_maps_by_name: SymbolMapsByName,
    line_budget: int,
) -> str:
    """
    Fetch a named symbol from the AST. Supports:
      "handleSubmit"          → top-level function/const/arrow
      "PaymentFlow.submit"    → class method
      "usePaymentFlow"        → hook (treated as arrow function or function)
    """
    if not name:
        return ""

    # Normalise: "ClassName.method" → (parent="ClassName", name="method")
    parts = name.split(".", 1)
    target_name = parts[-1]
    target_parent = parts[0] if len(parts) == 2 else None

    candidates: List[Tuple[str, object, object]] = []  # (fname, symbol, sf)

    for fname, (smap, sf) in symbol_maps_by_name.items():
        if smap is None:
            continue
        if file_hint and not _file_matches(fname, file_hint):
            continue
        for sym in smap.symbols:
            name_match = sym.name == target_name or sym.name.lower() == target_name.lower()
            parent_match = (target_parent is None) or (sym.parent == target_parent)
            if name_match and parent_match:
                candidates.append((fname, sym, sf))

    if not candidates:
        # Fuzzy fallback: substring match on name
        for fname, (smap, sf) in symbol_maps_by_name.items():
            if smap is None:
                continue
            if file_hint and not _file_matches(fname, file_hint):
                continue
            for sym in smap.symbols:
                if target_name.lower() in sym.name.lower():
                    candidates.append((fname, sym, sf))
        if not candidates:
            return ""

    # Pick best match: exact name > fuzzy; smaller symbol (more surgical)
    candidates.sort(key=lambda c: (
        0 if c[1].name == target_name else 1,
        c[1].end_line - c[1].start_line,
    ))

    fname, sym, _sf = candidates[0]
    return _format_symbol_block(fname, sym, "symbol", line_budget)


# ─────────────────────────────────────────────────────────────────────────────
# Grep resolution  (AST-expanded — world-class improvement)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_grep(
    pattern: str,
    file_hint: Optional[str],
    symbol_maps_by_name: SymbolMapsByName,
    line_budget: int,
) -> str:
    """
    Search for *pattern* in session files.

    Key difference from the basic grep: for each matching line, we use the
    AST to find the ENCLOSING SYMBOL and return the full symbol — not a raw
    ±N line window.  This gives the Surgeon complete, compilable code chunks.
    """
    if not pattern:
        return ""

    found_symbols: List[Tuple[str, object]] = []  # (fname, symbol) deduplicated
    seen_paths: set = set()

    for fname, (smap, sf) in symbol_maps_by_name.items():
        if file_hint and not _file_matches(fname, file_hint):
            continue
        content = sf.get("content", "") if isinstance(sf, dict) else ""
        if not content:
            continue

        lines = content.splitlines()
        pat_lower = pattern.lower()

        for i, line in enumerate(lines):
            if pat_lower not in line.lower():
                continue
            lineno = i + 1  # 1-indexed

            # Try AST expansion first
            enclosing = _enclosing_symbol(lineno, smap)
            if enclosing:
                key = f"{fname}::{enclosing.full_path}"
                if key not in seen_paths:
                    seen_paths.add(key)
                    found_symbols.append((fname, enclosing))
            else:
                # No AST symbol — create a tight raw window (±20 lines)
                key = f"{fname}::L{lineno}"
                if key not in seen_paths:
                    seen_paths.add(key)
                    ws = max(0, i - 20)
                    we = min(len(lines), i + 21)
                    raw = "\n".join(
                        f"{ws + j + 1:5d}: {lines[ws + j]}" for j in range(we - ws)
                    )
                    # Wrap in a lightweight pseudo-symbol for uniform handling
                    class _RawBlock:
                        full_path = f"L{lineno}"
                        start_line = ws + 1
                        end_line = we
                        code = "\n".join(lines[ws:we])
                        symbol_type_str = "region"

                    found_symbols.append((fname, _RawBlock()))

            if len(found_symbols) >= MAX_SYMBOLS_PER_REQUEST:
                break

        if len(found_symbols) >= MAX_SYMBOLS_PER_REQUEST:
            break

    if not found_symbols:
        return ""

    parts: List[str] = []
    used = 0
    for fname, sym in found_symbols:
        remaining = line_budget - used
        if remaining <= 5:
            break
        block = _format_symbol_block(fname, sym, f'grep:"{pattern}"', remaining)
        if block:
            parts.append(block)
            used += block.count("\n") + 1

    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Line-range resolution
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_lines(
    file_hint: str,
    start: int,
    end: int,
    symbol_maps_by_name: SymbolMapsByName,
    line_budget: int,
) -> str:
    """Fetch an exact line range from a named file."""
    if not file_hint:
        return ""

    for fname, (_smap, sf) in symbol_maps_by_name.items():
        if not _file_matches(fname, file_hint):
            continue
        content = sf.get("content", "") if isinstance(sf, dict) else ""
        if not content:
            continue
        lines = content.splitlines()
        s = max(0, start - 1)
        e = min(len(lines), end)
        e = min(e, s + line_budget)
        chunk = "\n".join(f"{s + j + 1:5d}: {lines[s + j]}" for j in range(e - s))
        return (
            f"LINE RANGE [{fname} L{start}–{e}]:\n"
            f"{chunk}"
        )

    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Callers resolution  (call-graph aware)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_callers(
    name: str,
    symbol_maps_by_name: SymbolMapsByName,
    line_budget: int,
) -> str:
    """
    Find every function that calls *name* across all session files.
    Returns the full enclosing symbol for each unique call site — gives the
    Surgeon a complete picture of how the function is consumed before editing it.
    """
    if not name:
        return ""

    # Patterns that indicate a call: name( or name<...>( or await name(
    call_patterns = [
        re.compile(rf"\b{re.escape(name)}\s*\("),
        re.compile(rf"\b{re.escape(name)}\s*<[^>]*>\s*\("),  # generic call
        re.compile(rf"await\s+{re.escape(name)}\s*\("),
    ]

    found: List[Tuple[str, object]] = []
    seen: set = set()

    for fname, (smap, sf) in symbol_maps_by_name.items():
        content = sf.get("content", "") if isinstance(sf, dict) else ""
        if not content:
            continue
        lines = content.splitlines()

        for i, line in enumerate(lines):
            if any(p.search(line) for p in call_patterns):
                lineno = i + 1
                enclosing = _enclosing_symbol(lineno, smap)
                if enclosing:
                    key = f"{fname}::{enclosing.full_path}"
                    if key not in seen and enclosing.full_path != name:
                        seen.add(key)
                        found.append((fname, enclosing))
                else:
                    key = f"{fname}::L{lineno}"
                    if key not in seen:
                        seen.add(key)
                        # Tight window around the call site
                        ws = max(0, i - 8)
                        we = min(len(lines), i + 9)

                        class _CallSite:
                            full_path = f"call site L{lineno}"
                            start_line = ws + 1
                            end_line = we
                            code = "\n".join(lines[ws:we])

                        found.append((fname, _CallSite()))

            if len(found) >= MAX_CALLERS:
                break
        if len(found) >= MAX_CALLERS:
            break

    if not found:
        return ""

    header = f"CALLERS OF `{name}` ({len(found)} found):\n"
    parts: List[str] = [header]
    used = len(header.splitlines())

    for fname, sym in found:
        remaining = line_budget - used
        if remaining <= 5:
            break
        block = _format_symbol_block(fname, sym, f"caller of {name}", remaining)
        if block:
            parts.append(block)
            used += block.count("\n") + 1

    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Usages resolution  (type / variable references)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_usages(
    name: str,
    symbol_maps_by_name: SymbolMapsByName,
    line_budget: int,
) -> str:
    """
    Find every place *name* (a type, interface, or const) is referenced.
    Returns the enclosing symbol for each unique reference site.
    Ideal for: understanding type contracts before changing a shared type.
    """
    if not name:
        return ""

    # Patterns for type usage (not just calls): : Name, <Name>, extends Name, implements Name
    usage_patterns = [
        re.compile(rf":\s*{re.escape(name)}\b"),          # type annotation
        re.compile(rf"<{re.escape(name)}[>,\s]"),         # generic
        re.compile(rf"\b(extends|implements)\s+{re.escape(name)}\b"),
        re.compile(rf"\b{re.escape(name)}\b"),            # any reference (broad fallback)
    ]

    found: List[Tuple[str, object]] = []
    seen: set = set()

    for fname, (smap, sf) in symbol_maps_by_name.items():
        content = sf.get("content", "") if isinstance(sf, dict) else ""
        if not content:
            continue
        lines = content.splitlines()

        for i, line in enumerate(lines):
            # Skip the symbol's own definition line (would be too noisy)
            if re.search(rf"^(export\s+)?(interface|type|const)\s+{re.escape(name)}\b", line.strip()):
                continue
            if any(p.search(line) for p in usage_patterns[:3]):  # specific patterns first
                lineno = i + 1
                enclosing = _enclosing_symbol(lineno, smap)
                if enclosing:
                    key = f"{fname}::{enclosing.full_path}"
                    if key not in seen:
                        seen.add(key)
                        found.append((fname, enclosing))
            if len(found) >= MAX_CALLERS:
                break
        if len(found) >= MAX_CALLERS:
            break

    if not found:
        return ""

    header = f"USAGES OF `{name}` ({len(found)} found):\n"
    parts: List[str] = [header]
    used = len(header.splitlines())

    for fname, sym in found:
        remaining = line_budget - used
        if remaining <= 5:
            break
        block = _format_symbol_block(fname, sym, f"usage of {name}", remaining)
        if block:
            parts.append(block)
            used += block.count("\n") + 1

    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _enclosing_symbol(lineno: int, smap) -> Optional[object]:
    """
    Return the narrowest symbol in *smap* that contains *lineno*.
    Prefer smaller symbols (more surgical). Returns None if no match.
    """
    if smap is None:
        return None
    best = None
    best_size = float("inf")
    for sym in smap.symbols:
        if sym.start_line <= lineno <= sym.end_line:
            size = sym.end_line - sym.start_line
            if size < best_size:
                best_size = size
                best = sym
    return best


def _format_symbol_block(fname: str, sym, reason: str, line_budget: int) -> str:
    """
    Format a single resolved symbol as a labelled, line-numbered code block.
    Truncates to line_budget if necessary.
    """
    code = getattr(sym, "code", "") or ""
    if not code:
        return ""

    lines = code.splitlines()
    start = getattr(sym, "start_line", 1)

    # Truncate to budget
    if len(lines) > line_budget:
        lines = lines[:line_budget]
        lines.append(f"  ... [{len(code.splitlines()) - line_budget} more lines truncated]")

    numbered = "\n".join(f"{start + j:5d}: {lines[j]}" for j in range(len(lines)))
    path = getattr(sym, "full_path", "?")
    sym_type = getattr(getattr(sym, "symbol_type", None), "value", "region")

    return (
        f"[{reason.upper()} → {fname} :: {path} "
        f"({sym_type}, L{start}–{start + len(lines) - 1})]:\n"
        f"{numbered}"
    )


def _file_matches(fname: str, hint: str) -> bool:
    """Return True if fname matches the user's file hint (suffix match)."""
    if not hint:
        return True
    return fname == hint or fname.endswith(hint) or hint.endswith(fname)
