"""
Surgical editor — applies approved changes to files.
Best Practice #2: Minimal footprint.
Best Practice #3: Always backup before writing.
"""
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.schemas import SurgicalChange, SurgicalApplyResponse, SurgicalOperation
from database import get_setting


def _backup_file(file_path: str) -> Optional[str]:
    """Create a timestamped backup of the file before modification."""
    if get_setting("auto_backup", "true").lower() != "true":
        return None

    src = Path(file_path)
    if not src.exists():
        return None

    backup_dir = src.parent / ".surgicalai_backups"
    backup_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{src.name}.{timestamp}.bak"
    shutil.copy2(str(src), str(backup_path))
    return str(backup_path)


def apply_symbol_replacement(file_content: str, start_line: int, end_line: int, new_code: str) -> str:
    """
    Replace lines start_line..end_line (1-indexed, inclusive) with new_code.

    This is the most reliable apply path — it uses AST line numbers directly,
    no SEARCH/REPLACE matching needed. Used when the AST symbol boundaries are known.

    Parameters
    ----------
    file_content : str
        The full current content of the file.
    start_line : int
        First line of the symbol to replace (1-indexed, inclusive).
    end_line : int
        Last line of the symbol to replace (1-indexed, inclusive).
    new_code : str
        Complete replacement code for the symbol.

    Returns
    -------
    str
        Updated file content with the symbol replaced.
    """
    lines = file_content.splitlines(keepends=True)
    before = lines[: start_line - 1]
    after = lines[end_line:]  # end_line is inclusive; lines[end_line:] excludes it

    if new_code and not new_code.endswith("\n"):
        new_code += "\n"

    return "".join(before) + new_code + "".join(after)


def _extract_core_diff(original_code: str, new_code: str):
    """
    Find the lines that actually changed between original and new code.
    Returns (orig_core_lines, new_core_lines, start_offset, end_offset)
    where offsets are relative to the start of original_code.
    """
    orig_lines = original_code.splitlines(keepends=True)
    new_lines = new_code.splitlines(keepends=True)

    # Find first differing line from the top
    top = 0
    while top < len(orig_lines) and top < len(new_lines):
        if orig_lines[top] != new_lines[top]:
            break
        top += 1

    # Find first differing line from the bottom
    bot_orig = len(orig_lines) - 1
    bot_new = len(new_lines) - 1
    while bot_orig > top and bot_new > top:
        if orig_lines[bot_orig] != new_lines[bot_new]:
            break
        bot_orig -= 1
        bot_new -= 1

    orig_core = orig_lines[top:bot_orig + 1]
    new_core = new_lines[top:bot_new + 1]
    return orig_core, new_core, top, len(orig_lines) - bot_orig - 1


def _find_nearest(file_content: str, find_text: str, hint_line: int) -> int:
    """
    Find the character index of `find_text` in `file_content`, preferring
    the occurrence nearest to `hint_line`.  Returns -1 if not found.
    """
    if not find_text:
        return -1

    # Collect ALL occurrences
    occurrences = []
    start = 0
    while True:
        idx = file_content.find(find_text, start)
        if idx == -1:
            break
        occurrences.append(idx)
        start = idx + 1

    if not occurrences:
        return -1
    if len(occurrences) == 1:
        return occurrences[0]

    # Pick the occurrence whose line number is closest to hint_line
    def _line_of(char_idx):
        return file_content[:char_idx].count("\n") + 1

    return min(occurrences, key=lambda idx: abs(_line_of(idx) - hint_line))


def _relocate_original_code(file_content: str, original_code: str, hint_start: int) -> tuple:
    """
    Relocate original_code in the current file when stale line numbers
    don't match.  Returns (new_start, new_end) or None if not found.
    Uses _find_nearest for proximity-aware search.
    """
    if not original_code or not original_code.strip():
        return None
    idx = _find_nearest(file_content, original_code.strip(), hint_start)
    if idx == -1:
        return None
    new_start = file_content[:idx].count("\n") + 1
    new_end = new_start + original_code.strip().count("\n")
    return (new_start, new_end)

