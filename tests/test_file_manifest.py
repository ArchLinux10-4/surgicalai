"""
Tests for session file manifest — DB-backed file tracking across chat turns.

Validates:
  1. New files are classified as "new"
  2. Unchanged files across turns are classified as "unchanged"
  3. Modified files (same name, different content) are classified as "modified"
  4. Badge rendering for each status
  5. Hash stability
  6. Graceful fallback when no session_id
"""
import ast
import os
import sys
import types
import textwrap
import sqlite3
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.join(_HERE, "..", "backend", "services", "pipeline.py"),
    os.path.join(_HERE, "..", "pipeline.py"),
]
_PIPELINE = None
for _c in _CANDIDATES:
    if os.path.isfile(_c):
        _PIPELINE = os.path.abspath(_c)
        break

if not _PIPELINE:
    raise FileNotFoundError(
        f"pipeline.py not found. Searched:\n" +
        "\n".join(f"  {os.path.abspath(c)}" for c in _CANDIDATES)
    )

# ── AST-based extraction (same pattern as test_safe_claude_call.py) ──────────

_FUNCS_TO_EXTRACT = [
    "_file_content_hash",
    "_ensure_file_manifest_table",
    "_classify_session_files",
    "_file_status_badge",
]

_CONSTANTS_TO_EXTRACT = [
    "_FILE_NEW",
    "_FILE_MODIFIED",
    "_FILE_UNCHANGED",
]

_DB_PATH = "/tmp/test_file_manifest.db"


def _extract_functions():
    src = open(_PIPELINE, encoding="utf-8").read()
    tree = ast.parse(src)

    # Collect top-level function source
    func_sources = {}
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _FUNCS_TO_EXTRACT:
                start = node.lineno - 1
                end = node.end_lineno
                func_sources[node.name] = "".join(lines[start:end])

    # Collect constants
    const_sources = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _CONSTANTS_TO_EXTRACT:
                    start = node.lineno - 1
                    end = node.end_lineno
                    const_sources[target.id] = "".join(lines[start:end])

    # Build namespace with stubs
    ns = {}

    # Stub _hashlib
    import hashlib
    ns["_hashlib"] = hashlib

    # Stub _dlog
    _dlog_calls = []
    def _dlog(event, **kwargs):
        _dlog_calls.append((event, kwargs))
    ns["_dlog"] = _dlog
    ns["_dlog_calls"] = _dlog_calls

    # Stub database module with real SQLite
    stub_db = types.ModuleType("database")

    def get_db_connection():
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    class _Ctx:
        def __enter__(self):
            self.conn = sqlite3.connect(_DB_PATH)
            return self.conn
        def __exit__(self, *a):
            self.conn.close()

    stub_db.get_db_connection = get_db_connection
    stub_db.get_db_ctx = lambda: _Ctx()
    sys.modules["database"] = stub_db

    # Execute constants first, then functions
    for name, source in const_sources.items():
        exec(compile(source, _PIPELINE, "exec"), ns)

    for name, source in func_sources.items():
        exec(compile(source, _PIPELINE, "exec"), ns)

    return ns


NS = _extract_functions()

_classify = NS["_classify_session_files"]
_badge = NS["_file_status_badge"]
_hash = NS["_file_content_hash"]
_FILE_NEW = NS["_FILE_NEW"]
_FILE_MODIFIED = NS["_FILE_MODIFIED"]
_FILE_UNCHANGED = NS["_FILE_UNCHANGED"]


def _cleanup_db():
    """Reset the test DB between tests."""
    if os.path.exists(_DB_PATH):
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("DROP TABLE IF EXISTS session_file_manifest")
        conn.commit()
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestFileContentHash:
    def test_same_content_same_hash(self):
        assert _hash("hello world") == _hash("hello world")

    def test_different_content_different_hash(self):
        assert _hash("hello") != _hash("world")

    def test_hash_is_16_chars(self):
        assert len(_hash("test content")) == 16


