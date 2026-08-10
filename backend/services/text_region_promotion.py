"""
text_region_promotion.py  —  Cross-boundary edit → virtual text-region promotion.

WHY THIS EXISTS  (proven root cause, sessions 430d9711 Grok + Sonnet 5)
----------------------------------------------------------------------
A model targets a small indexed symbol (e.g. the 1-line const
`ADMIN_USERNAMES`) but the *real* edit it wants spans adjacent lines that the
parser never indexed as a symbol (the `module.exports = { ... }` block right
below it — a plain object literal, not a named symbol). The resolved symbol
"box" is 1 line; the edit is 10 lines. Two failure shapes result, both
originating from the SAME cause — the edit does not fit the box:

  * Option A (old_code snippet, e.g. Grok):
      The symbol splice fails, the file-level fallback keeps the symbol edit as
      a NO-OP sentinel (`new_code = accumulator base`) and shunts the real
      change into a companion `_extra_ops` op. QA scopes to `symbol.code`,
      sees an EMPTY primary diff, and non-deterministically BLOCKS the change
      ("symbol unchanged, no code change at all").

  * Option B (edit_start_line/edit_end_line, e.g. Sonnet 5):
      `_apply_snippet_by_lines` rejects the edit as "out of bounds for symbol"
      (lines 72–81 cannot fit a 72–72 symbol). With no old_code the file-level
      fallback cannot even run, so the change is dropped entirely
      (`degenerate_drop → correction_no_edit_produced`) — a SILENT failure with
      no diff card at all.

THE FIX
-------
When the true edit span crosses out of the resolved symbol into adjacent
unindexed lines, PROMOTE the edit to a *virtual text-region symbol* built from
the real file window — the exact idiom this codebase already uses for
`virtual_sym` / `_gap_bridge` text regions. The virtual symbol's `.code` is a
verbatim slice of the file (so QA can scope to it and apply can find/replace
it), and the produced `new_code` is the genuine replacement for that window.
Every downstream consumer (QA before/after, `_make_diff`, the frontend
apply gate, the intermediate-state advance) then sees a REAL before → after
diff instead of an empty sentinel or nothing at all — for every model.

This module is intentionally self-contained: it imports only the schema types
and receives the pipeline's line-splice helper and structured logger via
dependency injection (`apply_by_lines`, `dlog`) so it can live in its own file
with no circular import back into pipeline.py.
"""

from typing import Callable, Optional, Tuple

from models.schemas import SymbolInfo, SymbolType


def _noop_dlog(_event: str, **_kwargs):  # pragma: no cover - trivial
    """Fallback logger used only if the caller injects nothing.

    The real pipeline always injects `_dlog`; this keeps the module importable
    and testable in isolation without ever crashing on a missing logger.
    """
    return None


def _abs_line_span_of_substring(text: str, sub: str) -> Optional[Tuple[int, int]]:
    """1-indexed inclusive (start_line, end_line) that `sub` occupies in `text`.

    Returns None when `sub` is absent or ambiguous (>1 occurrence) — ambiguity
    must NOT be silently resolved to the first hit, or we could promote an edit
    against the wrong region.
    """
    if not sub:
        return None
    if text.count(sub) != 1:
        return None
    idx = text.find(sub)
    if idx < 0:
        return None
    start_line = text.count("\n", 0, idx) + 1
    end_char = idx + len(sub) - 1
    end_line = text.count("\n", 0, end_char) + 1
    return start_line, end_line