def apply_operations(
    file_content: str,
    operations: list,
    hint_line: int = 1
) -> str:
    """
    v3.4.0: Apply search-and-replace operations mechanically.

    Each operation is {"find": "...", "replace": "..."}.
    The find text is located in the FULL file (not just a window),
    preferring matches near hint_line for disambiguation.

    This is the Tasklet pattern: the LLM decides WHAT to change,
    the machine does the actual editing.  Zero truncation risk.
    """
    result = file_content

    for op in operations:
        find_text = op.get("find", "") if isinstance(op, dict) else op.find
        replace_text = op.get("replace", "") if isinstance(op, dict) else op.replace

        if not find_text:
            continue

        idx = _find_nearest(result, find_text, hint_line)
        if idx == -1:
            # Try whitespace-normalized match as fallback
            norm_find = " ".join(find_text.split())
            # Rebuild file with normalized whitespace for matching
            lines = result.splitlines(keepends=True)
            found_range = None
            find_lines = find_text.splitlines()
            for i in range(len(lines)):
                if i + len(find_lines) > len(lines):
                    break
                match = True
                for j, fl in enumerate(find_lines):
                    if " ".join(lines[i + j].split()) != " ".join(fl.split()):
                        match = False
                        break
                if match:
                    found_range = (i, i + len(find_lines))
                    break

            if found_range:
                # Replace the matched lines
                before = "".join(lines[:found_range[0]])
                after = "".join(lines[found_range[1]:])
                result = before + replace_text + ("\n" if replace_text and not replace_text.endswith("\n") else "") + after
                continue

            raise ValueError(
                f"Couldn't find the exact code to modify. This usually means the file was "
                f"edited since it was uploaded, or there's a minor whitespace difference. "
                f"Looking for: `{find_text[:80].strip()}...` "
                f"Try re-uploading the latest version of the file and asking again."
            )

        # Apply the replacement
        result = result[:idx] + replace_text + result[idx + len(find_text):]

    return result


