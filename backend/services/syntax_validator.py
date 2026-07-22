"""
Compile-time syntax validation for JSX/TSX/TS/JS files.
Uses tree-sitter for fast, accurate parsing without Node.js dependency.
Falls back gracefully if tree-sitter is not installed.
"""
import datetime as _dt
import json
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

_DLOG_PATH = "/tmp/syntax_validator_dlog.jsonl"


def _dlog(event: str, **kwargs):
    """Same pattern as element_picker.py's _dlog: logger + flat-file,
    never raises. Added so any future false-positive/false-negative in
    this module (session ff34af1d class of bug) leaves a trace instead
    of silently altering QA scores with no evidence."""
    try:
        ts = _dt.datetime.utcnow().isoformat() + "Z"
        record = {"ts": ts, "event": event, **kwargs}
        logger.info("[syntax_validator] %s", json.dumps(record, default=str))
        try:
            with open(_DLOG_PATH, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass
    except Exception:
        pass


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
        code_bytes = code.encode('utf-8')
        tree = parser.parse(code_bytes)

        if not tree.root_node.has_error:
            return []

        raw_errors = []
        _dlog("syntax_validator_parse_has_error", filename=filename, ext=ext, code_len=len(code))
        # Pass the BYTE-encoded buffer, not the str — node.start_byte/end_byte
        # are byte offsets from tree-sitter. Slicing a Python str with byte
        # offsets desyncs as soon as any multi-byte UTF-8 character (curly
        # quotes, em dashes, emoji, etc.) appears earlier in the file,
        # producing garbled error text (session ff34af1d: "Spacing: '-"
        # instead of the real offending text).
        _find_error_nodes(tree.root_node, raw_errors, code_bytes, max_errors=8)

        if not raw_errors:
            _dlog("syntax_validator_all_errors_suppressed_as_benign", filename=filename)
            return []

        # Deduplicate cascading errors — keep meaningful ones
        final_errors = _deduplicate_errors(raw_errors)
        _dlog(
            "syntax_validator_reporting_errors",
            filename=filename,
            raw_count=len(raw_errors),
            final_count=len(final_errors),
            messages=[e.get("message") for e in final_errors],
        )
        return final_errors

    except ImportError:
        _dlog("syntax_validator_tree_sitter_not_installed", filename=filename)
        print("[SYNTAX_VALIDATOR] tree-sitter not installed, skipping validation")
        return []
    except Exception as e:
        _dlog("syntax_validator_unexpected_exception", filename=filename, error=str(e))
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


def _find_error_nodes(node, errors: list, code_bytes: bytes, max_errors: int = 8):
    """Walk tree-sitter parse tree and collect ERROR nodes.

    `code_bytes` MUST be the UTF-8 encoded buffer that was actually parsed —
    node.start_byte/end_byte are byte offsets, not character offsets.
    """
    if len(errors) >= max_errors:
        return

    if node.type == 'ERROR':
        error_text_preview = (
            code_bytes[node.start_byte:node.end_byte][:80].decode('utf-8', 'replace')
        )
        if _is_benign_jsx_text_punctuation(node):
            _dlog(
                "syntax_validator_benign_jsx_text_suppressed",
                line=node.start_point[0] + 1,
                col=node.start_point[1],
                text_preview=error_text_preview,
            )
            # Session ff34af1d proved (byte-for-byte, reproduced from
            # production log) that tree-sitter-typescript's TSX grammar
            # raises a false ERROR node for perfectly valid, unescaped
            # JSX text content containing '&', '&&', '<' or '>' used as
            # plain prose (e.g. "& many more", "5 > 3 items", "Terms &
            # Conditions"). Real browsers/Babel accept this without any
            # escaping. Confirmed via minimal repro: adjacent-JSX-element,
            # unclosed-tag, and bad-expression-block bugs are NOT affected
            # by this filter — they still get flagged (see
            # test_syntax_validator_jsx_text_false_positive.py).
            return

        line = node.start_point[0] + 1
        col = node.start_point[1]
        text = code_bytes[node.start_byte:node.end_byte][:120].decode('utf-8', 'replace')
        message = _classify_error(node, code_bytes)

        # Any ERROR node whose text begins with JSX-text-like punctuation
        # ('&', '<', '>') but that the guard did NOT suppress is exactly
        # the boundary case worth watching for regressions or missed
        # false positives — log it so any recurrence has a trace instead
        # of silently reappearing as an unexplained QA block.
        if text.lstrip()[:1] in ('&', '<', '>'):
            _dlog(
                "syntax_validator_punctuation_error_not_suppressed",
                line=line,
                col=col,
                text_preview=text[:80],
                message=message,
            )

        errors.append({
            "line": line,
            "column": col,
            "text": text,
            "message": message,
            "detail": f"Line {line}, col {col}: {text[:60]}"
        })
    elif node.has_error:
        for child in node.children:
            _find_error_nodes(child, errors, code_bytes, max_errors)


def _is_benign_jsx_text_punctuation(error_node) -> bool:
    """True if an ERROR node is just plain JSX text content containing raw
    '&', '&&', '<', or '>' punctuation — a known tree-sitter-typescript TSX
    grammar false positive, not an actual code defect.

    Only fires when:
      1. The ERROR node sits directly inside a jsx_element/jsx_fragment's
         children (i.e. it IS the text between open/close tags), and
      2. None of its descendants are actual JSX/expression nodes — real
         bugs (adjacent elements, unclosed tags, broken expression blocks)
         always have jsx_element/jsx_fragment/jsx_expression descendants
         under the ERROR node, so this never masks them.
    """
    parent = error_node.parent
    if parent is None or parent.type not in ('jsx_element', 'jsx_fragment'):
        return False
    if _has_jsx_descendant(error_node):
        return False

    # CONTENT check, not just structure. Verified against real Babel
    # (@babel/core + @babel/preset-react — ground truth for what actually
    # ships/compiles, not a guess):
    #   - bare '&' / '&&' followed by prose in JSX text -> Babel ACCEPTS
    #     (confirmed: `<div>salt & pepper && more</div>` compiles clean)
    #   - bare '>' in JSX text -> Babel REJECTS: "Unexpected token `>`.
    #     Did you mean `&gt;` or `{'>'}`?" — a REAL compile error
    #   - bare '<' in JSX text -> Babel REJECTS: "Unexpected token"
    # An earlier draft of this guard suppressed all three based on tree
    # shape alone (no jsx descendant under the ERROR node), which silently
    # hid a real `>` bug (over-suppression) — caught here by re-testing
    # against actual Babel before shipping. Only text that STARTS with
    # '&' is proven safe to suppress; '<' / '>' must stay flagged.
    error_text = error_node.text.decode('utf-8', 'replace') if error_node.text else ''
    return error_text.lstrip().startswith('&')


def _has_jsx_descendant(node) -> bool:
    for child in node.children:
        if child.type in (
            'jsx_element', 'jsx_fragment',
            'jsx_self_closing_element', 'jsx_expression',
        ):
            return True
        if _has_jsx_descendant(child):
            return True
    return False


def _classify_error(error_node, code_bytes: bytes) -> str:
    """Classify the error type for user-friendly messaging.

    `code_bytes` MUST be the UTF-8 encoded buffer that was parsed — all
    offsets here are byte offsets, not character offsets.
    """
    text = code_bytes[error_node.start_byte:error_node.end_byte][:200].decode('utf-8', 'replace')
    line_start = code_bytes.rfind(b'\n', 0, error_node.start_byte) + 1
    line_end = code_bytes.find(b'\n', error_node.end_byte)
    if line_end == -1:
        line_end = len(code_bytes)
    context = code_bytes[line_start:line_end].decode('utf-8', 'replace').strip()

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
