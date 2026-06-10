"""
File size validation for SurgicalAI uploads.

Limits:
  - Text/code files: 1 MB per file
  - Image files:    10 MB per file
  - Session total:  20 MB across all files
"""

from fastapi import HTTPException

MAX_TEXT_FILE_BYTES  = 1  * 1024 * 1024   # 1 MB
MAX_IMAGE_FILE_BYTES = 10 * 1024 * 1024   # 10 MB
MAX_SESSION_BYTES    = 20 * 1024 * 1024   # 20 MB

IMAGE_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "webp", "gif", "bmp", "svg", "heic", "heif",
})


def _file_limit(filename: str) -> int:
    """Return max allowed bytes based on file extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return MAX_IMAGE_FILE_BYTES if ext in IMAGE_EXTENSIONS else MAX_TEXT_FILE_BYTES


def validate_file_size(filename: str, size_bytes: int) -> None:
    """Raise HTTP 413 if the file exceeds its type-specific limit."""
    limit = _file_limit(filename)
    if size_bytes > limit:
        limit_mb = limit / (1024 * 1024)
        size_mb  = size_bytes / (1024 * 1024)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        kind = "Image" if ext in IMAGE_EXTENSIONS else "Text/code"
        raise HTTPException(
            status_code=413,
            detail=f"{kind} file too large: {size_mb:.1f}MB (limit: {limit_mb:.0f}MB)",
        )


def validate_session_total(current_total: int, new_bytes: int) -> None:
    """Raise HTTP 413 if adding this file would exceed the session limit."""
    if current_total + new_bytes > MAX_SESSION_BYTES:
        total_mb = MAX_SESSION_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"Session file limit exceeded ({total_mb:.0f}MB max)",
        )