def apply_change(file_content: str, change) -> str:
    """
    Apply a single SurgicalChange to file_content and return the updated content.

    Apply strategy (in priority order):

    1. Symbol-replacement path (PRIMARY — most reliable):
       Used when:
         - change.new_code is set, AND
         - change.symbol has valid start_line / end_line, AND
         - (change.operations is empty  OR  change.operations contains the sentinel
           {"find": symbol.code, "replace": new_code} pattern from the Claude pipeline)
       Calls apply_symbol_replacement() — zero risk of whitespace mismatch.

    2. Operations path (SEARCH/REPLACE):
       Used when change.operations is non-empty and the sentinel pattern is NOT detected.
       Iterates change.operations and performs exact text find-and-replace.
       Raises ValueError if NO operations matched — prevents silent no-op returns.

    3. Fallback:
       If neither path produces a change, returns file_content unchanged.

    Parameters
    ----------
    file_content : str
        The full current content of the file.
    change : SurgicalChange
        The change object.  Expected attributes:
          - new_code     : str | None
          - original_code: str | None
          - symbol       : object with .start_line, .end_line, .code
          - operations   : list[dict] with keys "find" / "replace"
          - applied      : bool  (set to True on success)

    Returns
    -------
    str
        Updated file content (or unchanged content if apply failed).
    """
    new_code = getattr(change, "new_code", None) or ""
    original_code = getattr(change, "original_code", None) or ""
    operations = getattr(change, "operations", None) or []
    symbol = getattr(change, "symbol", None)

    def _op_get(op, key, default=""):
        return op.get(key, default) if isinstance(op, dict) else getattr(op, key, default)

    # ── Companion (file-level) operations — session 0183c92e fix ────────
    # The pipeline may attach extra find/replace ops AFTER the sentinel
    # {"find": symbol.code, "replace": new_code} op — e.g. an import-line fix
    # produced by the QA correction loop that lives OUTSIDE the symbol.
    # Detect the sentinel-first pattern here so the symbol edit still uses the
    # reliable line-number path and the companions apply mechanically after it.
    companion_ops: list = []
    _sentinel_first = False
    if new_code and symbol is not None and operations:
        _sym_code_probe = (getattr(symbol, "code", None) or original_code or "").strip()
        if (
            _sym_code_probe
            and _op_get(operations[0], "find", "").strip() == _sym_code_probe
            and _op_get(operations[0], "replace", "").strip() == new_code.strip()
        ):
            _sentinel_first = True
            companion_ops = list(operations[1:])

    def _apply_companion_ops(content: str) -> str:
        for _cop in companion_ops:
            _f = _op_get(_cop, "find", "")
            _r = _op_get(_cop, "replace", "")
            if not _f:
                continue
            if _f in content:
                content = content.replace(_f, _r, 1)
            elif _r and _r in content:
                # Already applied (idempotent re-apply) — skip.
                continue
            else:
                import logging as _logging
                _logging.getLogger("surgical_editor").warning(
                    "companion op did not match file (find=%r...) — skipped",
                    _f[:80],
                )
        return content

    # ── Idempotent re-apply detection (session 52802d58 apply-409 fix) ──
    # If this change was ALREADY applied (its new_code is present verbatim in
    # the file and its original_code no longer is), re-applying it used to fail
    # every strategy and raise ValueError → 409 to the client, even though the
    # file is already in the desired state (proven: Layout.tsx lines 14-92
    # applied+saved at 21:54:25, identical re-apply 409'd at 21:54:52 in server
    # log 1783375017500).  Treat that as a no-op success instead.
    # NOTE on the original_code test below: for REPLACEMENT changes the old
    # code must be gone.  But for INSERTION-type changes new_code CONTAINS
    # original_code (the surrounding context anchor), so original_code never
    # disappears from the file — and every re-apply used to insert ANOTHER
    # copy (proven: SIDEBAR_PINNED_KEY constants block duplicated 5x in
    # Sidebar.tsx, session 52802d58; reproduced 1→2→3 duplications with this
    # exact function).  In that case new_code being present verbatim is
    # sufficient proof the change is already applied.
    if (
        new_code
        and original_code
        and len(new_code.strip()) >= 50
        and new_code.strip() in file_content
        and new_code.strip() != original_code.strip()
        and (
            original_code.strip() not in file_content       # replacement: old gone
            or original_code.strip() in new_code.strip()    # insertion: anchor kept by design
        )
    ):
        change.applied = True
        # Companion ops (e.g. an import-line fix) must still land even when the
        # symbol edit itself is already present. _apply_companion_ops is
        # idempotent, so a full re-apply stays a no-op.
        return _apply_companion_ops(file_content)

    # ------------------------------------------------------------------
    # Determine whether to use the symbol-replacement (line-number) path
    # ------------------------------------------------------------------
    can_use_line_replace = False
    start_line = None
    end_line = None

    if new_code and symbol is not None:
        sl = getattr(symbol, "start_line", None)
        el = getattr(symbol, "end_line", None)
        if sl and el and isinstance(sl, int) and isinstance(el, int) and sl >= 1 and el >= sl:
            start_line = sl
            end_line = el

            if not operations:
                # No operations at all — use line-replace
                can_use_line_replace = True
            else:
                # Check for the sentinel pattern: single operation whose "find"
                # is the original symbol code (set by analyze_and_plan_stream)
                sym_code = getattr(symbol, "code", None) or original_code
                def _op_field(op, key, default=""):
                    return op.get(key, default) if isinstance(op, dict) else getattr(op, key, default)
                if (
                    len(operations) == 1
                    and _op_field(operations[0], "find", "").strip() == sym_code.strip()
                    and _op_field(operations[0], "replace", "").strip() == new_code.strip()
                ):
                    can_use_line_replace = True

    # ------------------------------------------------------------------
    # Path 1: Symbol-replacement (line numbers) — with stale-line guard
    # ------------------------------------------------------------------
    if can_use_line_replace:
        # ── Stale line-number detection ──
        # When edits are applied one-at-a-time (not via applyAll), previous
        # edits shift line numbers.  Detect and relocate before slicing.
        file_lines = file_content.split("\n")
        slice_content = "\n".join(file_lines[start_line - 1 : end_line]).strip()
        anchor = (original_code or "").strip()
        _effective_start, _effective_end = start_line, end_line

        if anchor and slice_content != anchor:
            relocated = _relocate_original_code(file_content, anchor, start_line)
            if relocated:
                _effective_start, _effective_end = relocated
                import logging as _logging
                _logging.getLogger("surgical_editor").info(
                    "Stale line-number fix: relocated %d-%d → %d-%d",
                    start_line, end_line, _effective_start, _effective_end
                )
            else:
                # Could not relocate — fall through to Path 2/3
                import logging as _logging
                _logging.getLogger("surgical_editor").warning(
                    "Stale lines %d-%d: content mismatch, relocation failed — falling through",
                    start_line, end_line
                )
                can_use_line_replace = False

        if can_use_line_replace:
            updated = apply_symbol_replacement(file_content, _effective_start, _effective_end, new_code)
            if updated != file_content:
                change.applied = True
                return updated
            # If content didn't change (new == old), still mark applied
            change.applied = True
            return updated

    # ------------------------------------------------------------------
    # Path 2: SEARCH/REPLACE operations
    # ------------------------------------------------------------------
    if operations:
        result = file_content
        any_applied = False
        failed_finds = []

        for op in operations:
            find_text = op.get("find", "") if isinstance(op, dict) else getattr(op, "find", "")
            replace_text = op.get("replace", "") if isinstance(op, dict) else getattr(op, "replace", "")
            if not find_text:
                continue
            if find_text in result:
                result = result.replace(find_text, replace_text, 1)
                any_applied = True
            else:
                # Track which find strings failed for a useful error message
                failed_finds.append(find_text[:120].strip())

        if any_applied:
            change.applied = True
            return result

        # No operations matched at all. Do NOT raise yet — Path 3 (anchor
        # replace) and Path 4 (hunk-split recovery) may still apply this
        # change deterministically (e.g. stored whole-file gap-bridge changes
        # whose sentinel op is the entire stale file). Record the detail so
        # the final error, if reached, stays specific.
        previews = "; ".join(f"`{f[:60]}...`" if len(f) > 60 else f"`{f}`" for f in failed_finds[:3])
        _ops_failure_detail = (
            f"SEARCH/REPLACE failed: none of the {len(operations)} operation(s) matched the file. "
            f"Unmatched find strings: {previews}."
        )
    else:
        _ops_failure_detail = ""

    # ------------------------------------------------------------------
    # Path 3: Direct new_code replacement using original_code as anchor
    # (last-resort fallback if we have both strings but no line numbers)
    # ------------------------------------------------------------------
    if new_code and original_code and original_code in file_content:
        result = file_content.replace(original_code, new_code, 1)
        change.applied = True
        return result

    # ------------------------------------------------------------------
    # Path 4: Hunk-split recovery (session 52802d58 stored gap-bridge fix)
    # ------------------------------------------------------------------
    # Legacy sessions contain synthetic whole-file changes whose anchor is the
    # ENTIRE analysis-time file (created by the old gap-bridge fallback, since
    # removed).  Once any other change is applied, that anchor can never match
    # again — proven by 6/6 apply failures on "lines 1-991" in server logs.
    # Recovery: diff original_code vs new_code into minimal hunks, relocate
    # each hunk's old-side text (with context) in the CURRENT file, and apply
    # only if EVERY hunk matches exactly once.  Deterministic — no fuzzy
    # matching; any ambiguity falls through to the error below.
    if new_code and original_code and new_code != original_code:
        _recovered = _hunk_split_recover(file_content, original_code, new_code)
        if _recovered is not None and _recovered != file_content:
            try:
                from services.pipeline import _dlog as _dl4
                _sym4 = getattr(symbol, "full_path", None) or getattr(symbol, "name", "?") if symbol else "?"
                _dl4("apply_change_hunk_recovery_success", symbol=_sym4,
                     original_len=len(original_code), new_len=len(new_code))
            except Exception:
                pass
            change.applied = True
            return _recovered

    # Nothing worked — SURFACE the failure instead of silently returning the
    # file unchanged. A silent no-op here made apply_changes_to_file count the
    # change as "applied" while the fix was actually dropped (the QA-correction
    # drop bug). Raising lets the caller report exactly which change failed.
    try:
        from services.pipeline import _dlog
    except Exception:
        def _dlog(event, **kwargs):
            pass
    _sym_name = getattr(symbol, "full_path", None) or getattr(symbol, "name", "?") if symbol else "?"
    _dlog(
        "apply_change_all_paths_failed",
        symbol=_sym_name,
        had_new_code=bool(new_code),
        had_original_code=bool(original_code),
        original_code_in_file=bool(original_code and original_code in file_content),
        operations_count=len(operations),
        line_replace_attempted=bool(start_line and end_line),
        start_line=start_line,
        end_line=end_line,
    )
    raise ValueError(
        f"Change to '{_sym_name}' could not be applied by any strategy: "
        f"line-number replacement "
        f"{'failed (stale lines — content mismatch, relocation failed)' if (start_line and end_line) else 'not available (no line numbers)'}; "
        f"{_ops_failure_detail or (str(len(operations)) + ' SEARCH/REPLACE operation(s) available')}; "
        f"original_code anchor {'not found in current file content' if original_code else 'missing'}. "
        f"The file content has changed since this edit was generated — re-analyze against the current file."
    )