class TestFileBadge:
    def test_new_badge(self):
        badge = _badge(_FILE_NEW)
        assert "NEW" in badge

    def test_modified_badge(self):
        badge = _badge(_FILE_MODIFIED)
        assert "MODIFIED" in badge

    def test_unchanged_badge(self):
        badge = _badge(_FILE_UNCHANGED)
        assert "UNCHANGED" in badge

    def test_empty_status_empty_badge(self):
        assert _badge("") == ""


class TestClassifySessionFiles:
    def setup_method(self):
        _cleanup_db()

    def teardown_method(self):
        _cleanup_db()

    def test_first_turn_all_new(self):
        files = [
            {"filename": "App.tsx", "content": "export function App() {}"},
            {"filename": "screenshot.png", "content": "base64data", "file_type": "image"},
        ]
        result = _classify("session-1", files, [])
        assert result["App.tsx"]["status"] == _FILE_NEW
        assert result["screenshot.png"]["status"] == _FILE_NEW

    def test_second_turn_unchanged(self):
        files = [
            {"filename": "App.tsx", "content": "export function App() {}"},
        ]
        # Turn 1
        _classify("session-2", files, [])
        # Turn 2 — same files, some history
        history = [{"role": "user", "content": "fix the bug"}, {"role": "assistant", "content": "done"}]
        result = _classify("session-2", files, history)
        assert result["App.tsx"]["status"] == _FILE_UNCHANGED

    def test_modified_file_detected(self):
        files_v1 = [{"filename": "App.tsx", "content": "version 1"}]
        files_v2 = [{"filename": "App.tsx", "content": "version 2 with changes"}]
        # Turn 1
        _classify("session-3", files_v1, [])
        # Turn 2 — same filename, different content
        history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
        result = _classify("session-3", files_v2, history)
        assert result["App.tsx"]["status"] == _FILE_MODIFIED

    def test_new_file_added_second_turn(self):
        files_t1 = [{"filename": "App.tsx", "content": "app code"}]
        files_t2 = [
            {"filename": "App.tsx", "content": "app code"},
            {"filename": "new-screenshot.png", "content": "new image data", "file_type": "image"},
        ]
        # Turn 1
        _classify("session-4", files_t1, [])
        # Turn 2
        history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
        result = _classify("session-4", files_t2, history)
        assert result["App.tsx"]["status"] == _FILE_UNCHANGED
        assert result["new-screenshot.png"]["status"] == _FILE_NEW

    def test_no_session_id_returns_empty(self):
        files = [{"filename": "App.tsx", "content": "code"}]
        result = _classify("", files, [])
        assert result == {}

    def test_mixed_scenario_three_turns(self):
        """Turn 1: file A. Turn 2: file A + B. Turn 3: file A (modified) + B + C."""
        sid = "session-5"
        # Turn 1
        _classify(sid, [{"filename": "A.tsx", "content": "original A"}], [])
        # Turn 2
        h1 = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
        r2 = _classify(sid, [
            {"filename": "A.tsx", "content": "original A"},
            {"filename": "B.py", "content": "file B"},
        ], h1)
        assert r2["A.tsx"]["status"] == _FILE_UNCHANGED
        assert r2["B.py"]["status"] == _FILE_NEW
        # Turn 3
        h2 = h1 + [{"role": "user", "content": "z"}, {"role": "assistant", "content": "w"}]
        r3 = _classify(sid, [
            {"filename": "A.tsx", "content": "modified A content"},
            {"filename": "B.py", "content": "file B"},
            {"filename": "C.css", "content": "new file C"},
        ], h2)
        assert r3["A.tsx"]["status"] == _FILE_MODIFIED
        assert r3["B.py"]["status"] == _FILE_UNCHANGED
        assert r3["C.css"]["status"] == _FILE_NEW

    def test_dlog_called_on_classify(self):
        """Verify _dlog is called with file_manifest_classified event."""
        NS["_dlog_calls"].clear()
        files = [{"filename": "test.py", "content": "print('hi')"}]
        _classify("session-dlog", files, [])
        events = [call[0] for call in NS["_dlog_calls"]]
        assert "file_manifest_classified" in events
