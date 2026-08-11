"""
Structural QA — fast, deterministic, zero-LLM validation.

Runs BEFORE delivery. Catches the mechanical bugs that LLM-based QA
describes in prose but never blocks on:

  - Duplicate function/component definitions
  - Missing imports (hook used but never imported)
  - Wrong import path depth (../../ vs ../ based on file nesting)
  - Partial file output (expected exports missing)
  - Syntax errors (delegates to syntax_validator)
  - Suspicious line-count shrinkage

Returns a list of issue dicts. Empty list = passed.
Each issue: {"severity": "error"|"warning", "check": str, "message": str}

Design: every check is a pure function of (new_code, filename, metadata).
No network, no LLM, no disk I/O.  Runs in <50ms for any file.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_structural_qa(
    new_code: str,
    original_code: str,
    filename: str,
    symbol_path: str = "",
    file_content: str = "",
    all_changes: Optional[List[dict]] = None,
    change_description: str = "",
) -> List[Dict]:
    """
    Run all structural checks on a single change.

    Parameters
    ----------
    new_code        : the proposed replacement code for this symbol
    original_code   : the original symbol code being replaced
    filename        : full file path (used for import depth checks)
    symbol_path     : AST symbol name (e.g. "MobileChatPanel")
    file_content    : full original file content (for cross-reference checks)
    all_changes     : list of all change dicts in this batch (for cross-change checks)
    change_description : optional plan/edit description — enables deterministic
                         plan-completeness checks (session 3a6150e9)

    Returns
    -------
    List of issue dicts. Empty = all checks passed.
    """
    issues: List[Dict] = []
    ext = _ext(filename)

    # Only run on code files
    if ext not in (".ts", ".tsx", ".js", ".jsx", ".py", ".vue", ".svelte"):
        return []

    is_ts = ext in (".ts", ".tsx", ".js", ".jsx")

    # ── Check 1: Duplicate function/component definitions ────────────────
    issues.extend(_check_duplicate_definitions(new_code, filename))

    # ── Check 2: Missing hook/module imports ─────────────────────────────
    if is_ts:
        issues.extend(_check_missing_imports(new_code, filename, file_content, all_changes))

    # ── Check 3: Import path depth ───────────────────────────────────────
    if is_ts:
        issues.extend(_check_import_depth(new_code, filename))

    # ── Check 4: Partial file / missing exports ──────────────────────────
    issues.extend(_check_completeness(new_code, original_code, filename))

    # ── Check 5: Suspicious shrinkage ────────────────────────────────────
    issues.extend(_check_shrinkage(new_code, original_code, filename, symbol_path))

    # ── Check 6: Syntax validation (tree-sitter) ────────────────────────
    issues.extend(_check_syntax(new_code, filename))

    # ── Check 7: Props interface mismatch ────────────────────────────────
    if is_ts:
        issues.extend(_check_props_mismatch(new_code, filename))

    # ── Check 8: Dead imports (imported but never used in new code) ──────
    if is_ts:
        issues.extend(_check_dead_destructured_bindings(new_code, filename))

    # ── Check 9: Plan completeness (half-implemented edits) ──────────────
    # Session 3a6150e9: Country destructured but never written to commonData;
    # NEW CODE identical to ORIGINAL for "add endpoints" plans. Deterministic
    # checks catch these before burning QA-retry budget.
    if change_description:
        try:
            issues.extend(check_plan_completeness(
                change_description, original_code, new_code
            ))
        except Exception as _pc_err:
            # Never let plan-completeness regex/parsing blow up structural QA.
            try:
                from database import _dlog as _pc_dlog
                _pc_dlog(
                    "plan_completeness_error",
                    filename=filename,
                    symbol_path=symbol_path or "",
                    error=str(_pc_err)[:300],
                    description=(change_description or "")[:200],
                )
            except Exception:
                pass

    return issues


def filter_preexisting_issues(
    issues: List[Dict],
    original_code: str,
    filename: str,
    file_content: str = "",
    all_changes: Optional[List[dict]] = None,
) -> List[Dict]:
    """Remove structural issues that already exist in the original (unedited) code.

    Prevents false-positive blocks when the original file has pre-existing
    errors (e.g. syntax errors at lines the edit never touched) that the
    surgeon didn't cause.

    Session d007eaf1: structural QA blocked on syntax errors at lines 1465 and
    2718 of a 3,459-line symbol when the edit only touched 2 lines near L1792.
    This triggered 12 unnecessary multi-window correction calls that introduced
    a NEW bug, shipping QA 3/10 instead of the surgeon's correct 9/10 edit.
    """
    orig_issues = run_structural_qa(
        original_code, original_code, filename,
        file_content=file_content,
        all_changes=all_changes or [],
    )
    if not orig_issues:
        return issues  # no pre-existing issues, nothing to filter
    orig_fingerprints = {(i["check"], i["message"]) for i in orig_issues}
    return [i for i in issues if (i["check"], i["message"]) not in orig_fingerprints]


def format_structural_feedback(issues: List[Dict]) -> str:
    """
    Format structural QA issues into a prompt block for Claude retry.
    Similar to linter_validator.format_feedback_block().
    """
    if not issues:
        return ""

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    lines = [
        f"STRUCTURAL QA FAILED ({len(errors)} error(s), {len(warnings)} warning(s)):",
        "",
    ]

    for issue in errors:
        lines.append(f"  ✗ [{issue['check']}] {issue['message']}")

    for issue in warnings:
        lines.append(f"  ⚠ [{issue['check']}] {issue['message']}")

    lines.append("")
    lines.append(
        "You MUST fix ALL errors above. Warnings should be fixed if possible. "
        "Pay close attention to: import paths matching the file's directory depth, "
        "including ALL component definitions (not just snippets), and ensuring "
        "every used hook/module has a corresponding import statement."
    )
    return "\n".join(lines)


def has_blocking_issues(issues: List[Dict]) -> bool:
    """Return True if any issue is severity=error (blocks delivery)."""
    return any(i["severity"] == "error" for i in issues)


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────

def _check_duplicate_definitions(code: str, filename: str) -> List[Dict]:
    """
    Detect duplicate function/const component definitions.
    e.g. `function ProgressSteps` defined twice in the same file.
    """
    issues = []

    # Named function declarations
    fn_names = re.findall(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", code, re.MULTILINE)
    seen = {}
    for name in fn_names:
        if name in seen:
            issues.append(_error(
                "duplicate_definition",
                f"Function '{name}' is defined {fn_names.count(name)} times. "
                f"Each function must be defined exactly once."
            ))
            break  # one report per name is enough
        seen[name] = True

    # Arrow function components: const X = (...) => { or const X: React.FC
    arrow_names = re.findall(
        r"^(?:export\s+)?const\s+(\w+)\s*(?::\s*\w+(?:\.\w+)*(?:<[^>]*>)?\s*)?=\s*(?:\([^)]*\)|[a-zA-Z_]\w*)\s*=>",
        code, re.MULTILINE
    )
    seen_arrow = {}
    for name in arrow_names:
        if name in seen_arrow:
            issues.append(_error(
                "duplicate_definition",
                f"Component/const '{name}' is defined {arrow_names.count(name)} times."
            ))
            break
        seen_arrow[name] = True

    return issues


def _check_missing_imports(
    code: str,
    filename: str,
    file_content: str = "",
    all_changes: Optional[List[dict]] = None,
) -> List[Dict]:
    """
    Detect hooks/modules that are used in the code but never imported.
    Focuses on custom hooks (useXxx) since those are the #1 failure mode.
    """
    issues = []

    # Find all import statements — in the edit snippet, in the FULL original
    # file, and in any sibling edits to the same file in this batch.
    #
    # False-positive fix (session 52802d58): symbol-scoped edits (e.g. a
    # replacement of just the `Sidebar` function) naturally contain no import
    # lines, but the file's top-of-file imports still apply. Previously only
    # `code` (the snippet) was scanned, so hooks like useAppStore that were
    # imported at file top (Sidebar.tsx L2) were flagged as "never imported"
    # and the QA score was wrongly forced to 3/10.
    def _import_lines(src: str) -> str:
        return "\n".join(
            line for line in src.splitlines()
            if line.strip().startswith("import ") or line.strip().startswith("} from ")
            or "from '" in line or 'from "' in line
        )

    import_block = _import_lines(code)
    if file_content:
        import_block += "\n" + _import_lines(file_content)
    if all_changes:
        for _ch in all_changes:
            if not isinstance(_ch, dict):
                continue
            _ch_file = _ch.get("filename") or ""
            if _ch_file and filename and _ch_file != filename:
                continue
            import_block += "\n" + _import_lines(_ch.get("new_code") or "")

    # Find all useXxx calls in the code body (not in import lines)
    non_import_code = "\n".join(
        line for line in code.splitlines()
        if not (line.strip().startswith("import ") or line.strip().startswith("} from "))
    )

    # Custom hooks: useFileUpload, useSmartStream, useSessionFiles, etc.
    hook_calls = set(re.findall(r"\buse([A-Z]\w+)\s*\(", non_import_code))
    for hook_suffix in hook_calls:
        hook_name = f"use{hook_suffix}"
        # Skip built-in React hooks
        builtins = {
            "useState", "useEffect", "useRef", "useMemo", "useCallback",
            "useContext", "useReducer", "useLayoutEffect", "useImperativeHandle",
            "useDebugValue", "useDeferredValue", "useTransition", "useId",
            "useSyncExternalStore", "useInsertionEffect", "useOptimistic",
            "useActionState", "useFormStatus",
        }
        if hook_name in builtins:
            continue

        # Check if it's imported
        if hook_name not in import_block:
            # Maybe it's defined in this same file (snippet OR full file)
            def_pattern = rf"(?:function|const)\s+{re.escape(hook_name)}\b"
            if not re.search(def_pattern, code) and not (
                file_content and re.search(def_pattern, file_content)
            ):
                issues.append(_error(
                    "missing_import",
                    f"Hook '{hook_name}' is called but never imported. "
                    f"Add: import {{ {hook_name} }} from the appropriate module."
                ))

    return issues


def _check_import_depth(code: str, filename: str) -> List[Dict]:
    """
    Verify relative import paths match the file's directory depth.

    Classic bug: file is at components/mobile/MobileChatPanel.tsx
    but imports from '../hooks/useFileUpload' (1 level up)
    when it should be '../../hooks/useFileUpload' (2 levels up).
    """
    issues = []

    # Count directory depth from common roots
    # e.g. "components/mobile/File.tsx" → depth 2 from src/
    parts = filename.replace("\\", "/").split("/")

    # Find all relative imports
    rel_imports = re.findall(
        r"""from\s+['"](\.\./[^'"]+)['"]""",
        code
    )

    for imp_path in rel_imports:
        # Count how many '../' levels
        up_count = imp_path.count("../")

        # Check if the target exists at that depth
        # Heuristic: if file is in a subdirectory (like mobile/) and imports
        # from hooks/, it needs at least 2 levels up
        if "mobile/" in filename or "mobile\\" in filename:
            # File is nested — imports to sibling dirs of parent need ../../
            if up_count == 1:
                # Check if target is a common shared directory
                target = imp_path.replace("../", "", 1)
                shared_dirs = ["hooks/", "lib/", "stores/", "api/", "types/", "utils/"]
                for sd in shared_dirs:
                    if target.startswith(sd):
                        issues.append(_error(
                            "wrong_import_depth",
                            f"Import '{imp_path}' uses '../' (1 level up) but this file is in "
                            f"a nested directory. It likely needs '../../{target}' (2 levels up) "
                            f"to reach the '{sd.rstrip('/')}' directory."
                        ))
                        break

    return issues


def _tokens_cooccur(code: str, a: str, b: str, window: int = 500) -> bool:
    """True if identifiers ``a`` and ``b`` appear within ``window`` chars."""
    if not code or not a or not b:
        return False
    pat_a = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(a)}(?![A-Za-z0-9_])")
    pat_b = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(b)}(?![A-Za-z0-9_])")
    a_pos = [m.start() for m in pat_a.finditer(code)]
    b_pos = [m.start() for m in pat_b.finditer(code)]
    if not a_pos or not b_pos:
        return False
    for ap in a_pos:
        for bp in b_pos:
            if abs(ap - bp) <= window:
                return True
    return False


def _field_in_object_block(code: str, container: str, field: str) -> bool:
    """True if ``field`` appears inside a ``container = { ... }`` / ``: {`` object.

    Session 3a6150e9: plain proximity matched Country in ``const { Country } =
    req.body`` next to ``commonData = { ... }`` even when Country was never
    written into the object body — that half-implementation must fail.
    """
    if not code or not container or not field:
        return False
    for m in re.finditer(
        rf"(?<![A-Za-z0-9_]){re.escape(container)}\s*[=:]\s*\{{",
        code,
    ):
        start = m.end() - 1  # index of '{'
        depth = 0
        for i in range(start, len(code)):
            ch = code[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body = code[start : i + 1]
                    if re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])",
                        body,
                    ):
                        return True
                    break
    return False


def check_plan_completeness(
    description: str,
    original_code: str,
    new_code: str,
) -> List[Dict]:
    """Deterministic plan-fulfillment checks (session 3a6150e9 / marketrates).

    Catches:
      1. NEW CODE identical to ORIGINAL when a plan description was provided
         (e.g. "insert new endpoints" with zero bytes changed).
      2. Half-implemented "add X to/in Y" plans — e.g. Country destructured
         from req.body but never written into commonData (LLM QA scored 3).

    Object-literal only for (2): prose like "Add logging to help with debugging"
    must NOT fire — we only enforce when ``Y`` exists as ``Y = {`` / ``Y: {``
    in the edit code (the proven failure shape).
    """
    issues: List[Dict] = []
    desc = (description or "").strip()
    if not desc:
        return issues
    orig = original_code or ""
    new = new_code or ""

    if new.strip() == orig.strip():
        issues.append(_error(
            "plan_noop",
            "NEW CODE is identical to ORIGINAL CODE — the change plan was not "
            "applied. Re-emit the edit so the planned change is actually present.",
        ))
        return issues

    pair_re = re.compile(
        r"(?i)\b(?:add|persist|include|inject|wire|set|default)\s+"
        r"[`'\"]?([A-Za-z_][\w]*)[`'\"]?"
        r"(?:\s+(?:field|key|prop|property|param|parameter|value|endpoint|route))?"
        r"\s+(?:to|into|in|on|inside|within)\s+"
        r"[`'\"]?([A-Za-z_][\w]*)[`'\"]?"
    )
    _CONTAINER_SKIP = {
        "code", "file", "function", "component", "handler", "response",
        "request", "the", "this", "that", "order", "place",
        # English / prose containers — never object bindings
        "help", "support", "ensure", "make", "see", "allow", "prevent",
        "improve", "fix", "debug", "debugging", "user", "users",
        "ui", "page", "section", "form", "screen", "view", "panel",
        "sidebar", "modal", "button", "input", "field", "fields",
        "api", "db", "database", "server", "client", "app", "project",
    }
    seen = set()
    for m in pair_re.finditer(desc):
        field, container = m.group(1), m.group(2)
        key = (field.lower(), container.lower())
        if key in seen:
            continue
        seen.add(key)
        if len(field) < 2 or len(container) < 2:
            continue
        if container.lower() in _CONTAINER_SKIP or field.lower() in _CONTAINER_SKIP:
            continue

        # Only enforce when container is a real object literal in the edit.
        # Co-occurrence fallback was dropped after FP: "Add logging to help …".
        orig_has_obj = bool(re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(container)}\s*[=:]\s*\{{", orig
        ))
        new_has_obj = bool(re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(container)}\s*[=:]\s*\{{", new
        ))
        if not (orig_has_obj or new_has_obj):
            continue

        had = _field_in_object_block(orig, container, field)
        has = _field_in_object_block(new, container, field)

        if had and not has:
            issues.append(_error(
                "plan_incomplete",
                f"Plan requires `{field}` in `{container}`, but `{field}` was "
                f"removed from `{container}` in NEW CODE.",
            ))
        elif not had and not has:
            field_in_new = bool(re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])", new
            ))
            if field_in_new:
                issues.append(_error(
                    "plan_incomplete",
                    f"Plan requires `{field}` to be added to `{container}`, but "
                    f"`{field}` appears in NEW CODE without being written into "
                    f"`{container}` (half-implemented — e.g. destructured but "
                    f"never assigned into `{container}`).",
                ))
            else:
                issues.append(_error(
                    "plan_incomplete",
                    f"Plan requires `{field}` to be added to `{container}`, but "
                    f"`{field}` is absent from `{container}` in NEW CODE.",
                ))

    return issues


def _check_completeness(
    new_code: str, original_code: str, filename: str
) -> List[Dict]:
    """
    Detect partial file output — the new code is missing exports/components
    that existed in the original.
    """
    issues = []

    if not original_code:
        return []

    # Find exported symbols in original
    orig_exports = set(re.findall(
        r"export\s+(?:default\s+)?(?:function|const|class|interface|type)\s+(\w+)",
        original_code
    ))

    if not orig_exports:
        return []

    # Find them in new code
    new_exports = set(re.findall(
        r"export\s+(?:default\s+)?(?:function|const|class|interface|type)\s+(\w+)",
        new_code
    ))

    # Also check for non-export definitions that are used elsewhere
    orig_fns = set(re.findall(
        r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)",
        original_code, re.MULTILINE
    ))
    new_fns = set(re.findall(
        r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)",
        new_code, re.MULTILINE
    ))

    missing_exports = orig_exports - new_exports
    missing_fns = orig_fns - new_fns

    # Only flag if significant symbols are missing (not just renamed)
    if missing_exports:
        # Filter out trivially small symbols (types, interfaces)
        significant = [s for s in missing_exports if len(s) > 2]
        if significant:
            issues.append(_error(
                "missing_exports",
                f"Original code exports {significant} but the new code does not. "
                f"If these were intentionally removed, ignore this. Otherwise, the "
                f"output may be a partial snippet instead of the complete file."
            ))

    if len(missing_fns) >= 2:
        issues.append(_error(
            "missing_definitions",
            f"Original code defines functions {list(missing_fns)} that are absent "
            f"from the new code. The output appears to be incomplete — ensure ALL "
            f"components and helper functions are included."
        ))

    return issues


def _check_shrinkage(
    new_code: str, original_code: str, filename: str, symbol_path: str
) -> List[Dict]:
    """
    Flag if new code is suspiciously smaller than original (>40% shrinkage).
    """
    issues = []

    orig_lines = len(original_code.splitlines())
    new_lines = len(new_code.splitlines())

    if orig_lines < 20:
        return []  # Small symbols can legitimately shrink

    shrinkage = (orig_lines - new_lines) / orig_lines

    if shrinkage > 0.40:
        issues.append(_warning(
            "suspicious_shrinkage",
            f"New code for '{symbol_path or filename}' is {new_lines} lines vs "
            f"original {orig_lines} lines ({shrinkage:.0%} smaller). "
            f"This may indicate truncated output. Verify all logic is preserved."
        ))

    return issues


def _check_syntax(code: str, filename: str) -> List[Dict]:
    """Delegate to syntax_validator if available."""
    issues = []
    try:
        from services.syntax_validator import validate_syntax
        syntax_errors = validate_syntax(code, filename)
        for err in syntax_errors[:3]:
            issues.append(_error(
                "syntax_error",
                f"Line {err.get('line', '?')}: {err.get('message', 'syntax error')}"
            ))
    except ImportError:
        pass  # syntax_validator not available — skip silently
    except Exception:
        pass

    return issues


def _check_props_mismatch(code: str, filename: str) -> List[Dict]:
    """
    Detect when a component is defined with one props interface but called
    with completely different props.

    Classic bug: MobileComposeSheet defined as ({value, onChange}) but called
    as <MobileComposeSheet open={...} input={...} setInput={...} onAttach={...}/>
    """
    issues = []

    # Find component definitions with destructured props
    # Pattern: function X({ a, b, c }) or const X = ({ a, b, c }) =>
    comp_defs = re.findall(
        r"(?:function|const)\s+(\w+)\s*(?::\s*\w+(?:\.\w+)*(?:<[^>]*>)?\s*=\s*)?\(\s*\{([^}]+)\}",
        code
    )

    for comp_name, props_str in comp_defs:
        # Extract prop names from definition
        def_props = set(re.findall(r"\b(\w+)\b", props_str))
        # Remove common noise
        def_props -= {"children", "className", "style", "key", "ref"}

        if len(def_props) < 2:
            continue

        # Find JSX usages of this component
        jsx_usages = re.findall(
            rf"<{re.escape(comp_name)}\s+([^/>]+?)(?:/>|>)",
            code
        )

        for usage_str in jsx_usages:
            # Extract prop names from usage
            usage_props = set(re.findall(r"\b(\w+)\s*=", usage_str))
            usage_props -= {"className", "style", "key", "ref"}

            if len(usage_props) < 2:
                continue

            # Check overlap
            overlap = def_props & usage_props
            total = def_props | usage_props
            if total and len(overlap) / len(total) < 0.3:
                issues.append(_error(
                    "props_mismatch",
                    f"Component '{comp_name}' is defined with props "
                    f"{{{', '.join(sorted(def_props))}}} but called with "
                    f"{{{', '.join(sorted(usage_props))}}}. "
                    f"Only {len(overlap)}/{len(total)} props overlap — this will crash at runtime."
                ))
                break  # One report per component

    return issues


def _check_dead_destructured_bindings(code: str, filename: str) -> List[Dict]:
    """
    Detect variables destructured from a store/hook but never used in the
    rest of the code. These cause TypeScript warnings and indicate the
    refactor didn't fully clean up.
    """
    issues = []

    # Find destructured bindings from hook/store calls
    # Pattern: const { a, b, c } = useXxx(...) or = useSomeStore(...)
    destructs = re.findall(
        r"const\s*\{\s*([^}]+)\}\s*=\s*(use\w+)\s*\(",
        code
    )

    for bindings_str, hook_name in destructs:
        bindings = [b.strip().split(":")[0].strip() for b in bindings_str.split(",")]
        bindings = [b for b in bindings if b and not b.startswith("//")]

        # For each binding, check if it's used anywhere else in the code
        # (beyond the destructuring line itself)
        for binding in bindings:
            if not binding or len(binding) < 2:
                continue

            # Count occurrences in code (excluding the destructure line)
            pattern = rf"\b{re.escape(binding)}\b"
            all_matches = list(re.finditer(pattern, code))

            # If only 1 match (the destructure itself), it's dead
            if len(all_matches) <= 1:
                issues.append(_warning(
                    "dead_binding",
                    f"'{binding}' is destructured from {hook_name}() but never used. "
                    f"Remove it to avoid TypeScript 'declared but never read' warnings."
                ))

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _error(check: str, message: str) -> Dict:
    return {"severity": "error", "check": check, "message": message}


def _warning(check: str, message: str) -> Dict:
    return {"severity": "warning", "check": check, "message": message}


def _ext(filename: str) -> str:
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