def _hunk_split_recover(file_content: str, original_code: str, new_code: str):
    """Salvage a change whose anchor is stale by splitting it into hunks.

    Diffs original_code → new_code line-wise, then relocates each changed
    hunk (old-side lines plus expanding context) in the current file.  A hunk
    is applied ONLY if its old-side text occurs exactly once in the current
    file.  Returns the updated content, or None if any hunk is missing or
    ambiguous (caller then raises the normal apply error).  Deterministic:
    exact string matching only.
    """
    import difflib

    old_lines = original_code.split("\n")
    new_lines = new_code.split("\n")
    cur_lines = file_content.split("\n")
    n_cur = len(cur_lines)

    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    raw = [(a1, a2, b1, b2) for tag, a1, a2, b1, b2 in sm.get_opcodes() if tag != "equal"]
    if not raw or len(raw) > 50:
        return None

    def _find_unique(seq):
        """Return start index of the single occurrence of seq in cur_lines, else None."""
        if not seq:
            return None
        hits = []
        first = seq[0]
        for i in range(n_cur - len(seq) + 1):
            if cur_lines[i] == first and cur_lines[i:i + len(seq)] == seq:
                hits.append(i)
                if len(hits) > 1:
                    return None
        return hits[0] if len(hits) == 1 else None

    # Resolve each hunk to a (start, end, replacement_lines) span in cur_lines.
    spans = []
    for a1, a2, b1, b2 in raw:
        located = None
        for ctx in (2, 4, 8, 16):
            ca1, ca2 = max(0, a1 - ctx), min(len(old_lines), a2 + ctx)
            pre = old_lines[ca1:a1]
            post = old_lines[a2:ca2]
            seq = pre + old_lines[a1:a2] + post
            if not seq:
                continue
            idx = _find_unique(seq)
            if idx is not None:
                replacement = pre + new_lines[b1:b2] + post
                located = (idx, idx + len(seq), replacement)
                break
        if located is None:
            return None
        spans.append(located)

    # Reject overlapping spans (context expansion can collide) and apply
    # bottom-up so earlier indices stay valid.
    spans.sort(key=lambda s: s[0])
    for prev, nxt in zip(spans, spans[1:]):
        if nxt[0] < prev[1]:
            return None
    for start, end, replacement in reversed(spans):
        cur_lines[start:end] = replacement
    return "\n".join(cur_lines)


