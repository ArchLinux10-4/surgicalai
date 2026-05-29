"""
Compile-time syntax validation for JSX/TSX/TS/JS files.
Uses tree-sitter for fast, accurate parsing without Node.js dependency.
Falls back gracefully if tree-sitter is not installed.
"""
from typing import List, Dict


def validate_syntax(code: str, filename: str) -> List[Dict]:
    """
    Validate code syntax using tree-sitter.
    Returns empty list if valid, or list of error dicts.
    Each error: {"line": int, "column": int, "message": str, "detail": str}
    Only validates TSX/JSX/TS/JS files. Returns [] for other file types.
    """
    ext = _get_extension(filename)
    if ext not in ('.tsx', '.jsx', '.ts', '.js'):
        return []

    try:
        from tree_sitter import Language, Parser
        import tree_sitter_typescript as ts_typescript

        if ext in ('.tsx', '.jsx'):
            lang = Language(ts_typescript.language_tsx())
        else:
            lang = Language(ts_typescript.language_typescript())

        parser = Parser(lang)
        tree = parser.parse(code.encode('utf-8'))

        if not tree.root_node.has_error:
            return []

        raw_errors = []
        _find_error_nodes(tree.root_node, raw_errors, code, max_errors=8)

        if not raw_errors:
            return []

        # Deduplicate cascading errors — keep meaningful ones
        return _deduplicate_errors(raw_errors)

    except ImportError:
        print("[SYNTAX_VALIDATOR] tree-sitter not installed, skipping validation")
        return []
    except Exception as e:
        print(f"[SYNTAX_VALIDATOR] Unexpected error: {e}")
        return []


def count_errors(code: str, filename: str) -> int:
    """Quick error count — used to compare before/after surgeon changes."""
    return len(validate_syntax(code, filename))


def detect_redeclarations(code: str, filename: str) -> List[Dict]:
    """Detect program-level (module scope) redeclarations that are hard runtime
    SyntaxErrors ("Identifier 'x' has already been declared").

    validate_syntax() above only checks GRAMMAR — a duplicate top-level
    const/let/class/function is grammatically valid, so tree-sitter reports zero
    ERROR nodes and the gate waves it through. But `node --check` rejects a
    const/let/class collision. Node/tsc are not reliably present on the backend,
    so this catches that class deterministically using the tree-sitter parser
    already in use.

    Only flags TOP-LEVEL collisions where >=1 binding is const/let/class —
    matching JS runtime semantics. Legal redeclarations (function+function,
    var+var, var+function) are NOT flagged. Simple identifier bindings only;
    destructuring patterns are intentionally skipped to guarantee zero false
    positives. Returns [] for non-JS/TS files (Python module-scope rebinding is
    legal, never a SyntaxError).

    Each error dict: {"line", "column", "message", "detail"} — same shape as
    validate_syntax(), so it slots into the pipeline's existing block path.
    """
    ext = _get_extension(filename)
    if ext not in ('.tsx', '.jsx', '.ts', '.js'):
        return []
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_typescript as ts_typescript

        if ext in ('.tsx', '.jsx'):
            lang = Language(ts_typescript.language_tsx())
        else:
            lang = Language(ts_typescript.language_typescript())

        parser = Parser(lang)
        # tree-sitter node offsets are BYTE offsets — slice the encoded bytes,
        # not the str, or multi-byte UTF-8 chars desync names on large files.
        code_bytes = code.encode('utf-8')
        tree = parser.parse(code_bytes)
        root = tree.root_node

        def _txt(node):
            return code_bytes[node.start_byte:node.end_byte].decode('utf-8', 'replace')

        decls: dict = {}  # name -> list[(kind, line)]

        def _collect_lexical(node, kind):
            for child in node.named_children:
                if child.type == 'variable_declarator':
                    name_node = child.child_by_field_name('name')
                    if name_node is not None and name_node.type == 'identifier':
                        decls.setdefault(_txt(name_node), []).append(
                            (kind, name_node.start_point[0] + 1))

        for node in root.children:
            n = node
            if n.type == 'export_statement':
                inner = n.child_by_field_name('declaration')
                if inner is None:
                    continue
                n = inner
            t = n.type
            if t == 'lexical_declaration':
                kind = n.children[0].type if n.children else 'const'  # 'const' | 'let'
                _collect_lexical(n, kind)
            elif t == 'variable_declaration':
                _collect_lexical(n, 'var')
            elif t == 'function_declaration':
                name_node = n.child_by_field_name('name')
                if name_node is not None:
                    decls.setdefault(_txt(name_node), []).append(
                        ('function', name_node.start_point[0] + 1))
            elif t == 'class_declaration':
                name_node = n.child_by_field_name('name')
                if name_node is not None:
                    decls.setdefault(_txt(name_node), []).append(
                        ('class', name_node.start_point[0] + 1))

        errors: List[Dict] = []
        _HARD = {'const', 'let', 'class'}
        for name, occ in decls.items():
            if len(occ) < 2:
                continue
            if any(k in _HARD for k, _ln in occ):
                lines = sorted(ln for _k, ln in occ)
                errors.append({
                    "line": lines[1],          # the duplicate (second) declaration
                    "column": 0,
                    "message": f"Identifier '{name}' has already been declared",
                    "detail": (f"'{name}' declared {len(occ)}x at top level "
                               f"(lines {', '.join(str(l) for l in lines)}); "
                               f">=1 is const/let/class — runtime SyntaxError"),
                })
        return errors
    except ImportError:
        print("[SYNTAX_VALIDATOR] tree-sitter not installed, skipping redeclaration check")
        return []
    except Exception as e:
        print(f"[SYNTAX_VALIDATOR] redeclaration check error: {e}")
        return []


