"""Find the session file that most likely produced a picked DOM node.

Pure functions. No Playwright and no database, so the scoring can be tested
without a browser.
"""
from __future__ import annotations

import os
import re

_NON_GRAPH = {"image", "pdf", "csv", "excel"}
_MAX_BYTES = 2_000_000


def stable_classes(classes) -> list[str]:
    """Drop Tailwind utilities (hyphen plus a digit) and CSS-module hashes."""
    out = []
    for raw in classes or []:
        token = str(raw or "").strip()
        if not token or len(token) > 80:
            continue
        if re.search(r"_[A-Za-z0-9]{5,}$", token):
            continue
        if re.search(r"-\d", token):
            continue
        out.append(token)
    return out


def _snippet(lines: list[str], line: int) -> str:
    if not lines:
        return ""
    idx = min(max(line, 1), len(lines)) - 1
    return lines[idx].strip()[:180]


def _id_hit(line: str, el_id: str) -> bool:
    escaped = re.escape(el_id)
    if re.search(rf"""id\s*=\s*(['"]){escaped}\1""", line):
        return True
    if re.search(rf"""id\s*=\s*\{{\s*['"]{escaped}['"]\s*\}}""", line):
        return True
    return False


def _class_hit(line: str, token: str, filename: str) -> bool:
    escaped = re.escape(token)
    if re.search(rf"""class(?:Name)?\s*=\s*['"][^'"]*\b{escaped}\b""", line):
        return True
    if re.search(rf"""className\s*=\s*\{{[^}}]*['"]{escaped}['"]""", line):
        return True
    if filename.endswith((".css", ".scss", ".sass", ".less")):
        if re.search(rf"""\.{escaped}(?![\w-])""", line):
            return True
    return False


def _jinja_wrapped(line: str, text: str) -> bool:
    idx = line.find(text)
    if idx < 0:
        return False
    before = line[:idx]
    after = line[idx + len(text):]
    open_at = before.rfind("{{")
    if open_at < 0 or before.rfind("}}") > open_at:
        return False
    return "}}" in after


def _basename_match(filename: str, react_file: str) -> bool:
    base = os.path.basename(react_file.replace("\\", "/"))
    if not base:
        return False
    name = filename.replace("\\", "/")
    return name == base or name.endswith("/" + base) or os.path.basename(name) == base


def locate_in_files(files: list[dict], node: dict) -> list[dict]:
    """Return up to five {file, file_id, line, score, snippet} hits, best first."""
    node = node or {}
    react = node.get("react") or {}
    if not isinstance(react, dict):
        react = {}
    react_file = str(react.get("file") or "")
    try:
        react_line = int(react.get("line") or 1)
    except (TypeError, ValueError):
        react_line = 1
    el_id = str(node.get("el_id") or node.get("elId") or node.get("id") or "")
    classes = node.get("classes")
    if not classes and isinstance(node.get("className"), str):
        classes = node["className"].split()
    tokens = stable_classes(classes)
    text = " ".join(str(node.get("text") or "").split())[:60]
    tag = str(node.get("tag") or "").lower()

    hits = []
    for src in files or []:
        if (src.get("file_type") or "") in _NON_GRAPH:
            continue
        content = src.get("content") or ""
        if len(content.encode("utf-8", "replace")) > _MAX_BYTES:
            continue
        filename = src.get("filename") or ""
        if not filename:
            continue
        lines = content.splitlines() or [""]
        weighted: list[tuple[int, int, str]] = []

        if react_file and _basename_match(filename, react_file):
            line = min(max(react_line, 1), len(lines))
            weighted.append((100, line, _snippet(lines, line)))

        if el_id:
            for i, line in enumerate(lines, 1):
                if _id_hit(line, el_id):
                    weighted.append((60, i, _snippet(lines, i)))

        class_points = 0
        class_line = None
        class_snip = ""
        for token in tokens:
            for i, line in enumerate(lines, 1):
                if _class_hit(line, token, filename):
                    class_points += 15
                    if class_line is None:
                        class_line = i
                        class_snip = _snippet(lines, i)
                    break
            if class_points >= 45:
                class_points = 45
                break
        if class_points and class_line:
            weighted.append((class_points, class_line, class_snip))

        if text and len(text) >= 3:
            for i, line in enumerate(lines, 1):
                collapsed = " ".join(line.split())
                if text not in collapsed:
                    continue
                if _jinja_wrapped(line, text):
                    continue
                weighted.append((40, i, _snippet(lines, i)))
                break

        if not weighted:
            continue
        total = sum(item[0] for item in weighted)
        best = max(weighted, key=lambda item: (item[0], -item[1]))
        line_no = best[1]
        window = lines[max(0, line_no - 3): line_no + 2]
        if tag and any(f"<{tag}" in row.lower() for row in window):
            total += 10
        hits.append({
            "file": filename,
            "file_id": src.get("id") or src.get("file_id") or "",
            "line": line_no,
            "score": total,
            "snippet": best[2],
        })

    hits.sort(key=lambda item: (-item["score"], item["file"]))
    return hits[:5]
