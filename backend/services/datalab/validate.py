"""
DataLab data-QA gate.

This REPLACES the code-surgery QA gate for the spreadsheet lane (AST/lint/tsc
are meaningless for tabular data). It checks structural integrity and guards
against degenerate output, and always emits a before/after diff so QA stays
visible to the user (shown as the "Data validated" badge + diff card).

A result fails the gate on any CRITICAL issue. Warnings inform but don't block.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .loader import LoadedWorkbook

# Words in the user's request that make an empty / much-smaller result plausible.
_REDUCING_INTENT = re.compile(
    r"\b(filter|only|keep|remove|delete|drop|exclude|where|top|first|last|"
    r"dedup|deduplicate|unique|distinct|sample|limit|head)\b", re.IGNORECASE
)


@dataclass
class QAReport:
    passed: bool
    score: int
    verdict: str
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    diff: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        parts = []
        if self.issues:
            parts.append("issues: " + "; ".join(self.issues))
        if self.warnings:
            parts.append("warnings: " + "; ".join(self.warnings))
        if not parts:
            parts.append("clean")
        return " | ".join(parts)


def validate_result(
    wb: LoadedWorkbook,
    primary_sheet_index: int,
    out_columns: Optional[List[str]],
    out_rows: Optional[List[List[str]]],
    user_request: str,
) -> QAReport:
    """Score a transform result for structural integrity + degeneracy."""
    issues: List[str] = []
    warnings: List[str] = []

    src = wb.sheets[primary_sheet_index] if 0 <= primary_sheet_index < len(wb.sheets) else None
    rows_before = src.row_count if src else 0
    cols_before = len(src.columns) if src else 0
    rows_after = len(out_rows) if out_rows is not None else 0
    cols_after = len(out_columns) if out_columns else 0

    diff = {
        "rows_before": rows_before,
        "rows_after": rows_after,
        "cols_before": cols_before,
        "cols_after": cols_after,
        "row_delta": rows_after - rows_before,
        "col_delta": cols_after - cols_before,
    }

    # CRITICAL: no schema at all
    if not out_columns:
        issues.append("result has no columns")

    # CRITICAL: degenerate empty output when input had data and the request
    # does not imply a reducing operation.
    if rows_before > 0 and rows_after == 0:
        if not _REDUCING_INTENT.search(user_request or ""):
            issues.append(
                "degenerate empty result (input had "
                f"{rows_before} rows, output 0, request was not a reducing op)"
            )
        else:
            warnings.append("result is empty (request implied a reducing op)")

    # CRITICAL: every cell empty (data wiped out)
    if out_rows:
        non_empty_cells = any(
            (c is not None and str(c) != "") for r in out_rows[:200] for c in r
        )
        if not non_empty_cells:
            issues.append("all output cells are empty (data appears wiped)")

    # WARNING: large unexplained row loss (>90%) without reducing intent
    if rows_before > 10 and rows_after > 0:
        if rows_after < rows_before * 0.1 and not _REDUCING_INTENT.search(user_request or ""):
            warnings.append(
                f"row count dropped sharply ({rows_before}→{rows_after})"
            )

    # WARNING: all columns dropped to one (possible accidental projection)
    if cols_before >= 2 and cols_after == 1:
        warnings.append(f"column count collapsed ({cols_before}→1)")

    score = 10 - 5 * len(issues) - 2 * len(warnings)
    score = max(0, min(10, score))
    passed = len(issues) == 0
    verdict = "pass" if passed else "fail"
    return QAReport(
        passed=passed, score=score, verdict=verdict,
        issues=issues, warnings=warnings, diff=diff,
    )
