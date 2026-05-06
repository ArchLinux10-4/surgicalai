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


def apply_change(
    file_path: str,
    file_content: str,
    change: SurgicalChange
) -> str:
    """
    Apply a single surgical change to file content.
    Uses line-number anchoring with context guards.
    Returns the new file content.
    """
    lines = file_content.splitlines(keepends=True)
    symbol = change.symbol

    # Validate the target region still matches what we parsed
    # (guards against stale changes being applied to modified files)
    target_lines = lines[symbol.start_line - 1:symbol.end_line]
    current_block = "".join(target_lines).rstrip()
    expected_block = change.original_code.rstrip()

    if current_block != expected_block:
        # Try fuzzy match — find where the original code actually is now
        orig_lines = change.original_code.splitlines()
        first_line = orig_lines[0].strip() if orig_lines else ""

        found_at = None
        for i, line in enumerate(lines):
            if line.strip() == first_line:
                # Check if this block matches
                candidate = "".join(lines[i:i + len(orig_lines)]).rstrip()
                if candidate == change.original_code.rstrip():
                    found_at = i
                    break

        if found_at is None:
            raise ValueError(
                f"Cannot apply change to '{symbol.full_path}': "
                f"the target code has been modified since analysis. "
                f"Re-analyze the file and try again."
            )

        # Update line range to actual location
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
    change_ids: Optional[list[str]] = None
) -> SurgicalApplyResponse:
    """
    Apply one or more approved changes to a file.
    Creates backup first. Applies changes in reverse line order to preserve positions.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        original_content = f.read()

    # Filter to only requested changes
    to_apply = changes
    if change_ids:
        to_apply = [c for c in changes if c.id in change_ids]

    if not to_apply:
        raise ValueError("No matching changes to apply.")

    # Sort by line number descending — apply bottom-up to preserve line positions
    to_apply_sorted = sorted(to_apply, key=lambda c: c.symbol.start_line, reverse=True)

    # Backup
    backup_path = _backup_file(file_path)

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

    # Write final content
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(current_content)

    return SurgicalApplyResponse(
        file_path=file_path,
        new_content=current_content,
        applied_count=applied_count,
        backup_path=backup_path
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
