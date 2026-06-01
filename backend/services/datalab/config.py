"""
DataLab configuration + feature flag.

The kill switch is read from the `datalab_enabled` setting (DB), falling back to
the DATALAB_ENABLED environment variable. Default is OFF, so a fresh deploy is
inert until an admin explicitly turns it on. No redeploy needed to flip it.
"""
from __future__ import annotations

import os

# Hard limits (v1). Conservative on purpose — guards Railway memory + Postgres.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024          # 15 MB raw spreadsheet/csv
MAX_ROWS_PROFILE_SAMPLE = 50                 # rows sent to Claude as a sample
MAX_SHEETS = 50                              # hard cap on sheets per workbook
MAX_VERSIONS_PER_FILE = 50                   # version-chain safety cap
TRANSFORM_TIMEOUT_SEC = 25                   # wall-clock cap for a transform
TRANSFORM_MAX_MEM_MB = 512                   # memory cap for transform subprocess

# Spreadsheet / tabular mime + extension detection.
SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv", ".tsv"}
SPREADSHEET_MIMES = {
    "text/csv",
    "text/tab-separated-values",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroEnabled.12",
}


def datalab_enabled() -> bool:
    """
    True only when the feature is explicitly enabled. Reads the DB setting first
    (so it can be toggled live), then falls back to the env var. Any failure to
    read returns False — fail-closed, never accidentally on.
    """
    try:
        from database import get_setting  # local import: avoid import cycle
        val = (get_setting("datalab_enabled", "") or "").strip().lower()
        if val in ("1", "true", "yes", "on"):
            return True
        if val in ("0", "false", "no", "off"):
            return False
    except Exception:
        pass
    return os.getenv("DATALAB_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def is_spreadsheet(filename: str, mime: str = "") -> bool:
    """Detect whether an upload belongs to the data lane."""
    name = (filename or "").lower()
    ext = name[name.rfind("."):] if "." in name else ""
    if ext in SPREADSHEET_EXTENSIONS:
        return True
    return (mime or "").lower().split(";")[0].strip() in SPREADSHEET_MIMES
