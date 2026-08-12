"""Multi-window correction *collapse* recovery.

Smoking gun (session 430d9711, surgical_debug_430d9711 (3).jsonl):
------------------------------------------------------------------
A large blocked symbol (`DashboardAIAssistant`, 881 lines) was routed to the
multi-window correction path (3 scattered windows). The surgeon attempted an
edit that SPANNED window 1 -> window 2 (it opened a bracket in window 1 whose
match belonged in window 2), but then REFUSED window 2
(`correction_multi_window_no_edit`: "I still need the actual message-construction
block"). The splicer faithfully reassembled the windows (line arithmetic exact:
881 + 1 + 0 = 882), but the resulting *full symbol* was structurally broken:

    correction_multi_window_fragment_rejected
      is_brace_unbalanced=true
      brace_balance_reason="mismatched bracket: found closing ')' but the
                            innermost open bracket was '['"

The brace guard CORRECTLY refused to ship the broken code -> that behaviour is
healthy and is NOT changed here. The defect is purely in *recovery*: the symbol
stayed blocked and the next retry round re-ran the IDENTICAL N-window
decomposition, reproducing the IDENTICAL imbalance (round 0 AND round 1 both
failed the same way). Meanwhile a manual re-prompt — which sees the whole
changed region at once and can make the cross-window edit atomically — healed it
cleanly in a single pass.

Root cause: the multi-window decomposition splits an edit that must be made
atomically across a window boundary; when one window is left unedited the join
is malformed. There is no splicer/guard bug to fix.

Fix (this module):
------------------
When a multi-window correction is rejected specifically for a brace/bracket
imbalance, record a per-idx "collapse" hint. On the next correction round the
routing collapses all scattered change-clusters into a SINGLE contiguous window
(by passing a very large ``merge_gap`` to ``_find_changed_windows``). A single
window routes through the already-proven single-window correction branch: one
atomic splice, no cross-window boundary, and its own brace guard still protects
against shipping broken code — exactly reproducing what the manual re-prompt did.

Why this is safe / cannot regress a passing case:
  * The hint is only ever set on ``is_brace_unbalanced`` rejection, which today
    is a GUARANTEED hard-block (zero landed cards for that symbol). Anything on
    this path can only turn a guaranteed failure into a possible success.
  * No existing splicer, guard, accept path, or the single-pass path is touched.
  * Worst case (collapsed window still imbalances) is identical to today: the
    single-window path widens context via ``_window_widen_hint`` next round and,
    failing that, the symbol stays blocked — same outcome as before.
  * No infinite loop: once collapsed the symbol is a single window (handled by
    the single-window path, which uses ``_window_widen_hint``, not this hint),
    and the whole retry loop is capped by ``MAX_QA_RETRIES``.

Every branch emits a ``_dlog`` event (passed in as ``dlog``) so the behaviour is
fully traceable in the surgical debug log, per project logging policy.
"""

from __future__ import annotations

from typing import Any, Callable

# Larger than any realistic symbol line count. Passed as ``merge_gap`` to
# ``_find_changed_windows`` so the gap-based clustering (gap > merge_gap) never
# splits -> every scattered change region falls into ONE cluster -> exactly one
# window. See _find_changed_windows clustering loop.
MW_COLLAPSE_MERGE_GAP = 10 ** 9


def record_brace_collapse_hint(
    collapse_hint: dict[int, bool],
    idx: int,
    *,
    is_brace_unbalanced: bool,
    dlog: Callable[..., Any],
    **dlog_ctx: Any,
) -> bool:
    """Record a collapse hint for ``idx`` iff the multi-window rejection was a
    brace/bracket imbalance.

    Returns True when a hint was recorded (i.e. next round will collapse).

    Only the brace-imbalance signature is handled here — that is the class of
    failure proven to be caused by an un-completable cross-window edit. Other
    multi-window rejection reasons (content-loss / duplication / content-gain)
    have their own dedicated guards and retry semantics and are deliberately
    left untouched.
    """
    if not is_brace_unbalanced:
        dlog(
            "mw_collapse_hint_skipped",
            idx=idx,
            reason="rejection_not_brace_imbalance",
            note="collapse recovery only applies to brace/bracket-imbalanced "
                 "multi-window splices; other guards handle their own retries",
            **dlog_ctx,
        )
        return False

    collapse_hint[idx] = True
    dlog(
        "mw_collapse_hint_recorded",
        idx=idx,
        note="multi-window splice was brace/bracket-unbalanced (un-completable "
             "cross-window edit) — next round will collapse scattered windows "
             "into ONE atomic window and route through the single-window path",
        **dlog_ctx,
    )
    return True


