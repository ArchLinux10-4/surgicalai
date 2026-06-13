"""
Multi-language AST parser.
Best Practice #1: Read the map before you touch the territory.
We build a complete symbol map FIRST — the AI sees structure, not raw 20k lines.
"""
import ast
import re
from pathlib import Path
from typing import List, Optional, Tuple
from models.schemas import SymbolInfo, SymbolMap, SymbolType


LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".sh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".sql": "sql",
    ".md": "markdown",
}


def detect_language(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    return LANGUAGE_MAP.get(ext, "plaintext")


class ASTParser:
    """
    Builds a complete symbol map from source code.
    Python uses the built-in AST (perfect accuracy).
    Other languages use battle-tested regex patterns.
    """

    def parse(self, source: str, file_path: str) -> SymbolMap:
        language = detect_language(file_path)
        lines = source.splitlines()

        if language == "python":
            symbols = self._parse_python(source, lines)
        elif language in ("javascript", "typescript"):
            symbols = self._parse_js_ts(source, lines)
        elif language == "go":
            symbols = self._parse_go(source, lines)
        elif language == "rust":
            symbols = self._parse_rust(source, lines)
        elif language == "java":
            symbols = self._parse_java(source, lines)
        elif language == "html":
            symbols = self._parse_html(source, lines)
        elif language == "css":
            symbols = self._parse_css(source, lines)
        elif language == "markdown":
            symbols = self._parse_markdown(source, lines)
        else:
            symbols = self._parse_generic(source, lines)

        imports = self._extract_imports(source, language)

        return SymbolMap(
            file_path=file_path,
            language=language,
            symbols=symbols,
            imports=imports,
            total_lines=len(lines),
        )

    def get_symbol(self, symbol_map: SymbolMap, symbol_path: str) -> Optional[SymbolInfo]:
        """Find a symbol by path like 'ClassName.method_name' or 'function_name'."""
        parts = symbol_path.split(".")
        if len(parts) == 1:
            for s in symbol_map.symbols:
                if s.name == parts[0] and s.parent is None:
                    return s
        elif len(parts) == 2:
            parent, name = parts
            for s in symbol_map.symbols:
                if s.name == name and s.parent == parent:
                    return s
        return None

    def get_symbol_with_context(
        self, source: str, symbol: SymbolInfo, context_lines: int = 5
    ) -> Tuple[str, int, int]:
        """
        Return the symbol code with N lines of surrounding context.
        Context helps the surgeon understand imports/variables above the function.
        Returns (code_with_context, actual_start, actual_end)
        """
        lines = source.splitlines()
        ctx_start = max(0, symbol.start_line - 1 - context_lines)
        ctx_end = min(len(lines), symbol.end_line + context_lines)
        return (
            "\n".join(lines[ctx_start:ctx_end]),
            ctx_start + 1,
            ctx_end,
        )

    # ─── Python Parser (Gold Standard) ───────────────────────────────────────

    def _parse_python(self, source: str, lines: List[str]) -> List[SymbolInfo]:
        symbols: List[SymbolInfo] = []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return self._parse_generic(source, lines)

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.class_stack: List[str] = []

            def visit_ClassDef(self, node: ast.ClassDef):
                start = node.lineno
                # Include decorators
                if node.decorator_list:
                    start = node.decorator_list[0].lineno
                end = node.end_lineno
                indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
                code = "\n".join(lines[start - 1 : end])
                sig = f"class {node.name}"
                if node.bases:
                    base_names = [
                        ast.unparse(b) if hasattr(ast, "unparse") else "..."
                        for b in node.bases
                    ]
                    sig += f"({', '.join(base_names)})"

                symbols.append(
                    SymbolInfo(
                        name=node.name,
                        symbol_type=SymbolType.CLASS,
                        start_line=start,
                        end_line=end,
                        parent=self.class_stack[-1] if self.class_stack else None,
                        indentation=indent,
                        code=code,
                        signature=sig,
                    )
                )
                self.class_stack.append(node.name)
                self.generic_visit(node)
                self.class_stack.pop()

            def _visit_func(self, node):
                start = node.lineno
                if node.decorator_list:
                    start = node.decorator_list[0].lineno
                end = node.end_lineno
                indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
                code = "\n".join(lines[start - 1 : end])
                stype = SymbolType.METHOD if self.class_stack else SymbolType.FUNCTION
                is_async = isinstance(node, ast.AsyncFunctionDef)
                prefix = "async def " if is_async else "def "
                try:
                    args_str = ast.unparse(node.args) if hasattr(ast, "unparse") else "..."
                except Exception:
                    args_str = "..."
                sig = f"{prefix}{node.name}({args_str})"

                symbols.append(
                    SymbolInfo(
                        name=node.name,
                        symbol_type=stype,
                        start_line=start,
                        end_line=end,
                        parent=self.class_stack[-1] if self.class_stack else None,
                        indentation=indent,
                        code=code,
                        signature=sig,
                    )
                )
                self.generic_visit(node)

            visit_FunctionDef = _visit_func
            visit_AsyncFunctionDef = _visit_func

        Visitor().visit(tree)
        return symbols

    # ─── JavaScript / TypeScript Parser ──────────────────────────────────────

    def _parse_js_ts(self, source: str, lines: List[str]) -> List[SymbolInfo]:
        symbols: List[SymbolInfo] = []

        # Class declarations
        class_re = re.compile(
            r"^(export\s+)?(abstract\s+)?class\s+(\w+)", re.MULTILINE
        )
        # Function declarations
        func_re = re.compile(
            r"^(export\s+)?(export\s+default\s+)?(async\s+)?function\s*\*?\s*(\w+)\s*\(",
            re.MULTILINE,
        )
        # Arrow functions assigned to const/let/var
        # FIX v3.11.3: Added (?:\s*:\s*[^=\n]+?)? to handle TypeScript type annotations
        # e.g. `export const LoginPage: React.FC = () => {` was previously invisible
        arrow_re = re.compile(
            r"^(export\s+)?(const|let|var)\s+(\w+)(?:\s*:\s*[^=\n]+?)?\s*=\s*(async\s+)?\(.*?\)\s*=>",
            re.MULTILINE,
        )
        # Class methods (indented) — handles TS modifiers + return type annotations
        method_re = re.compile(
            r"^(\s+)(?:private\s+|protected\s+|public\s+|static\s+|abstract\s+|override\s+|readonly\s+)*(async\s+)?(\w+)\s*\([^)]*\)(?:\s*:\s*[^{\n]+)?\s*\{",
            re.MULTILINE,
        )

        # Build class spans list: (class_name, start_line, end_line)
        class_spans = []
        for m in class_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            name = m.group(3)
            class_spans.append((name, line_no, end_line))
            symbols.append(
                SymbolInfo(
                    name=name,
                    symbol_type=SymbolType.CLASS,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        def _class_for_line(lnum: int) -> Optional[str]:
            for cname, cstart, cend in class_spans:
                if cstart < lnum <= cend:
                    return cname
            return None

        # Extract class methods (indented) — iterate method_re
        _SKIP = {"if", "for", "while", "switch", "catch", "else", "return", "function", "class"}
        for m in method_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            parent = _class_for_line(line_no)
            if parent is None:
                continue
            method_name = m.group(3)
            if method_name in _SKIP:
                continue
            end_line = self._find_block_end(lines, line_no - 1)
            indent = len(m.group(1))
            symbols.append(
                SymbolInfo(
                    name=method_name,
                    symbol_type=SymbolType.METHOD,
                    start_line=line_no,
                    end_line=end_line,
                    parent=parent,
                    indentation=indent,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        for m in func_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            name = m.group(4)
            symbols.append(
                SymbolInfo(
                    name=name,
                    symbol_type=SymbolType.FUNCTION,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        for m in arrow_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            name = m.group(3)
            symbols.append(
                SymbolInfo(
                    name=name,
                    symbol_type=SymbolType.ARROW_FUNCTION,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        # ── React HOC wrappers: React.memo(), forwardRef(), lazy() ──
        # These wrap a function/component in a higher-order call.  The inner
        # braces live inside the memo() paren, so _find_block_end's brace
        # tracker misses them.  Instead we scan forward for the closing ");".
        hoc_re = re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*"
            r"(?:React\.)?(?:memo|forwardRef|lazy)\s*\(",
            re.MULTILINE,
        )
        _hoc_existing = {s.start_line for s in symbols}
        closing_re = re.compile(r"^\}?\s*\)\s*;?\s*$")
        for m in hoc_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            if line_no in _hoc_existing:
                continue
            name = m.group(1)
            decl_line = lines[line_no - 1]
            start_indent = len(decl_line) - len(decl_line.lstrip())
            # Skip one-liners where parens balance on the same line
            paren_depth = 0
            for ch in decl_line:
                if ch == "(":
                    paren_depth += 1
                elif ch == ")":
                    paren_depth -= 1
            if paren_depth == 0:
                continue
            # Scan forward for closing "})" or ")" at start indent
            end_line = len(lines)
            for scan_i in range(line_no, len(lines)):
                ln = lines[scan_i]
                stripped = ln.strip()
                if not stripped:
                    continue
                cur_indent = len(ln) - len(ln.lstrip())
                if cur_indent <= start_indent and closing_re.match(stripped):
                    end_line = scan_i + 1
                    break
            symbols.append(
                SymbolInfo(
                    name=name,
                    symbol_type=SymbolType.FUNCTION,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        # ── Multi-line destructured arrow functions ──
        # Catches `const X = (\n  prop1,\n) => {` where the opening paren
        # is on the declaration line but `=>` is on a subsequent line.
        # _find_block_end already handles these correctly (brace at paren
        # depth 0 on the `=> {` line).
        ml_arrow_re = re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*\(",
            re.MULTILINE,
        )
        _ml_existing = {s.start_line for s in symbols}
        for m in ml_arrow_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            if line_no in _ml_existing:
                continue
            name = m.group(1)
            # Confirm `=>` exists within 20 lines
            is_arrow = False
            for scan_i in range(line_no - 1, min(line_no + 19, len(lines))):
                if re.search(r"\)\s*=>", lines[scan_i]):
                    is_arrow = True
                    break
            if not is_arrow:
                continue
            end_line = self._find_block_end(lines, line_no - 1)
            symbols.append(
                SymbolInfo(
                    name=name,
                    symbol_type=SymbolType.ARROW_FUNCTION,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        # Module-level VALUE constants — template literals and large object/array
        # literals that are NOT arrow functions. e.g. `const CSS = \`...500 lines...\``
        # or a big config object. Without this pass such a constant is invisible to
        # the editor, so any edit that targets it (a CSS block, a config map) can
        # never resolve and is silently dropped. Only sizeable multi-line values
        # are indexed so the symbol map is not flooded with trivial scalars.
        value_const_re = re.compile(
            r"^(?:export\s+)?(?:export\s+default\s+)?(const|let|var)\s+(\w+)"
            r"(?:\s*:\s*[^=\n]+?)?\s*=\s*([`{\[])",
            re.MULTILINE,
        )
        _value_existing = {s.start_line for s in symbols}
        for m in value_const_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            if line_no in _value_existing:
                continue  # already captured (e.g. arrow-function const)
            opener_off = m.end() - 1  # offset of the ` { or [ opener
            end_line = self._find_value_end(source, opener_off)
            if end_line - line_no + 1 < 3:
                continue  # too small to be worth indexing — avoid map bloat
            name = m.group(2)
            symbols.append(
                SymbolInfo(
                    name=name,
                    symbol_type=SymbolType.VARIABLE,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=lines[line_no - 1].strip()[:120],
                )
            )

        # Express / Fastify / Koa-style route handlers passed as anonymous callbacks.
        # e.g. router.post('/api/batch-search', async (req, res) => { ... })
        # These hold the bulk of the logic in a routes file, yet none of the patterns
        # above match them (there is no name binding). We synthesize a stable,
        # targetable name from the HTTP verb + route path so the surgeon can edit
        # them with full QA instead of degrading to manual instructions.
        route_re = re.compile(
            r"^\s*(?:export\s+(?:default\s+)?)?(?:const\s+\w+\s*=\s*)?"
            r"\w+\s*\.\s*(get|post|put|patch|delete|options|head|all)\s*\(\s*"
            r"[`'\"]([^`'\"]+)[`'\"]\s*,",
            re.MULTILINE,
        )
        _existing_starts = {s.start_line for s in symbols}
        _route_seen: dict = {}
        for m in route_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            if line_no in _existing_starts:
                continue  # already captured by another pattern
            end_line = self._find_block_end(lines, line_no - 1)
            code = "\n".join(lines[line_no - 1 : end_line])
            if "{" not in code:
                continue  # not a real handler body (e.g. a router mount)
            method = m.group(1).lower()
            path = m.group(2)
            slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_").lower()
            base = f"route_{method}_{slug}" if slug else f"route_{method}"
            if base in _route_seen:
                _route_seen[base] += 1
                name = f"{base}_{_route_seen[base]}"
            else:
                _route_seen[base] = 0
                name = base
            symbols.append(
                SymbolInfo(
                    name=name,
                    symbol_type=SymbolType.FUNCTION,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code=code,
                    signature=f"{method.upper()} {path}  —  {lines[line_no - 1].strip()}",
                )
            )

        return symbols

    def _skip_template(self, source: str, i: int) -> int:
        """Given i at an opening backtick, return the index of the matching
        closing backtick (handles \\ escapes and nested ${ } interpolations,
        which may themselves contain template literals). If unterminated,
        returns the last index."""
        n = len(source)
        i += 1  # past opening backtick
        while i < n:
            c = source[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                return i
            if c == "$" and i + 1 < n and source[i + 1] == "{":
                depth = 1
                i += 2
                while i < n and depth > 0:
                    cc = source[i]
                    if cc == "\\":
                        i += 2
                        continue
                    if cc == "`":
                        i = self._skip_template(source, i)
                    elif cc == "{":
                        depth += 1
                    elif cc == "}":
                        depth -= 1
                    i += 1
                continue
            i += 1
        return n - 1

    def _find_value_end(self, source: str, start_offset: int) -> int:
        """Given start_offset at an opening delimiter (` { or [) of a value
        expression, return the 1-based line number of the matching close.
        String- and template-literal-aware so delimiters inside strings are
        ignored. Falls back to end-of-file when unbalanced."""
        n = len(source)
        opener = source[start_offset]
        if opener == "`":
            end = self._skip_template(source, start_offset)
            return source[:end].count("\n") + 1
        # object / array literal — balance braces/brackets, skipping string
        # and template-literal contents.
        i = start_offset
        depth = 0
        while i < n:
            c = source[i]
            if c == "\\":
                i += 2
                continue
            if c in ('"', "'"):
                q = c
                i += 1
                while i < n:
                    if source[i] == "\\":
                        i += 2
                        continue
                    if source[i] == q:
                        break
                    i += 1
            elif c == "`":
                i = self._skip_template(source, i)
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return source[:i].count("\n") + 1
            i += 1
        return source.count("\n") + 1

    # ─── Go Parser ───────────────────────────────────────────────────────────

    def _parse_go(self, source: str, lines: List[str]) -> List[SymbolInfo]:
        symbols: List[SymbolInfo] = []
        func_re = re.compile(
            r"^func\s+(?:\((\w+)\s+\*?(\w+)\)\s+)?(\w+)\s*\(", re.MULTILINE
        )
        type_re = re.compile(r"^type\s+(\w+)\s+struct\s*\{", re.MULTILINE)

        for m in type_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            symbols.append(
                SymbolInfo(
                    name=m.group(1),
                    symbol_type=SymbolType.CLASS,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        for m in func_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            receiver_type = m.group(2)
            func_name = m.group(3)
            stype = SymbolType.METHOD if receiver_type else SymbolType.FUNCTION
            symbols.append(
                SymbolInfo(
                    name=func_name,
                    symbol_type=stype,
                    start_line=line_no,
                    end_line=end_line,
                    parent=receiver_type,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        return symbols

    # ─── Rust Parser ─────────────────────────────────────────────────────────

    def _parse_rust(self, source: str, lines: List[str]) -> List[SymbolInfo]:
        symbols: List[SymbolInfo] = []
        fn_re = re.compile(
            r"^(\s*)(pub\s+)?(async\s+)?fn\s+(\w+)\s*[(<]", re.MULTILINE
        )
        struct_re = re.compile(r"^(pub\s+)?struct\s+(\w+)", re.MULTILINE)
        impl_re = re.compile(r"^impl\s+(?:\w+\s+for\s+)?(\w+)", re.MULTILINE)

        current_impl: Optional[str] = None

        for m in struct_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            symbols.append(
                SymbolInfo(
                    name=m.group(2),
                    symbol_type=SymbolType.CLASS,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        for m in fn_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            indent = len(m.group(1))
            symbols.append(
                SymbolInfo(
                    name=m.group(4),
                    symbol_type=SymbolType.METHOD if indent > 0 else SymbolType.FUNCTION,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=indent,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        return symbols

    # ─── Java Parser ─────────────────────────────────────────────────────────

    def _parse_java(self, source: str, lines: List[str]) -> List[SymbolInfo]:
        symbols: List[SymbolInfo] = []
        class_re = re.compile(
            r"^(public\s+|private\s+|protected\s+)?(abstract\s+|final\s+)?class\s+(\w+)",
            re.MULTILINE,
        )
        method_re = re.compile(
            r"^\s+(public|private|protected|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+\w+\s*)?\{",
            re.MULTILINE,
        )

        for m in class_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            symbols.append(
                SymbolInfo(
                    name=m.group(3),
                    symbol_type=SymbolType.CLASS,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=0,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        for m in method_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            indent = len(lines[line_no - 1]) - len(lines[line_no - 1].lstrip())
            symbols.append(
                SymbolInfo(
                    name=m.group(2),
                    symbol_type=SymbolType.METHOD,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=indent,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        return symbols

    # ─── HTML Parser ─────────────────────────────────────────────────────────

    def _find_html_close_tag(self, lines: List[str], start_idx: int, tag_name: str) -> int:
        """
        Find the closing </tag> for a given opening tag starting at start_idx (0-based).
        Tracks nesting depth. Ignores self-closing tags.
        Returns 1-based line number of the closing tag line.
        """
        depth = 0
        open_re = re.compile(r"<" + re.escape(tag_name) + r"(?:\s|>)", re.IGNORECASE)
        close_re = re.compile(r"</" + re.escape(tag_name) + r"\s*>", re.IGNORECASE)
        self_close_re = re.compile(r"<" + re.escape(tag_name) + r"\b[^>]*/\s*>", re.IGNORECASE)

        limit = min(start_idx + 5000, len(lines))
        for i in range(start_idx, limit):
            line = lines[i]
            # Count self-closing occurrences to exclude them
            sc = len(self_close_re.findall(line))
            opens = len(open_re.findall(line)) - sc
            closes = len(close_re.findall(line))
            depth += opens - closes
            if depth <= 0 and closes > 0:
                return i + 1  # 1-based
        # Fallback
        return start_idx + 100

    def _parse_html(self, source: str, lines: List[str]) -> List[SymbolInfo]:
        symbols: List[SymbolInfo] = []

        # Major structural tags to look for
        major_tags = {"head", "body", "header", "nav", "main", "footer",
                      "section", "article", "aside", "form"}

        # Track parent sections for nesting — list of (tag_name, symbol_name, start_line_1based, end_line_1based)
        section_spans: List[Tuple[str, str, int, int]] = []

        # Counters for unnamed elements
        table_counter = 0
        style_counter = 0
        script_counter = 0
        heading_counter = {f"h{i}": 0 for i in range(1, 7)}

        # Regex patterns
        # Match opening tags: <tag ...> or <tag>
        tag_re = re.compile(
            r"<(head|body|header|nav|main|footer|section|article|aside|form|style|script|table|h[1-6])\b([^>]*)>",
            re.IGNORECASE,
        )
        id_re = re.compile(r'id\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        # For elements with id attribute on any tag
        any_id_re = re.compile(r"<(\w+)\b([^>]*)\bid\s*=\s*[\"']([^\"']+)[\"']([^>]*)>", re.IGNORECASE)

        def _find_parent(line_1based: int) -> Optional[str]:
            """Find the innermost enclosing section for a given line."""
            best = None
            best_start = -1
            for _tag, sname, sstart, send in section_spans:
                if sstart < line_1based <= send and sstart > best_start:
                    best = sname
                    best_start = sstart
            return best

        # First pass: collect major sections to build parent mapping
        for i, line in enumerate(lines):
            for m in tag_re.finditer(line):
                tag = m.group(1).lower()
                attrs = m.group(2)
                id_match = id_re.search(attrs)
                line_no = i + 1  # 1-based

                if tag in major_tags:
                    end_line = self._find_html_close_tag(lines, i, tag)
                    sym_name = tag
                    if id_match:
                        sym_name = f"{tag}#{id_match.group(1)}"
                    section_spans.append((tag, sym_name, line_no, end_line))

        # Second pass: build all symbols
        # Track seen (line_no, col) to avoid duplicates
        seen_positions: set = set()

        for i, line in enumerate(lines):
            for m in tag_re.finditer(line):
                tag = m.group(1).lower()
                attrs = m.group(2)
                id_match = id_re.search(attrs)
                line_no = i + 1
                pos_key = (line_no, m.start())

                if pos_key in seen_positions:
                    continue

                if tag in major_tags:
                    end_line = self._find_html_close_tag(lines, i, tag)
                    sym_name = tag
                    if id_match:
                        sym_name = f"{tag}#{id_match.group(1)}"
                    parent = _find_parent(line_no)
                    seen_positions.add(pos_key)
                    symbols.append(
                        SymbolInfo(
                            name=sym_name,
                            symbol_type=SymbolType.CLASS,
                            start_line=line_no,
                            end_line=end_line,
                            parent=parent,
                            indentation=0,
                            code="\n".join(lines[line_no - 1 : end_line]),
                            signature=lines[i].strip(),
                        )
                    )

                elif tag == "style":
                    style_counter += 1
                    end_line = self._find_html_close_tag(lines, i, "style")
                    sym_name = f"style_{style_counter}"
                    if id_match:
                        sym_name = f"style#{id_match.group(1)}"
                    parent = _find_parent(line_no)
                    seen_positions.add(pos_key)
                    symbols.append(
                        SymbolInfo(
                            name=sym_name,
                            symbol_type=SymbolType.FUNCTION,
                            start_line=line_no,
                            end_line=end_line,
                            parent=parent,
                            indentation=0,
                            code="\n".join(lines[line_no - 1 : end_line]),
                            signature=lines[i].strip(),
                        )
                    )

                elif tag == "script":
                    script_counter += 1
                    end_line = self._find_html_close_tag(lines, i, "script")
                    sym_name = f"script_{script_counter}"
                    if id_match:
                        sym_name = f"script#{id_match.group(1)}"
                    parent = _find_parent(line_no)
                    seen_positions.add(pos_key)
                    symbols.append(
                        SymbolInfo(
                            name=sym_name,
                            symbol_type=SymbolType.FUNCTION,
                            start_line=line_no,
                            end_line=end_line,
                            parent=parent,
                            indentation=0,
                            code="\n".join(lines[line_no - 1 : end_line]),
                            signature=lines[i].strip(),
                        )
                    )

                elif tag == "table":
                    table_counter += 1
                    end_line = self._find_html_close_tag(lines, i, "table")
                    sym_name = f"table_{table_counter}"
                    if id_match:
                        sym_name = f"table#{id_match.group(1)}"
                    parent = _find_parent(line_no)
                    seen_positions.add(pos_key)
                    symbols.append(
                        SymbolInfo(
                            name=sym_name,
                            symbol_type=SymbolType.FUNCTION,
                            start_line=line_no,
                            end_line=end_line,
                            parent=parent,
                            indentation=0,
                            code="\n".join(lines[line_no - 1 : end_line]),
                            signature=lines[i].strip(),
                        )
                    )

                elif tag.startswith("h") and tag[1:].isdigit():
                    # Heading: extract text content
                    heading_counter[tag] = heading_counter.get(tag, 0) + 1
                    # Try to get closing tag on same line
                    heading_text = ""
                    close_heading_re = re.compile(
                        r"<" + re.escape(tag) + r"[^>]*>(.*?)</" + re.escape(tag) + r">",
                        re.IGNORECASE | re.DOTALL,
                    )
                    # Check current line for inline heading
                    hm = close_heading_re.search(line)
                    if hm:
                        # Strip any inner HTML tags from heading text
                        heading_text = re.sub(r"<[^>]+>", "", hm.group(1)).strip()
                        end_line = line_no
                    else:
                        # Multi-line heading — find close tag
                        end_line = self._find_html_close_tag(lines, i, tag)
                        # Extract text from the span
                        raw = "\n".join(lines[i:end_line])
                        heading_text = re.sub(r"<[^>]+>", "", raw).strip()

                    # Sanitize heading text for symbol name
                    sanitized = re.sub(r"[^a-zA-Z0-9_\- ]", "", heading_text).strip()
                    sanitized = re.sub(r"\s+", "_", sanitized)
                    if sanitized:
                        sym_name = f"{tag}_{sanitized}"
                    else:
                        sym_name = f"{tag}_{heading_counter[tag]}"

                    parent = _find_parent(line_no)
                    seen_positions.add(pos_key)
                    symbols.append(
                        SymbolInfo(
                            name=sym_name,
                            symbol_type=SymbolType.CLASS,
                            start_line=line_no,
                            end_line=end_line,
                            parent=parent,
                            indentation=0,
                            code="\n".join(lines[line_no - 1 : end_line]),
                            signature=lines[i].strip(),
                        )
                    )

        # Third pass: named elements (any tag with id) not already captured
        for i, line in enumerate(lines):
            line_no = i + 1
            for m in any_id_re.finditer(line):
                tag = m.group(1).lower()
                elem_id = m.group(3)
                # Skip tags we already handle above
                if tag in major_tags or tag in ("style", "script", "table") or (tag.startswith("h") and len(tag) == 2 and tag[1:].isdigit()):
                    continue
                end_line = self._find_html_close_tag(lines, i, tag)
                sym_name = f"{tag}#{elem_id}"
                parent = _find_parent(line_no)
                seen_positions.add((line_no, m.start()))
                symbols.append(
                    SymbolInfo(
                        name=sym_name,
                        symbol_type=SymbolType.CLASS,
                        start_line=line_no,
                        end_line=end_line,
                        parent=parent,
                        indentation=0,
                        code="\n".join(lines[line_no - 1 : end_line]),
                        signature=lines[i].strip(),
                    )
                )

        return symbols

    # ─── CSS Parser ──────────────────────────────────────────────────────────

    def _parse_css(self, source: str, lines: List[str]) -> List[SymbolInfo]:
        symbols: List[SymbolInfo] = []

        # @media queries
        media_re = re.compile(r"^(\s*)@media\b[^{]*\{", re.MULTILINE)
        # @keyframes
        keyframes_re = re.compile(r"^(\s*)@keyframes\s+([\w-]+)\s*\{", re.MULTILINE)
        # General @ rules (font-face, etc.)
        at_rule_re = re.compile(r"^(\s*)@([\w-]+)\b[^{]*\{", re.MULTILINE)
        # Regular selectors — lines that end with { and are not @ rules
        selector_re = re.compile(r"^(\s*)([^@/\s{}][^{}]*?)\s*\{", re.MULTILINE)

        seen_starts: set = set()

        # @keyframes
        for m in keyframes_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            if line_no in seen_starts:
                continue
            end_line = self._find_block_end(lines, line_no - 1)
            seen_starts.add(line_no)
            symbols.append(
                SymbolInfo(
                    name=f"@keyframes {m.group(2)}",
                    symbol_type=SymbolType.FUNCTION,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=len(m.group(1)),
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        # @media
        for m in media_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            if line_no in seen_starts:
                continue
            end_line = self._find_block_end(lines, line_no - 1)
            # Build a name from the media query
            raw_sig = m.group(0).strip().rstrip("{").strip()
            seen_starts.add(line_no)
            symbols.append(
                SymbolInfo(
                    name=raw_sig,
                    symbol_type=SymbolType.CLASS,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=len(m.group(1)),
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        # Regular selectors (top-level only — indentation == 0)
        for m in selector_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            if line_no in seen_starts:
                continue
            indent = len(m.group(1))
            selector_text = m.group(2).strip()
            # Skip comment lines
            if selector_text.startswith("/*") or selector_text.startswith("*"):
                continue
            end_line = self._find_block_end(lines, line_no - 1)
            seen_starts.add(line_no)
            symbols.append(
                SymbolInfo(
                    name=selector_text,
                    symbol_type=SymbolType.FUNCTION,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=indent,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )

        return symbols

    # ─── Markdown Parser ─────────────────────────────────────────────────────

    def _parse_markdown(self, source: str, lines: List[str]) -> List[SymbolInfo]:
        symbols: List[SymbolInfo] = []

        # Collect all headings first: (line_index_0based, level, text)
        heading_re = re.compile(r"^(#{1,6})\s+(.+)$")
        headings: List[Tuple[int, int, str]] = []
        for i, line in enumerate(lines):
            m = heading_re.match(line.rstrip())
            if m:
                level = len(m.group(1))
                text = m.group(2).strip()
                headings.append((i, level, text))

        if not headings:
            return symbols

        # For each heading, determine end line (next heading of same/higher level or EOF)
        for idx, (line_idx, level, text) in enumerate(headings):
            # Find end: next heading with level <= this level
            end_line_0 = len(lines) - 1  # default to EOF (0-based last line index)
            for next_idx in range(idx + 1, len(headings)):
                next_line_idx, next_level, _ = headings[next_idx]
                if next_level <= level:
                    end_line_0 = next_line_idx - 1
                    break

            start_line = line_idx + 1  # 1-based
            end_line = end_line_0 + 1  # 1-based

            # Find parent: closest preceding heading with strictly lower level number
            parent = None
            for prev_idx in range(idx - 1, -1, -1):
                prev_line_idx, prev_level, prev_text = headings[prev_idx]
                if prev_level < level:
                    parent = prev_text
                    break

            symbols.append(
                SymbolInfo(
                    name=text,
                    symbol_type=SymbolType.CLASS,
                    start_line=start_line,
                    end_line=end_line,
                    parent=parent,
                    indentation=level,
                    code="\n".join(lines[line_idx : end_line_0 + 1]),
                    signature=lines[line_idx].strip(),
                )
            )

        return symbols

    # ─── Generic Fallback ────────────────────────────────────────────────────

    def _parse_generic(self, source: str, lines: List[str]) -> List[SymbolInfo]:
        """Heuristic-based fallback for unsupported languages."""
        symbols: List[SymbolInfo] = []
        # Match common function-like patterns
        generic_re = re.compile(
            r"^(\s*)(?:function|def|fn|func|sub|proc)\s+(\w+)\s*\(", re.MULTILINE
        )
        for m in generic_re.finditer(source):
            line_no = source[: m.start()].count("\n") + 1
            end_line = self._find_block_end(lines, line_no - 1)
            indent = len(m.group(1))
            symbols.append(
                SymbolInfo(
                    name=m.group(2),
                    symbol_type=SymbolType.FUNCTION,
                    start_line=line_no,
                    end_line=end_line,
                    parent=None,
                    indentation=indent,
                    code="\n".join(lines[line_no - 1 : end_line]),
                    signature=m.group(0).strip(),
                )
            )
        return symbols

    # ─── Import Extractor ────────────────────────────────────────────────────

    def _extract_imports(self, source: str, language: str) -> List[str]:
        imports = []
        lines = source.splitlines()
        for line in lines[:50]:  # Imports are always near the top
            stripped = line.strip()
            if language == "python" and (
                stripped.startswith("import ") or stripped.startswith("from ")
            ):
                imports.append(stripped)
            elif language in ("javascript", "typescript") and (
                stripped.startswith("import ") or stripped.startswith("require(")
            ):
                imports.append(stripped)
            elif language == "go" and (
                stripped.startswith("import") or stripped.startswith('"')
            ):
                imports.append(stripped)
            elif language == "rust" and stripped.startswith("use "):
                imports.append(stripped)
        return imports

    # ─── Block End Finder ────────────────────────────────────────────────────

    def _find_block_end(self, lines: List[str], start_idx: int) -> int:
        """
        Find the end of a code block by tracking brace depth or indentation.
        start_idx is 0-based.
        """
        if start_idx >= len(lines):
            return start_idx + 1

        start_line = lines[start_idx]
        start_indent = len(start_line) - len(start_line.lstrip())

        # Try brace matching first
        # Track paren depth so braces inside parameter destructuring
        # e.g. function Foo({ a, b }: Props) { are NOT counted as block boundaries
        depth_brace = 0
        depth_paren = 0
        found_open = False
        for i in range(start_idx, len(lines)):
            for ch in lines[i]:
                if ch == "(":
                    depth_paren += 1
                elif ch == ")":
                    if depth_paren > 0:
                        depth_paren -= 1
                elif ch == "{" and depth_paren == 0:
                    depth_brace += 1
                    found_open = True
                elif ch == "}" and depth_paren == 0:
                    depth_brace -= 1
                    if found_open and depth_brace == 0:
                        return i + 1  # 1-based

        # Fallback: indentation-based (Python style)
        for i in range(start_idx + 1, len(lines)):
            line = lines[i]
            if line.strip() == "":
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= start_indent and line.strip():
                return i  # 1-based (line before this one)

        return min(start_idx + 200, len(lines))
