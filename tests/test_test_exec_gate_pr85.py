"""
PR #85 — whole-session test-execution gate (natural/Claude pipeline parity).

These tests exercise the REAL shipped source of `_assemble_patched_file_map` and
`_run_tests_inline` by extracting them from backend/services/pipeline.py with AST
(no heavy service imports), so they validate the exact code that ships.

Run:  python -m pytest tests/test_test_exec_gate_pr85.py -q
"""
import ast
import asyncio
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.normpath(os.path.join(_HERE, "..", "backend", "services", "pipeline.py"))


def _load(*names):
    src = open(_PIPELINE, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            exec(ast.get_source_segment(src, node), ns)
    return ns


_NS = _load("_assemble_patched_file_map", "_run_tests_inline")
assemble = _NS["_assemble_patched_file_map"]
run_tests = _NS["_run_tests_inline"]


class _Sym:
    def __init__(self, code):
        self.code = code


def _cs(filename, orig, new, qa_orig=None):
    return {"filename": filename, "symbol": _Sym(orig), "new_code": new,
            "qa_original_content": qa_orig, "description": "test"}


# ── _assemble_patched_file_map ───────────────────────────────────────────────

def test_assemble_applies_change_and_preserves_siblings():
    sess = [
        {"filename": "app.py", "content": "def add(a,b):\n    return a+b\n"},
        {"filename": "util.py", "content": "X = 1\n"},
        {"filename": "tests/test_app.py", "content": "from app import add\n"},
    ]
    out = assemble(sess, [_cs("app.py", "def add(a,b):\n    return a+b\n",
                                          "def add(a,b):\n    return a-b\n")])
    assert out["app.py"] == "def add(a,b):\n    return a-b\n"
    assert out["util.py"] == "X = 1\n"            # un-edited sibling preserved
    assert "tests/test_app.py" in out             # test file carried through


def test_assemble_multiple_changes_same_file():
    sess = [{"filename": "util.py", "content": "X = 1\nY = 2\n"}]
    out = assemble(sess, [_cs("util.py", "X = 1", "X = 10"),
                          _cs("util.py", "Y = 2", "Y = 20")])
    assert out["util.py"] == "X = 10\nY = 20\n"


def test_assemble_missing_original_is_graceful():
    sess = [{"filename": "app.py", "content": "keep me\n"}]
    out = assemble(sess, [_cs("app.py", "NOT PRESENT", "whatever")])
    assert out["app.py"] == "keep me\n"           # no crash, unchanged


def test_assemble_file_not_in_session_uses_qa_original():
    out = assemble([], [_cs("new.py", "old", "new", qa_orig="old\n")])
    assert out["new.py"] == "new\n"


def test_assemble_none_and_empty_safe():
    assert assemble(None, None) == {}
    assert assemble([], []) == {}


# ── _run_tests_inline (core mechanism: good->pass, buggy->fail, none->skip) ──

def _ensure_pytest_on_path():
    # The runner shells out to `python -m pytest`; make sure that interpreter has pytest.
    os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")


def test_runner_passes_on_good_change():
    _ensure_pytest_on_path()
    files = {
        "calc.py": "def add(a, b):\n    s = a + b\n    return s\n",
        "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    }
    res = asyncio.run(run_tests(files, "s"))
    assert res["verdict"] == "passed"
    assert res["passed"] == 1 and res["failed"] == 0


def test_runner_fails_on_buggy_change():
    _ensure_pytest_on_path()
    files = {
        "calc.py": "def add(a, b):\n    return a - b\n",
        "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    }
    res = asyncio.run(run_tests(files, "s"))
    assert res["verdict"] == "failed"
    assert res["failed"] >= 1


def test_runner_skips_when_no_tests():
    res = asyncio.run(run_tests({"calc.py": "def add(a,b): return a+b\n"}, "s"))
    assert res["verdict"] == "skipped"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