def should_collapse(idx: int, collapse_hint: dict[int, bool]) -> bool:
    """True when ``idx`` was previously brace-rejected on the multi-window path
    and should therefore be corrected as a single collapsed window this round."""
    return bool(collapse_hint.get(idx))


def build_span_collapsed_window(
    windows: list,
    original_code: str,
    broken_code: str,
    context_lines: int,
    dlog: Callable[..., Any],
    **dlog_ctx: Any,
) -> list:
    """Collapse ALREADY-COMPUTED scattered correction windows into ONE atomic
    window spanning ``[min(window_start), max(window_end)]`` (edited/broken
    line-space).

    Why this exists (session 3d9da3fd, DashboardAIAssistant.jsx):
    --------------------------------------------------------------
    ``build_collapsed_windows`` above re-runs the *diff* with a huge merge_gap.
    That only ever spans regions that actually CHANGED between original and
    broken code. But the class of bug that most needs collapsing is an
    *omission*: the surgeon referenced an identifier (``researchCountry``) in a
    changed window while its declaration — the real fix site — lives in a
    DIFFERENT, UNCHANGED region that only exists as a window because
    ``_augment_windows_with_qa_refs`` seeded it from the QA finding. A diff-only
    collapse drops that augmented window entirely, so the model never sees the
    declaration site and can never add the missing ``useState``.

    This helper instead collapses the windows the caller ALREADY built (diff
    windows + QA-ref-augmented windows) by taking their min/max bounds, so the
    single atomic window provably contains BOTH the changed usage and the
    augmented fix site. It routes through the proven single-window correction
    branch (one atomic splice, own brace guard) — exactly what a manual
    re-prompt that sees the whole region does.

    Returns a 1-element list (routes single-window) or ``[]`` (caller degrades
    to the full-symbol re-emit branch). Never raises.
    """
    if not windows:
        dlog(
            "mw_span_collapse_no_windows",
            note="no windows to collapse — caller degrades to full-symbol re-emit",
            **dlog_ctx,
        )
        return []
    try:
        broken_lines = broken_code.splitlines()
        orig_lines = original_code.splitlines()
        if not broken_lines:
            dlog("mw_span_collapse_empty_broken", **dlog_ctx)
            return []

        ws = min(int(w["window_start"]) for w in windows)
        we = max(int(w["window_end"]) for w in windows)
        ws = max(0, ws)
        we = min(len(broken_lines) - 1, we)
        if ws > we:
            dlog("mw_span_collapse_bad_range", ws=ws, we=we, **dlog_ctx)
            return []

        # ── Seam elimination (session 3d9da3fd_round5) ───────────────────
        # A min/max span of the augmented windows can still end mid-structure
        # (round5: span 1–1054 of a 1147-line JSX component — the closing
        # JSX/braces lived in the excluded 93-line tail). Asked to re-emit a
        # 92% slice, the model closed the still-open structures at the end of
        # its window (+27 lines); the untouched broken tail then followed and
        # the splice was brace-rejected: "found closing ')' but the innermost
        # open bracket was '['". No prompt reliably stops a model from
        # completing open JSX at the end of its window, so the atomic window
        # must have NO seam: snap to the ENTIRE symbol. This path only runs
        # after a guaranteed hard-block (brace-rejected multi-window), the
        # full re-emit is the proven manual-re-prompt shape, and output cost
        # is well under model caps (~13K tokens for an 1147-line symbol vs
        # 64K/128K limits). Worst case is identical to today: the Path W
        # brace guard still refuses a broken re-emit.
        _pre_snap = (ws, we)
        ws = 0
        we = len(broken_lines) - 1
        if _pre_snap != (ws, we):
            dlog(
                "mw_span_collapse_snapped_full_symbol",
                span_before_start=_pre_snap[0] + 1,
                span_before_end=_pre_snap[1] + 1,
                total_lines=len(broken_lines),
                note="span ended mid-structure — snapped to whole symbol so the "
                     "correction has no splice seam (round5: partial 1–1054 slice "
                     "of 1147-line JSX invited early structure-closing)",
                **dlog_ctx,
            )

        window_lines = broken_lines[ws:we + 1]
        numbered_broken = "\n".join(
            f"{ws + i + 1:4d} | {line}" for i, line in enumerate(window_lines)
        )
        # Same-index original mapping as _augment_windows_with_qa_refs: the
        # splice is authoritative in broken space; numbered_original is context.
        ows = min(ws, len(orig_lines) - 1) if orig_lines else 0
        owe = min(we, len(orig_lines) - 1) if orig_lines else -1
        if orig_lines and ows <= owe:
            orig_window = orig_lines[ows:owe + 1]
            numbered_original = "\n".join(
                f"{ows + i + 1:4d} | {line}" for i, line in enumerate(orig_window)
            )
        else:
            numbered_original = "(no corresponding original lines)"

        collapsed = {
            "window_start": ws,
            "window_end": we,
            "numbered_broken": numbered_broken,
            "numbered_original": numbered_original,
            "window_line_count": we - ws + 1,
            "total_edit_lines": len(broken_lines),
            "total_orig_lines": len(orig_lines),
            "changed_line_count": sum(
                int(w.get("changed_line_count", 0) or 0) for w in windows
            ),
            "cluster_index": 0,
            "total_clusters": 1,
        }
        dlog(
            "mw_span_collapse_built",
            source_window_count=len(windows),
            window_start=ws + 1,
            window_end=we + 1,
            window_line_count=collapsed["window_line_count"],
            note="augmented scattered windows collapsed into ONE atomic span "
                 "covering both the changed usage and the QA-ref fix site — "
                 "routes through the proven single-window correction path",
            **dlog_ctx,
        )
        return [collapsed]
    except Exception as exc:  # pragma: no cover - defensive
        dlog(
            "mw_span_collapse_error",
            error=str(exc),
            error_type=type(exc).__name__,
            note="span collapse raised — returning [] so caller degrades to the "
                 "full-symbol re-emit branch (safe fallback)",
            **dlog_ctx,
        )
        return []


