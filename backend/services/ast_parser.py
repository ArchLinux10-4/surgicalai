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
        arrow_re = re.compile(
            r"^(export\s+)?(const|let|var)\s+(\w+)\s*=\s*(async\s+)?\(.*?\)\s*=>",
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

        return symbols

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
        depth = 0
        found_open = False
        for i in range(start_idx, min(start_idx + 2000, len(lines))):
            for ch in lines[i]:
                if ch == "{":
                    depth += 1
                    found_open = True
                elif ch == "}":
                    depth -= 1
                    if found_open and depth == 0:
                        return i + 1  # 1-based

        # Fallback: indentation-based (Python style)
        for i in range(start_idx + 1, min(start_idx + 2000, len(lines))):
            line = lines[i]
            if line.strip() == "":
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= start_indent and line.strip():
                return i  # 1-based (line before this one)

        return min(start_idx + 200, len(lines))