def promote_to_text_region_edit(
    *,
    file_content: str,
    resolved_symbol,
    new_code: str,
    edit_start_line: Optional[int] = None,
    edit_end_line: Optional[int] = None,
    located_old_code: Optional[str] = None,
    apply_by_lines: Optional[Callable[..., Tuple[Optional[str], bool, str]]] = None,
    dlog: Optional[Callable[..., None]] = None,
    session_id: str = "",
    filename: str = "",
    symbol_name: str = "",
    user_id: str = "",
    site: str = "",
) -> Tuple[Optional[SymbolInfo], Optional[str], bool, str]:
    """Promote a cross-boundary edit to a virtual text-region symbol.

    Exactly ONE anchor source must be supplied:
      * Option B — `edit_start_line` + `edit_end_line` (absolute 1-indexed,
        inclusive). `new_code` is the replacement for that whole line range.
        Requires `apply_by_lines` (the pipeline's `_apply_snippet_by_lines`).
      * Option A — `located_old_code` (the EXACT file bytes already matched by
        the caller's whitespace-tolerant locator). `new_code` replaces it.

    Returns (virtual_symbol, full_new_code, ok, reason).
      ok=True  -> virtual_symbol.code is a verbatim slice of `file_content`
                  (QA-scopable, apply-findable) and full_new_code is the real
                  replacement for that window.
      ok=False -> reason explains why promotion was declined; the caller keeps
                  its existing fallback behaviour unchanged.

    NEVER raises for expected failure modes — it returns ok=False with a
    reason. The caller wraps the call in try/except for the truly unexpected.
    """
    dlog = dlog or _noop_dlog

    flines = file_content.splitlines(keepends=True)
    total = len(flines)
    if total == 0:
        dlog("text_region_promotion_skipped", session_id=session_id,
             filename=filename, symbol=symbol_name, site=site,
             reason="empty_file", user_id=user_id)
        return None, None, False, "empty_file"

    sym_start = int(getattr(resolved_symbol, "start_line", 0) or 0)
    sym_end = int(getattr(resolved_symbol, "end_line", 0) or 0)

    mode = None
    span_start = span_end = None

    # ── Determine the true edit span in ABSOLUTE file lines ──────────────
    if edit_start_line and edit_end_line:
        mode = "line_numbers"
        span_start, span_end = int(edit_start_line), int(edit_end_line)
        if not (1 <= span_start <= span_end <= total):
            dlog("text_region_promotion_skipped", session_id=session_id,
                 filename=filename, symbol=symbol_name, site=site,
                 reason="span_out_of_file_bounds",
                 span=f"{span_start}-{span_end}", file_total=total,
                 user_id=user_id)
            return None, None, False, f"span_out_of_file_bounds:{span_start}-{span_end}/{total}"
    elif located_old_code:
        mode = "old_code"
        _span = _abs_line_span_of_substring(file_content, located_old_code)
        if _span is None:
            dlog("text_region_promotion_skipped", session_id=session_id,
                 filename=filename, symbol=symbol_name, site=site,
                 reason="old_code_absent_or_ambiguous_in_file",
                 old_code_len=len(located_old_code), user_id=user_id)
            return None, None, False, "old_code_absent_or_ambiguous_in_file"
        span_start, span_end = _span
    else:
        dlog("text_region_promotion_skipped", session_id=session_id,
             filename=filename, symbol=symbol_name, site=site,
             reason="no_anchor_source", user_id=user_id)
        return None, None, False, "no_anchor_source"

    # ── Build the window: union of the edit span and the resolved symbol ──
    # Unioning with the resolved symbol keeps the promotion coherent with what
    # the model *thought* it was editing and guarantees the window is never
    # smaller than either region.
    win_start = span_start
    win_end = span_end
    if sym_start:
        win_start = min(win_start, sym_start)
    if sym_end:
        win_end = max(win_end, sym_end)
    win_start = max(1, win_start)
    win_end = min(total, win_end)

    win_code = "".join(flines[win_start - 1:win_end])
    if not win_code:
        dlog("text_region_promotion_skipped", session_id=session_id,
             filename=filename, symbol=symbol_name, site=site,
             reason="empty_window", window=f"{win_start}-{win_end}", user_id=user_id)
        return None, None, False, "empty_window"

    # ── Produce the replacement text for the whole window ────────────────
    if mode == "line_numbers":
        if apply_by_lines is None:
            return None, None, False, "apply_by_lines_not_provided"
        full_new, ok, reason = apply_by_lines(
            win_code, win_start, span_start, span_end, new_code
        )
        if not ok or full_new is None:
            dlog("text_region_promotion_skipped", session_id=session_id,
                 filename=filename, symbol=symbol_name, site=site,
                 reason="window_line_splice_failed", detail=reason,
                 window=f"{win_start}-{win_end}", span=f"{span_start}-{span_end}",
                 user_id=user_id)
            return None, None, False, f"window_line_splice_failed:{reason}"
    else:  # old_code
        if win_code.count(located_old_code) != 1:
            dlog("text_region_promotion_skipped", session_id=session_id,
                 filename=filename, symbol=symbol_name, site=site,
                 reason="old_code_ambiguous_in_window",
                 occurrences=win_code.count(located_old_code),
                 window=f"{win_start}-{win_end}", user_id=user_id)
            return None, None, False, "old_code_ambiguous_in_window"
        full_new = win_code.replace(located_old_code, new_code, 1)
        reason = f"old_code_window_replace:{win_start}-{win_end}"

    # ── Guards: real change + verbatim-scopable before-text ──────────────
    if full_new == win_code:
        dlog("text_region_promotion_skipped", session_id=session_id,
             filename=filename, symbol=symbol_name, site=site,
             reason="noop_after_splice", window=f"{win_start}-{win_end}",
             user_id=user_id)
        return None, None, False, "noop_after_splice"

    if win_code not in file_content:
        # Should be impossible (win_code is a contiguous slice) — belt & braces
        # so QA scoping (`symbol.code in intermediate`) can never silently fall
        # back to a whole-file compare because of a slicing bug.
        dlog("text_region_promotion_skipped", session_id=session_id,
             filename=filename, symbol=symbol_name, site=site,
             reason="window_not_verbatim_in_file", window=f"{win_start}-{win_end}",
             user_id=user_id)
        return None, None, False, "window_not_verbatim_in_file"

    virtual = SymbolInfo(
        name=f"_text_region_L{win_start}_{win_end}",
        symbol_type=SymbolType.VARIABLE,
        start_line=win_start,
        end_line=win_end,
        parent=None,
        indentation=0,
        code=win_code,
        signature=(
            f"promoted text region L{win_start}-{win_end} "
            f"(model targeted '{symbol_name}' at L{sym_start}-{sym_end}; "
            f"edit span L{span_start}-{span_end}; anchor={mode})"
        ),
    )

    dlog("text_region_promotion_built", session_id=session_id,
         filename=filename, symbol=symbol_name, site=site, mode=mode,
         resolved_symbol_range=f"{sym_start}-{sym_end}",
         edit_span=f"{span_start}-{span_end}",
         window=f"{win_start}-{win_end}",
         window_code_len=len(win_code), new_code_len=len(full_new),
         virtual_name=virtual.name, reason=reason, user_id=user_id)

    return virtual, full_new, True, reason