def apply_changes_to_file(
    file_path: str,
    changes: list[SurgicalChange],
    change_ids: Optional[list[str]] = None,
    file_content: Optional[str] = None
) -> SurgicalApplyResponse:
    """
    Apply one or more approved changes to a file.
    Creates backup first. Applies changes in reverse line order to preserve positions.
    If file_content is provided and the file does not exist on disk (uploaded in cloud mode),
    applies changes in-memory and returns result without writing to disk.
    """
    on_disk = os.path.exists(file_path)

    if not on_disk and not file_content:
        raise FileNotFoundError(f"File not found: {file_path}")

    if on_disk:
        with open(file_path, "r", encoding="utf-8") as f:
            original_content = f.read()
    else:
        original_content = file_content

    # Filter to only requested changes
    to_apply = changes
    if change_ids:
        to_apply = [c for c in changes if c.id in change_ids]

    if not to_apply:
        raise ValueError("No matching changes to apply.")

    # Sort by line number descending — apply bottom-up to preserve line positions.
    # EXCEPTION: "_gap_bridge" changes are whole-file anchors (start_line=1,
    # end_line=file_total). If applied last (their natural sort position, since
    # start_line=1 is always smallest), their anchor is the ENTIRE ORIGINAL FILE —
    # which can never match after any other edit has already changed the file,
    # and can never be found again by substring relocation either. So gap_bridge
    # must be applied FIRST, establishing the correct baseline; the other edits'
    # own stale-line relocation (by their own small, unique anchor text) then
    # safely absorbs any line-number shift gap_bridge introduces.
    try:
        from services.pipeline import _dlog
    except Exception:
        def _dlog(event, **kwargs):
            pass

    _gap_bridge_count = sum(
        1 for c in to_apply if getattr(c.symbol, "name", "") == "_gap_bridge"
    )
    _dlog(
        "apply_all_sort_start",
        total_changes=len(to_apply),
        gap_bridge_count=_gap_bridge_count,
        change_names=[getattr(c.symbol, "name", "") for c in to_apply],
        change_start_lines=[getattr(c.symbol, "start_line", None) for c in to_apply],
    )

    to_apply_sorted = sorted(
        to_apply,
        key=lambda c: (getattr(c.symbol, "name", "") != "_gap_bridge", -c.symbol.start_line),
    )

    _dlog(
        "apply_all_sort_result",
        sorted_names=[getattr(c.symbol, "name", "") for c in to_apply_sorted],
        sorted_start_lines=[getattr(c.symbol, "start_line", None) for c in to_apply_sorted],
        gap_bridge_moved_first=(
            _gap_bridge_count > 0
            and getattr(to_apply_sorted[0].symbol, "name", "") == "_gap_bridge"
        ),
    )

    # Backup only when file exists on disk
    backup_path = _backup_file(file_path) if on_disk else None

    # Apply changes — per-change failure isolation.
    # OLD BEHAVIOR: one failed change aborted the ENTIRE batch (409 to the
    # client) and rolled back everything, so 4 good edits died with 1 bad one.
    # NEW BEHAVIOR: apply every change that can be applied; collect failures
    # and report them in the response so nothing is dropped silently.
    current_content = original_content
    applied_count = 0
    failed_changes: list = []

    for change in to_apply_sorted:
        _c_sym = getattr(change.symbol, "full_path", None) or getattr(change.symbol, "name", "?")
        try:
            current_content = apply_change(current_content, change)
            applied_count += 1
            _dlog("apply_all_change_ok", symbol=_c_sym,
                  applied_count=applied_count)
        except ValueError as e:
            _dlog("apply_all_change_failed", symbol=_c_sym,
                  change_id=getattr(change, "id", None),
                  reason=str(e))
            failed_changes.append({
                "change_id": getattr(change, "id", None),
                "symbol": _c_sym,
                "reason": str(e),
            })

    _dlog("apply_all_batch_summary",
          total=len(to_apply_sorted),
          applied=applied_count,
          failed=len(failed_changes),
          failed_symbols=[f["symbol"] for f in failed_changes])

    if failed_changes and applied_count == 0:
        # Nothing applied at all — keep the old contract: raise so the
        # caller gets a hard error (surfaced as 409 by the router).
        _summary = "; ".join(
            f"{f['symbol']}: {f['reason'][:160]}" for f in failed_changes[:5]
        )
        raise ValueError(
            f"All {len(failed_changes)} change(s) failed to apply. {_summary}"
        )

    # v3.3.1: HTML structure validation — reject if critical structure was lost
    _ext = Path(file_path).suffix.lower()
    if _ext in ('.html', '.htm'):
        _lower = current_content.lower()
        _orig_lower = original_content.lower()
        _critical = ['<html', '</html>', '<body', '</body>']
        _lost = [tag for tag in _critical
                 if tag in _orig_lower and tag not in _lower]
        if _lost:
            if applied_count > 0 and backup_path:
                shutil.copy2(backup_path, file_path)
            raise ValueError(
                f"Apply rejected: the change removed critical HTML structure "
                f"({', '.join(_lost)}). This usually means the Surgeon truncated "
                f"a large script block. Please re-analyze the file."
            )
        # Check that file didn't shrink by more than 15% (catches accidental deletion)
        _orig_size = len(original_content)
        _new_size  = len(current_content)
        if _orig_size > 1000 and _new_size < _orig_size * 0.85:
            if applied_count > 0 and backup_path:
                shutil.copy2(backup_path, file_path)
            raise ValueError(
                f"Apply rejected: the result is {round((_orig_size - _new_size)/_orig_size*100)}% "
                f"smaller than the original ({_orig_size:,} -> {_new_size:,} bytes). "
                f"This likely means content was accidentally deleted. Please re-analyze."
            )

    # Write final content only when file exists on disk
    if on_disk:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(current_content)
        return SurgicalApplyResponse(
            file_path=file_path,
            new_content=current_content,
            applied_count=applied_count,
            backup_path=backup_path,
            cloud_mode=False,
            modified_content=current_content,
            failed_count=len(failed_changes),
            failed_changes=failed_changes
        )
    else:
        # Cloud/in-memory mode — file was uploaded, not on disk
        return SurgicalApplyResponse(
            file_path=file_path,
            new_content=current_content,
            applied_count=applied_count,
            backup_path=None,
            cloud_mode=True,
            modified_content=current_content,
            failed_count=len(failed_changes),
            failed_changes=failed_changes
        )


def restore_backup(file_path: str, backup_path: str) -> bool:
    """Restore a file from its backup."""
    if not os.path.exists(backup_path):
        return False
    shutil.copy2(backup_path, file_path)
    return True


def list_backups(file_path: str) -> list[dict]:
    """List all backups for a file."""
    src = Path(file_path)
    backup_dir = src.parent / ".surgicalai_backups"

    if not backup_dir.exists():
        return []

    backups = []
    for f in sorted(backup_dir.glob(f"{src.name}.*.bak"), reverse=True):
        backups.append({
            "path": str(f),
            "timestamp": f.stem.split(".")[-1],
            "size": f.stat().st_size
        })

    return backups