def _find_error_nodes(node, errors: list, code: str, max_errors: int = 8):
    """Walk tree-sitter parse tree and collect ERROR nodes."""
    if len(errors) >= max_errors:
        return

    if node.type == 'ERROR':
        line = node.start_point[0] + 1
        col = node.start_point[1]
        text = code[node.start_byte:node.end_byte][:120]

        errors.append({
            "line": line,
            "column": col,
            "text": text,
            "message": _classify_error(node, code),
            "detail": f"Line {line}, col {col}: {text[:60]}"
        })
    elif node.has_error:
        for child in node.children:
            _find_error_nodes(child, errors, code, max_errors)


def _classify_error(error_node, code: str) -> str:
    """Classify the error type for user-friendly messaging."""
    text = code[error_node.start_byte:error_node.end_byte][:200]
    line_start = code.rfind('\n', 0, error_node.start_byte) + 1
    line_end = code.find('\n', error_node.end_byte)
    if line_end == -1:
        line_end = len(code)
    context = code[line_start:line_end].strip()

    # Adjacent JSX elements pattern
    if '<>' in text or '</>' in text or (text.strip().startswith('<') and error_node.parent):
        parent = error_node.parent
        if parent:
            jsx_types = ('jsx_element', 'jsx_fragment', 'jsx_self_closing_element')
            jsx_siblings = sum(1 for c in parent.children if c.type in jsx_types)
            if jsx_siblings >= 1:
                return "Adjacent JSX elements must be wrapped in a single parent (<>...</>)"

    if text.strip().startswith('<'):
        return "Invalid JSX structure — possible unclosed or mismatched tag"

    if text.strip().startswith('{'):
        return "Invalid expression block in JSX"

    if 'return' in context.lower():
        return "Invalid return statement structure"

    clean = text.strip()[:40]
    return f"Syntax error near: {clean}"


def _deduplicate_errors(errors: List[Dict]) -> List[Dict]:
    """Remove cascading errors — keep first occurrence per region."""
    if not errors:
        return []

    # Sort by line number
    errors.sort(key=lambda e: e["line"])

    # Keep errors that are at least 10 lines apart (cascading errors cluster together)
    result = [errors[0]]
    for err in errors[1:]:
        if err["line"] - result[-1]["line"] >= 10:
            result.append(err)

    # Cap at 3 errors max for user-friendly output
    return result[:3]


def _get_extension(filename: str) -> str:
    """Get lowercased file extension."""
    if '.' not in filename:
        return ''
    return '.' + filename.rsplit('.', 1)[-1].lower()
