"""
Regression test for the file-request dict-shape starvation bug.

Root cause (proven from session d8f0ed39, uploaded by user, 2026-07-23):

`_parse_filereq_content` only accepted a JSON array (`["a.py", "b.py"]`) or
a bare JSON string (`"a.py"`). Claude, following the same dict convention
used by the neighboring `<search_request>` (`{"terms": [...]}`) and
`<history_request>` (`{"filename": ..., "query": ...}`) tools documented in
the very same instruction block, sometimes emits `<file_request>` as a JSON
object too, e.g. `{"filename": "jobRoutes.js"}`.

A dict is valid JSON, so the old code never reached its non-JSON fallback
branch either -- it matched neither `isinstance(parsed, list)` nor
`isinstance(parsed, str)` and fell straight through to `return []`, with
NO log trace of what happened. The file request was silently dropped, the
model never got the file it explicitly asked for, and it gave up
(`<blocked>`) after only 3 of 24 available turns -- nowhere near a
turn-budget exhaustion, proving the "need more search rounds" hypothesis
wrong and this parser gap the actual cause.

This test replays the exact failing payload byte-for-byte and locks in the
fix without touching any of the previously-working input shapes.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import _parse_filereq_content  # noqa: E402


def test_real_case_dict_single_filename_from_session_d8f0ed39():
    """Byte-for-byte replay of the exact tag body that starved the model in
    session d8f0ed39: <file_request>{"filename": "jobRoutes.js"}</file_request>."""
    raw = '{"filename": "jobRoutes.js"}'
    assert _parse_filereq_content(raw) == ["jobRoutes.js"]


def test_dict_plural_filenames_list():
    raw = '{"filenames": ["a.py", "b.tsx"]}'
    assert _parse_filereq_content(raw) == ["a.py", "b.tsx"]


def test_dict_with_whitespace_in_value_is_stripped():
    raw = '{"filename": "  spaced.py  "}'
    assert _parse_filereq_content(raw) == ["spaced.py"]


def test_dict_unrecognized_keys_returns_empty_not_crash():
    """A dict shape with neither 'filename' nor 'filenames' must degrade to
    an empty result, never raise."""
    raw = '{"totally": "unrelated"}'
    assert _parse_filereq_content(raw) == []


def test_dict_empty_filename_value_returns_empty():
    raw = '{"filename": ""}'
    assert _parse_filereq_content(raw) == []


def test_existing_json_array_shape_unchanged():
    """No regression: the documented array shape must keep working exactly
    as before."""
    raw = '["file1.py", "file2.tsx"]'
    assert _parse_filereq_content(raw) == ["file1.py", "file2.tsx"]


def test_existing_bare_string_shape_unchanged():
    raw = '"solo.py"'
    assert _parse_filereq_content(raw) == ["solo.py"]


def test_existing_plain_text_fallback_unchanged():
    """No regression: comma/newline separated plain filenames (non-JSON)
    must still work via the pre-existing fallback path."""
    raw = "a.py, b.tsx"
    assert _parse_filereq_content(raw) == ["a.py", "b.tsx"]


def test_empty_input_returns_empty():
    assert _parse_filereq_content("") == []
    assert _parse_filereq_content("   ") == []
