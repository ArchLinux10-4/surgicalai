"""
DataLab profiler — build a compact, Claude-friendly JSON profile of a workbook.

The profile is what Claude reasons over — NOT the full data. It carries schema,
inferred semantic type hints, null counts, uniqueness, value ranges, and a small
row sample per sheet. This keeps token cost bounded and makes correctness
independent of Claude ever seeing every row.

Inference is hint-only and never mutates the underlying strings.
"""
from __future__ import annotations

import re
from typing import List

from .config import MAX_ROWS_PROFILE_SAMPLE
from .loader import LoadedWorkbook, Sheet

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_DATE_RE = re.compile(
    r"^(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})"
    r"([ T]\d{1,2}:\d{2}(:\d{2})?)?$"
)
_BOOL_VALUES = {"true", "false", "yes", "no", "0", "1", "y", "n", "t", "f"}


def _looks_zero_padded(values: List[str]) -> bool:
    """Detect leading-zero codes (ZIP, account ids) so we never numeric-coerce."""
    seen = False
    for v in values:
        if v and _INT_RE.match(v):
            if len(v) > 1 and v[0] == "0":
                seen = True
            elif v[0] == "0" and len(v) == 1:
                continue
    return seen


def _infer_type(values: List[str]) -> str:
    """Hint-only semantic type for a column's non-empty sample."""
    non_empty = [v for v in values if v != "" and v is not None]
    if not non_empty:
        return "empty"
    if _looks_zero_padded(non_empty):
        return "text"  # zero-padded codes must stay text
    checks = {"int": 0, "float": 0, "date": 0, "bool": 0}
    for v in non_empty:
        s = v.strip()
        if _INT_RE.match(s):
            checks["int"] += 1
        if _FLOAT_RE.match(s):
            checks["float"] += 1
        if _DATE_RE.match(s):
            checks["date"] += 1
        if s.lower() in _BOOL_VALUES:
            checks["bool"] += 1
    n = len(non_empty)
    if checks["date"] == n:
        return "date"
    if checks["int"] == n:
        return "int"
    if checks["float"] == n:
        return "float"
    if checks["bool"] == n:
        return "bool"
    return "text"


def _column_profile(name: str, col_values: List[str]) -> dict:
    non_empty = [v for v in col_values if v != "" and v is not None]
    null_count = len(col_values) - len(non_empty)
    inferred = _infer_type(col_values)
    prof = {
        "name": name,
        "inferred_type": inferred,
        "null_count": null_count,
        "non_null": len(non_empty),
        "distinct": len(set(non_empty)),
    }
    if inferred in ("int", "float") and non_empty:
        try:
            nums = [float(v) for v in non_empty]
            prof["min"] = min(nums)
            prof["max"] = max(nums)
        except Exception:
            pass
    # a few example values (deduped, short)
    examples = []
    for v in non_empty:
        if v not in examples:
            examples.append(v)
        if len(examples) >= 5:
            break
    prof["examples"] = examples
    return prof


def _sheet_profile(sheet: Sheet) -> dict:
    cols = sheet.columns
    # transpose only the sampled rows for column stats (bounded cost)
    sample_rows = sheet.rows[:max(MAX_ROWS_PROFILE_SAMPLE, 200)]
    col_profiles = []
    for ci, cname in enumerate(cols):
        col_values = [r[ci] if ci < len(r) else "" for r in sample_rows]
        col_profiles.append(_column_profile(cname, col_values))
    return {
        "name": sheet.name,
        "row_count": sheet.row_count,
        "column_count": len(cols),
        "columns": col_profiles,
        "sample": [
            dict(zip(cols, r)) for r in sheet.rows[:MAX_ROWS_PROFILE_SAMPLE]
        ],
    }


def build_profile(wb: LoadedWorkbook, filename: str) -> dict:
    """Top-level profile sent to Claude for transform authoring."""
    return {
        "filename": filename,
        "kind": wb.kind,
        "sheet_count": len(wb.sheets),
        "total_rows": wb.total_rows,
        "sheets": [_sheet_profile(s) for s in wb.sheets],
    }
