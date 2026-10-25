"""One sentence per apply failure code.

Routers copy `detail` from this dict. They do not invent a second wording.
"""

FAILURE_TEXT = {
    "already_applied": "Already applied",
    "anchor_not_found": "The file changed since this edit was generated.",
    "file_missing": "The file is not in this session.",
    "write_failed": "The file could not be saved.",
}


def annotate_failure(item: dict) -> dict:
    """Return a copy with `code` and `detail` filled from FAILURE_TEXT."""
    out = dict(item or {})
    code = out.get("code") or ("already_applied" if out.get("already_applied") else "anchor_not_found")
    if code not in FAILURE_TEXT:
        code = "anchor_not_found"
    out["code"] = code
    out["detail"] = FAILURE_TEXT[code]
    return out
