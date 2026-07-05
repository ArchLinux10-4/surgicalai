"""
Pre-QA deterministic sanity checks — flag-gated via PRE_QA_SANITY (default OFF).

Runs BEFORE the LLM QA agent. Pure local checks, no API calls, milliseconds.
Findings are heuristics handed to the QA agent as hints — never verdicts,
never gates. This module must NEVER raise: every check is individually
guarded, and run_sanity_checks always returns a well-formed dict.

Checks:
  1. python_syntax   — ast.parse on the new symbol (.py only). Only flags if
                       the ORIGINAL parsed cleanly (so we never blame the edit
                       for pre-existing breakage).
  2. bracket_balance — net (), [], {} balance changed between original and new.
                       Delta-based to dampen string/comment false positives.
  3. duplicate_defs  — a def/class name appears more times in new than original
                       (.py only). Classic surgical-edit paste error.
  4. truncation      — new code looks cut off: much shorter than original, or
                       ends mid-statement (trailing comma/operator/open bracket),
                       or has an unterminated triple-quote (.py only).
"""

import ast
import re
import textwrap


def _check_python_syntax(filename, original_code, new_code):
    if not filename.lower().endswith(".py"):
        return None
    try:
        ast.parse(textwrap.dedent(original_code))
    except SyntaxError:
        return None  # original already broken — can't blame the edit
    try:
        ast.parse(textwrap.dedent(new_code))
    except SyntaxError as e:
        return (
            f"Python syntax error in NEW CODE at line {e.lineno}: "
            f"{e.msg} — the original parsed cleanly, so the edit introduced this."
        )
    return None


def _net_balance(code, open_ch, close_ch):
    return code.count(open_ch) - code.count(close_ch)


def _check_bracket_balance(original_code, new_code):
    findings = []
    for open_ch, close_ch, label in (
        ("(", ")", "parentheses"),
        ("[", "]", "square brackets"),
        ("{", "}", "curly braces"),
    ):
        old_net = _net_balance(original_code, open_ch, close_ch)
        new_net = _net_balance(new_code, open_ch, close_ch)
        if new_net != old_net:
            findings.append(
                f"Net {label} balance changed: original {old_net:+d} vs new {new_net:+d} "
                f"— possible unclosed or extra {label} (heuristic; strings/comments can skew counts)."
            )
    return findings


_DEF_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def _count_defs(code):
    counts = {}
    for name in _DEF_RE.findall(code):
        counts[name] = counts.get(name, 0) + 1
    return counts


def _check_duplicate_defs(filename, original_code, new_code):
    if not filename.lower().endswith(".py"):
        return []
    old_counts = _count_defs(original_code)
    findings = []
    for name, new_n in _count_defs(new_code).items():
        old_n = old_counts.get(name, 0)
        if new_n > 1 and new_n > old_n:
            findings.append(
                f"'{name}' is defined {new_n}x in NEW CODE but {old_n}x in original "
                f"— possible accidental duplicate definition."
            )
    return findings


_MID_STATEMENT_ENDINGS = (",", "+", "-", "*", "/", "=", "(", "[", "{", "&&", "||", "=>", "and", "or", "not", ".")


def _check_truncation(filename, original_code, new_code):
    findings = []
    old_lines = original_code.strip().splitlines()
    new_stripped = new_code.strip()
    new_lines = new_stripped.splitlines()

    if len(old_lines) > 10 and len(new_lines) < len(old_lines) * 0.5:
        findings.append(
            f"NEW CODE is {len(new_lines)} lines vs {len(old_lines)} in original (<50%) "
            f"— verify nothing was accidentally dropped (may be a legitimate deletion)."
        )

    if new_lines:
        last = new_lines[-1].strip()
        for ending in _MID_STATEMENT_ENDINGS:
            if last.endswith(ending):
                findings.append(
                    f"NEW CODE ends with '{ending}' — looks cut off mid-statement (possible truncation)."
                )
                break

    if filename.lower().endswith(".py"):
        for quote in ('"""', "'''"):
            if new_stripped.count(quote) % 2 == 1:
                findings.append(
                    f"Odd number of {quote} in NEW CODE — possible unterminated triple-quoted string."
                )
    return findings


def run_sanity_checks(filename, original_code, new_code):
    """
    Run all deterministic checks. Never raises.
    Returns: {"findings": [str], "checked": [str], "errors": [str]}
    """
    findings = []
    checked = []
    errors = []

    original_code = original_code or ""
    new_code = new_code or ""
    filename = filename or ""

    try:
        checked.append("python_syntax")
        syntax_finding = _check_python_syntax(filename, original_code, new_code)
        if syntax_finding:
            findings.append(syntax_finding)
    except Exception as e:
        errors.append(f"python_syntax: {e}")

    try:
        checked.append("bracket_balance")
        findings.extend(_check_bracket_balance(original_code, new_code))
    except Exception as e:
        errors.append(f"bracket_balance: {e}")

    try:
        checked.append("duplicate_defs")
        findings.extend(_check_duplicate_defs(filename, original_code, new_code))
    except Exception as e:
        errors.append(f"duplicate_defs: {e}")

    try:
        checked.append("truncation")
        findings.extend(_check_truncation(filename, original_code, new_code))
    except Exception as e:
        errors.append(f"truncation: {e}")

    return {"findings": findings, "checked": checked, "errors": errors}
