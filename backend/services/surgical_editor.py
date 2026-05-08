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

from models.schemas import SurgicalChange, SurgicalApplyResponse
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


def apply_change(
    file_path: str,
    file_content: str,
    change: SurgicalChange
) -> str:
    """
    Apply a single surgical change to file content.
    Uses line-number anchoring with context guards.
    Falls back to core-diff matching when windows overlap (multi-change sets).
    Returns the new file content.
    """
    lines = file_content.splitlines(keepends=True)
    symbol = change.symbol

    # Validate the target region still matches what we parsed
    target_lines = lines[symbol.start_line - 1:symbol.end_line]
    current_block = "".join(target_lines).rstrip()
    expected_block = change.original_code.rstrip()

    if current_block != expected_block:
        # --- Tier 1: full-block fuzzy scan (shifted location) ---
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
            # --- Tier 2: core-diff matching (handles overlapping windows) ---
            # Extract just the lines that actually changed between original and new
            orig_core, new_core, top_ctx, bot_ctx = _extract_core_diff(
                change.original_code, change.new_code or ""
            )

            if not orig_core:
                raise ValueError(
                    f"Cannot apply change to '{symbol.full_path}': "
                    f"no differing lines found between original and new code."
                )

            core_first = orig_core[0].strip() if orig_core else ""

            # Search within ±150 lines of target_line hint (or full file)
            hint = getattr(symbol, 'target_line', None) or symbol.start_line
            search_start = max(0, hint - 150)
            search_end = min(len(lines), hint + 150)

            core_found_at = None
            for i in range(search_start, search_end):
                if lines[i].strip() == core_first:
                    candidate_core = "".join(lines[i:i + len(orig_core)]).rstrip()
                    if candidate_core == "".join(orig_core).rstrip():
                        core_found_at = i
                        break

            if core_found_at is None:
                # Widen search to whole file
                for i, line in enumerate(lines):
                    if line.strip() == core_first:
                        candidate_core = "".join(lines[i:i + len(orig_core)]).rstrip()
                        if candidate_core == "".join(orig_core).rstrip():
                            core_found_at = i
                            break

            if core_found_at is None:
                raise ValueError(
                    f"Cannot apply change to '{symbol.full_path}': "
                    f"the target code was not found — it may have already been "
                    f"applied or was modified by another change in this set. "
                    f"Re-analyze the file if the issue persists."
                )

            # Replace only the core changed lines, leave surrounding context intact
            new_core_block = "".join(new_core)
            if new_core_block and not new_core_block.endswith("\n"):
                new_core_block += "\n"

            new_lines = (
                lines[:core_found_at] +
                ([new_core_block] if new_core_block else []) +
                lines[core_found_at + len(orig_core):]
            )
            return "".join(new_lines)

        actual_start = found_at
        actual_end = found_at + len(orig_lines)
    else:
        actual_start = symbol.start_line - 1
        actual_end = symbol.end_line

    # Build new content
    if change.new_code:
        # Modify: replace the target block
        new_block = change.new_code
        # Ensure it ends with a newline
        if not new_block.endswith("\n"):
            new_block += "\n"

        new_lines = (
            lines[:actual_start] +
            [new_block] +
            lines[actual_end:]
        )
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
