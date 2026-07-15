"""
File size validation for SurgicalAI uploads.

Limits:
  - Text/code files: 1 MB per file
  - Image files:    10 MB per file
  - PDF files:      15 MB per file
  - Session total:  30 MB across all files
"""

from fastapi import HTTPException

MAX_TEXT_FILE_BYTES  = 1  * 1024 * 1024   # 1 MB
MAX_IMAGE_FILE_BYTES = 10 * 1024 * 1024   # 10 MB
MAX_PDF_FILE_BYTES   = 15 * 1024 * 1024   # 15 MB
MAX_SESSION_BYTES    = 30 * 1024 * 1024   # 30 MB

IMAGE_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "webp", "gif", "bmp", "svg", "heic", "heif",
})

PDF_EXTENSIONS = frozenset({"pdf"})


def _file_limit(filename: str) -> int:
    """Return max allowed bytes based on file extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in IMAGE_EXTENSIONS:
        return MAX_IMAGE_FILE_BYTES
    if ext in PDF_EXTENSIONS:
        return MAX_PDF_FILE_BYTES
    return MAX_TEXT_FILE_BYTES


def validate_file_size(filename: str, size_bytes: int) -> None:
    """Raise HTTP 413 if the file exceeds its type-specific limit."""
    limit = _file_limit(filename)
    if size_bytes > limit:
        limit_mb = limit / (1024 * 1024)
        size_mb  = size_bytes / (1024 * 1024)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in IMAGE_EXTENSIONS:
            kind = "Image"
        elif ext in PDF_EXTENSIONS:
            kind = "PDF"
        else:
            kind = "Text/code"
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
