"""
Regression test for the large-file <file_request> deadlock.

Trace c5e5194a proved: three tier-1 files >500 lines were rendered as grep
EXCERPTS only, but the agent-loop file_request guard told the model they were
"already fully loaded" and refused to serve them.  With every file_request
blocked, the model could only <search_request> grep fragments, never assembled
a full function body, and hit the phase deadline with zero edits written.

The fix: _build_natural_file_context now returns a third value,
`tier1_excerpt_only` — the set of tier-1 files that were shown as excerpts
(large grep window, or truncated symbol-less preview) rather than full
content.  The guard must let a file_request for those files fall through to the
handler (which serves FULL CONTENT once) instead of redirecting.

This test verifies:
  1. FUNCTIONAL: a >500-line tier-1 code file is flagged excerpt-only, while a
     small file rendered as FULL CONTENT is NOT.
  2. SOURCE: the file_request guard only redirects fully-loaded files and lets
     excerpt-only files pass through.
"""
import ast
import os
import re
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.normpath(os.path.join(_HERE, "..", "backend", "services", "pipeline.py"))


def _load_source():
    with open(_PIPELINE, "r") as f:
        return f.read()


def _extract_function(src, name):
    """Return the source text of a top-level function by name."""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"function {name} not found in pipeline.py")


class _FakeSymbolType:
    def __init__(self, value):
        self.value = value


class _FakeSymbol:
    def __init__(self, start, end):
        self.symbol_type = _FakeSymbolType("function")
        self.full_path = "do_thing"
        self.start_line = start
        self.end_line = end


class _FakeSymbolMap:
    def __init__(self, start, end):
        self.symbols = [_FakeSymbol(start, end)]


class LargeFileExcerptFunctional(unittest.TestCase):
    """Exec _build_natural_file_context in isolation with light stubs."""

    def setUp(self):
        src = _load_source()
        fn_src = _extract_function(src, "_build_natural_file_context")
        ns = {
            "re": re,
            "_extract_search_terms": lambda s: [],
            "_score_file_relevance": lambda sf, smap, req, terms: 1,  # positive -> tier1
            "_file_status_badge": lambda s: "",
            "LARGE_FILE_WINDOW": 500,
            "_grep_relevant_sections": lambda *a, **k: "L10: do_thing()\nL11: return x",
            "_dlog": lambda *a, **k: None,
        }
        exec(compile(fn_src, _PIPELINE, "exec"), ns)
        self._build = ns["_build_natural_file_context"]

    def _run(self):
        big_content = "\n".join(f"line {i}" for i in range(600))   # 600 lines > 500
        small_content = "\n".join(f"line {i}" for i in range(20))  # 20 lines <= 500
        session_files = [
            {"filename": "big.py",   "content": big_content,   "file_type": "code", "lines": 600},
            {"filename": "small.py", "content": small_content, "file_type": "code", "lines": 20},
        ]
        symbol_maps_by_name = {
            "big.py":   (_FakeSymbolMap(1, 600), session_files[0]),
            "small.py": (_FakeSymbolMap(1, 20),  session_files[1]),
        }
        return self._build(
            session_files, symbol_maps_by_name, "fix the do_thing function",
        )

    def test_returns_three_tuple(self):
        result = self._run()
        self.assertEqual(len(result), 3,
                         "must return (context, tier1_names, tier1_excerpt_only)")

    def test_large_file_flagged_excerpt_only(self):
        _ctx, tier1_names, excerpt_only = self._run()
        self.assertIn("big.py", tier1_names)
        self.assertIn("big.py", excerpt_only,
                      ">500-line file shown as grep excerpt must be excerpt-only")

    def test_small_file_not_excerpt_only(self):
        _ctx, tier1_names, excerpt_only = self._run()
        self.assertIn("small.py", tier1_names)
        self.assertNotIn("small.py", excerpt_only,
                         "<=500-line file shown as FULL CONTENT must NOT be excerpt-only")

    def test_large_file_context_says_grep(self):
        ctx, _n, _e = self._run()
        self.assertIn("LARGE FILE", ctx)  # confirms grep-excerpt branch actually ran


class GuardConsumesExcerptSet(unittest.TestCase):
    """Source-level checks that the guard uses tier1_excerpt_only correctly."""

    def setUp(self):
        self.src = _load_source()

    def test_caller_unpacks_three_values(self):
        self.assertIn(
            "file_context, _tier1_names, _tier1_excerpt_only = _build_natural_file_context(",
            self.src,
            "caller must unpack the new excerpt-only set",
        )

    def test_guard_excludes_excerpt_only(self):
        # The redirect predicate must require files NOT be excerpt-only.
        self.assertIn("fn not in _tier1_excerpt_only", self.src)

    def test_guard_has_passthrough(self):
        # Excerpt-only requests must be detected and NOT redirected.
        self.assertIn("_excerpt_req = [fn for fn in fnames_req if fn in _tier1_excerpt_only]", self.src)
        self.assertIn("if (not _excerpt_req)", self.src)

    def test_guard_logs_passthrough(self):
        self.assertIn("agent_filereq_largefile_passthrough", self.src)


if __name__ == "__main__":
    unittest.main()
