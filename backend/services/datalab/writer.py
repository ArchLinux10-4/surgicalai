"""
DataLab writer — turn a transform result (columns + string rows) into real,
downloadable file bytes, plus a markdown preview for the File Drawer.

Output format follows the source: CSV/TSV sources produce CSV; Excel sources
produce a clean .xlsx. (A SQL transform yields a derived table, so the result
is written as a fresh sheet — original cell formatting is not carried onto a
reshaped result. Narrow in-place formatting preservation is a future iteration.)
"""
from __future__ import annotations

import csv
import io
from typing import List, Tuple

PREVIEW_ROW_CAP = 200


def result_to_markdown(columns: List[str], rows: List[List[str]],
                       sheet_name: str = "Result") -> str:
    """Compact markdown table preview (mirrors the upload preview style)."""
    if not columns:
        return f"### {sheet_name}\n\n(empty result)"
    header = "| " + " | ".join(str(c) for c in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body_rows = rows[:PREVIEW_ROW_CAP]
    lines = [header, sep]
    for r in body_rows:
        cells = [("" if c is None else str(c)).replace("|", "\\|").replace("\n", " ")
                 for c in r]
        if len(cells) < len(columns):
            cells += [""] * (len(columns) - len(cells))
        lines.append("| " + " | ".join(cells[:len(columns)]) + " |")
    md = f"### {sheet_name}\n\n" + "\n".join(lines)
    if len(rows) > PREVIEW_ROW_CAP:
        md += f"\n\n... [{len(rows) - PREVIEW_ROW_CAP} more rows not shown]"
    return md


def write_csv_bytes(columns: List[str], rows: List[List[str]],
                    delimiter: str = ",") -> bytes:
    out = io.StringIO()
    w = csv.writer(out, delimiter=delimiter, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow(list(r))
    # UTF-8 with BOM so Excel opens accented text correctly
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def write_xlsx_bytes(columns: List[str], rows: List[List[str]],
                     sheet_name: str = "Result") -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    # Excel sheet titles: max 31 chars, no  []:*?/\
    safe = "".join(c for c in (sheet_name or "Result") if c not in '[]:*?/\\')[:31] or "Result"
    ws.title = safe
    if columns:
        ws.append([str(c) for c in columns])
    for r in rows:
        ws.append([("" if c is None else str(c)) for c in r])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _split_name(filename: str) -> Tuple[str, str]:
    if "." in filename:
        i = filename.rfind(".")
        return filename[:i], filename[i:]
    return filename, ""


def versioned_name(base_filename: str, target_ext: str):
    """
    Derive the next versioned name from the source filename (no DB needed).
      budget.xlsx     -> (budget_v2.xlsx, 2)
      budget_v2.xlsx  -> (budget_v3.xlsx, 3)
    Returns (new_filename, new_version_number).
    """
    import re as _re
    stem, _ = _split_name(base_filename)
    m = _re.search(r"_v(\d+)$", stem)
    if m:
        cur = int(m.group(1))
        stem = stem[: m.start()]
    else:
        cur = 1
    new_version = cur + 1
    return f"{stem}_v{new_version}{target_ext}", new_version


def write_result(columns: List[str], rows: List[List[str]],
                 source_kind: str, source_delimiter: str = ",",
                 sheet_name: str = "Result") -> Tuple[bytes, str, str, str]:
    """
    Returns (bytes, ext, mime, file_type) for the chosen output format.
    file_type matches session_files vocabulary ('csv' | 'excel').
    """
    if source_kind in ("csv", "tsv"):
        delim = "\t" if source_kind == "tsv" else (source_delimiter or ",")
        ext = ".tsv" if source_kind == "tsv" else ".csv"
        mime = "text/tab-separated-values" if source_kind == "tsv" else "text/csv"
        return write_csv_bytes(columns, rows, delim), ext, mime, "csv"
    data = write_xlsx_bytes(columns, rows, sheet_name)
    return (data, ".xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "excel")
