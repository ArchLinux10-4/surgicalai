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
                f"Cannot find target text in file (search-and-replace failed). "
                f"Text not found: {find_text[:120]}..."
            )

        # Apply the replacement
        result = result[:idx] + replace_text + result[idx + len(find_text):]

    return result


def apply_change(
    file_path: str,
    file_content: str,
    change: SurgicalChange
) -> str:
    """
    Apply a single surgical change to file content.

    v3.4.0 primary path: operations-based search-and-replace.
    Legacy fallback: 4-tier matching strategy for pre-v3.4.0 changes.

    Returns the new file content.
    """
    # ── v3.4.0: Operations-based apply (primary path) ──────────────────────
    if change.operations:
        hint = getattr(change.symbol, 'target_line', None) or change.symbol.start_line
        return apply_operations(
            file_content,
            change.operations,
            hint_line=hint
        )

    # ── Legacy path (v3.3.x and earlier changes) ──────────────────────────
    lines = file_content.splitlines(keepends=True)
    symbol = change.symbol
    hint_line = getattr(symbol, 'target_line', None) or symbol.start_line

    # -- Tier -1: INSERT_BEFORE mode (v3.3.1) -----------------------------------
    # Used for safe script injection — never replaces existing content.
    # Finds insert_anchor in the file and inserts replacement just before it.
    _ins_mode   = getattr(change, 'insert_mode', False)
    _ins_anchor = getattr(change, 'insert_anchor', None)
    _ins_repl   = getattr(change, 'replacement', None)

    if _ins_mode and _ins_anchor and _ins_repl:
        # Find insert_anchor line in file (search near hint_line first, then full file)
        anchor_strip = _ins_anchor.strip()
        anchor_found_at = None
        search_ranges = [
            range(max(0, hint_line - 1), min(len(lines), hint_line + 2000)),
            range(len(lines))
        ]
        for sr in search_ranges:
            if anchor_found_at is not None:
                break
            for i in sr:
                if lines[i].strip() == anchor_strip:
                    anchor_found_at = i
                    break

        if anchor_found_at is not None:
            # Build insertion: ensure replacement ends with newline
            repl_text = _ins_repl.rstrip("\n") + "\n"
            new_file_lines = (
                lines[:anchor_found_at] +
                [repl_text] +
                lines[anchor_found_at:]
            )
            return "".join(new_file_lines)
        # anchor not found — fall through to normal tiers as last resort

    # -- Tier 0: target_element matching (primary path v3.3.0+) ---------------
    # target_element is the minimal block of lines that actually changed.
    # Narrower than the full window so overlapping windows don't interfere.
    tgt_elem = getattr(change, 'target_element', None)
    repl_elem = getattr(change, 'replacement', None)
    if tgt_elem:
        tgt_lines = tgt_elem.splitlines(keepends=True)
        first_tgt = tgt_lines[0].strip() if tgt_lines else ""

        # Search near hint_line first (fast), then whole file
        tgt_found_at = None
        search_ranges = [
            range(max(0, hint_line - 200), min(len(lines), hint_line + 200)),
            range(len(lines))
        ]
        for search_range in search_ranges:
            if tgt_found_at is not None:
                break
            for i in search_range:
                if lines[i].strip() == first_tgt:
                    candidate = "".join(lines[i:i + len(tgt_lines)]).rstrip()
                    if candidate == tgt_elem.rstrip():
                        tgt_found_at = i
                        break

        if tgt_found_at is not None:
            repl = (repl_elem or "").rstrip()
            if repl and not repl.endswith("\n"):
                repl += "\n"
            new_file_lines = (
                lines[:tgt_found_at] +
                ([repl] if repl else []) +
                lines[tgt_found_at + len(tgt_lines):]
            )
            return "".join(new_file_lines)
        # target_element not found -- fall through to window matching

    # -- Tier 1: exact full-window match at expected line numbers --------------
    target_lines = lines[symbol.start_line - 1:symbol.end_line]
    current_block = "".join(target_lines).rstrip()
    expected_block = change.original_code.rstrip()

    if current_block == expected_block:
        actual_start = symbol.start_line - 1
        actual_end = symbol.end_line
    else:
        # -- Tier 2: full-window fuzzy scan (block may have shifted) ----------
        orig_lines = change.original_code.splitlines(keepends=True)
        first_line = orig_lines[0].strip() if orig_lines else ""

        found_at = None
        for i, line in enumerate(lines):
            if line.strip() == first_line:
                candidate = "".join(lines[i:i + len(orig_lines)]).rstrip()
                if candidate == change.original_code.rstrip():
                    found_at = i
                    break

        if found_at is not None:
            actual_start = found_at
            actual_end = found_at + len(orig_lines)
        else:
            # -- Tier 3: on-the-fly core-diff scan ----------------------------
            orig_core, new_core, _t, _b = _extract_core_diff(
                change.original_code, change.new_code or ""
            )
            if not orig_core:
                raise ValueError(
                    f"Cannot apply change to '{symbol.full_path}': "
                    f"no differing lines found between original and new code."
                )
            core_first = orig_core[0].strip()
            core_found_at = None
            search_ranges2 = [
                range(max(0, hint_line - 150), min(len(lines), hint_line + 150)),
                range(len(lines))
            ]
            for search_range in search_ranges2:
                if core_found_at is not None:
                    break
                for i in search_range:
                    if lines[i].strip() == core_first:
                        candidate_core = "".join(lines[i:i + len(orig_core)]).rstrip()
                        if candidate_core == "".join(orig_core).rstrip():
                            core_found_at = i
                            break

            if core_found_at is None:
                raise ValueError(
                    f"Cannot apply change to '{symbol.full_path}': "
                    f"the target code was not found — it may have already been "
                    f"applied or modified by another change in this set. "
                    f"Re-analyze the file if the issue persists."
                )
            new_core_block = "".join(new_core)
            if new_core_block and not new_core_block.endswith("\n"):
                new_core_block += "\n"
            return "".join(
                lines[:core_found_at] +
                ([new_core_block] if new_core_block else []) +
                lines[core_found_at + len(orig_core):]
            )

        actual_start = found_at
        actual_end = found_at + len(orig_lines)

    # Build new content (Tier 1 / Tier 2 path)
    if change.new_code:
        new_block = change.new_code
        if not new_block.endswith("\n"):
            new_block += "\n"
        new_lines = lines[:actual_start] + [new_block] + lines[actual_end:]
    else:
        # Delete: remove the block
        new_lines = lines[:actual_start] + lines[actual_end:]

    return "".join(new_lines)


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

    # Sort by line number descending — apply bottom-up to preserve line positions
    to_apply_sorted = sorted(to_apply, key=lambda c: c.symbol.start_line, reverse=True)

    # Backup only when file exists on disk
    backup_path = _backup_file(file_path) if on_disk else None

    # Apply changes
    current_content = original_content
    applied_count = 0

    for change in to_apply_sorted:
        try:
            current_content = apply_change(file_path, current_content, change)
            applied_count += 1
        except ValueError as e:
            # Restore from backup if partial failure
            if applied_count > 0 and backup_path:
                shutil.copy2(backup_path, file_path)
            raise ValueError(f"Failed applying change to {change.symbol.full_path}: {e}")

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
            modified_content=current_content
        )
    else:
        # Cloud/in-memory mode — file was uploaded, not on disk
        return SurgicalApplyResponse(
            file_path=file_path,
            new_content=current_content,
            applied_count=applied_count,
            backup_path=None,
            cloud_mode=True,
            modified_content=current_content
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
