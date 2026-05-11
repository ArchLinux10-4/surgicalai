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
                if (
                    len(operations) == 1
                    and operations[0].get("find", "").strip() == sym_code.strip()
                    and operations[0].get("replace", "").strip() == new_code.strip()
                ):
                    can_use_line_replace = True

    # ------------------------------------------------------------------
    # Path 1: Symbol-replacement (line numbers)
    # ------------------------------------------------------------------
    if can_use_line_replace:
        updated = apply_symbol_replacement(file_content, start_line, end_line, new_code)
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

        for op in operations:
            find_text = op.get("find", "")
            replace_text = op.get("replace", "")
            if not find_text:
                continue
            if find_text in result:
                result = result.replace(find_text, replace_text, 1)
                any_applied = True

        if any_applied:
            change.applied = True
            return result

    # ------------------------------------------------------------------
    # Path 3: Direct new_code replacement using original_code as anchor
    # (last-resort fallback if we have both strings but no line numbers)
    # ------------------------------------------------------------------
    if new_code and original_code and original_code in file_content:
        result = file_content.replace(original_code, new_code, 1)
        change.applied = True
        return result

    # Nothing worked — return unchanged
    return file_content

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
            current_content = apply_change(current_content, change)
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
