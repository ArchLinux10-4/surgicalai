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