def build_collapsed_windows(
    symbol_code: str,
    broken_code: str,
    find_changed_windows: Callable[..., list],
    context_lines: int,
    dlog: Callable[..., Any],
    **dlog_ctx: Any,
) -> list:
    """Return change-windows for ``symbol_code`` -> ``broken_code`` collapsed
    into a SINGLE contiguous window.

    Delegates entirely to the caller's proven ``_find_changed_windows`` (passed
    in) with a very large ``merge_gap`` so its own gap-based clustering merges
    every scattered change region into one cluster -> one window. This reuses
    the exact, already-tested window-building code — this module adds no new
    diffing or splicing logic of its own.

    Returns:
      * a 1-element list  -> routes through the single-window correction branch;
      * an empty list     -> caller degrades safely (full-symbol re-emit branch).

    A >1 result is not expected (merge_gap makes clustering impossible) but is
    handled defensively: it is logged and returned unchanged so the caller keeps
    its existing multi-window behaviour rather than crashing — i.e. worst case is
    identical to today.
    """
    try:
        windows = find_changed_windows(
            symbol_code,
            broken_code,
            context_lines=context_lines,
            merge_gap=MW_COLLAPSE_MERGE_GAP,
        )
    except Exception as exc:  # pragma: no cover - defensive
        dlog(
            "mw_collapse_build_error",
            error=str(exc),
            error_type=type(exc).__name__,
            note="collapse window build raised — returning [] so caller degrades "
                 "to the full-symbol re-emit branch (safe fallback)",
            **dlog_ctx,
        )
        return []

    window_count = len(windows)
    if window_count == 1:
        _w = windows[0]
        dlog(
            "mw_collapse_windows_built",
            window_count=window_count,
            window_start=_w.get("window_start", -1) + 1,
            window_end=_w.get("window_end", -1) + 1,
            window_line_count=_w.get("window_line_count", -1),
            total_edit_lines=_w.get("total_edit_lines", -1),
            note="scattered windows collapsed into ONE atomic window — will route "
                 "through the proven single-window correction/splice path",
            **dlog_ctx,
        )
    elif window_count == 0:
        dlog(
            "mw_collapse_windows_empty",
            window_count=0,
            note="find_changed_windows returned no windows — caller degrades to "
                 "full-symbol re-emit (safe fallback)",
            **dlog_ctx,
        )
    else:
        dlog(
            "mw_collapse_windows_unexpected_count",
            window_count=window_count,
            note="expected exactly 1 collapsed window with huge merge_gap; got "
                 ">1 — returning as-is so caller keeps existing behaviour (no "
                 "regression vs today)",
            **dlog_ctx,
        )

    return windows
