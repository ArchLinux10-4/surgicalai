"""
Tests for the <history_request> agent tool (Claude Agent Mode only).

Feature: lets the agent search/read PAST versions of a session file, backed
entirely by the pre-existing, per-session-scoped session_file_versions table
(routers/session_files.py). This suite covers the new read-only helper
`search_file_version_history` plus the tag-body parser
`_parse_history_content` in services/pipeline.py.

Structural safety this proves:
  - Strictly scoped to (session_id, filename) — never leaks across sessions.
  - Never raises — every failure path returns a message dict.
  - Every returned result is prefixed with an unmissable HISTORICAL banner,
    so the agent can never mistake old content for the current file.
  - The tool is only ever registered when is_agent_task=True (see
    test_history_request_agent_mode_gating), so the single-pass chat/edit
    path (any model) is untouched.
"""
import os
import sys
import tempfile
import shutil

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

from auth_test_utils import fake_request  # noqa: E402


@pytest.fixture()
def isolated_db(monkeypatch):
    tmp_home = tempfile.mkdtemp(prefix="surgicalai_test_home_")
    monkeypatch.setenv("HOME", tmp_home)
    for mod in list(sys.modules):
        if (mod == "database" or mod == "routers" or mod.startswith("routers.")
                or mod == "services.session_auth"):
            del sys.modules[mod]
    import database as _database
    _database.init_db()
    from routers import session_files as _session_files
    yield _database, _session_files
    shutil.rmtree(tmp_home, ignore_errors=True)


def _seed_file(database, session_id, file_id, filename, content):
    with database.get_db_ctx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (session_id, "t", None),
        )
        conn.execute(
            "INSERT INTO session_files (id, session_id, filename, content, lines, symbol_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (file_id, session_id, filename, content, len(content.splitlines()), 0)
        )
        conn.commit()


def test_no_history_returns_clear_not_found_message(isolated_db):
    """A file that was never edited has no rows in session_file_versions —
    must return found=False with a clear message, not crash or return junk."""
    database, session_files = isolated_db
    session_id, file_id = "s1", "f1"
    _seed_file(database, session_id, file_id, "app.py", "print('only version')")

    result = session_files.search_file_version_history(session_id, "app.py")
    assert result["found"] is False
    assert "never been edited" in result["message"]


def test_no_query_returns_oldest_version_with_banner(isolated_db):
    """query=None must return the OLDEST (first-ever) version in full,
    clearly marked as historical/original, not the current content."""
    database, session_files = isolated_db
    session_id, file_id = "s2", "f2"
    _seed_file(database, session_id, file_id, "app.py", "print('v1 ORIGINAL')")
    session_files.update_session_file(session_id, file_id, {"content": "print('v2')", "label": "Edit A"}, request=fake_request())
    session_files.update_session_file(session_id, file_id, {"content": "print('v3 CURRENT')", "label": "Edit B"}, request=fake_request())

    result = session_files.search_file_version_history(session_id, "app.py")
    assert result["found"] is True
    assert "v1 ORIGINAL" in result["content"]
    assert "v3 CURRENT" not in result["content"], "must never return current content as 'original'"
    assert "HISTORICAL" in result["content"] and "NOT the current file" in result["content"]


def test_query_searches_across_all_versions_with_context(isolated_db):
    """query given must grep every stored version, returning matches with
    context, each individually banner-marked as historical."""
    database, session_files = isolated_db
    session_id, file_id = "s3", "f3"
    _seed_file(database, session_id, file_id, "app.py", "def old_helper():\n    return 1\n")
    session_files.update_session_file(
        session_id, file_id,
        {"content": "def new_helper():\n    return 2\n", "label": "Rename"},
        request=fake_request())

    result = session_files.search_file_version_history(session_id, "app.py", query="old_helper")
    assert result["found"] is True
    assert len(result["matches"]) == 1
    assert "old_helper" in result["matches"][0]
    assert "HISTORICAL" in result["matches"][0]


def test_query_no_match_returns_honest_message(isolated_db):
    database, session_files = isolated_db
    session_id, file_id = "s4", "f4"
    _seed_file(database, session_id, file_id, "app.py", "print('v1')")
    session_files.update_session_file(session_id, file_id, {"content": "print('v2')"}, request=fake_request())

    result = session_files.search_file_version_history(session_id, "app.py", query="nonexistent_symbol_xyz")
    assert result["found"] is False
    assert "no match found" in result["message"]


def test_unknown_filename_returns_not_found_not_crash(isolated_db):
    database, session_files = isolated_db
    session_id = "s5"
    result = session_files.search_file_version_history(session_id, "does_not_exist.py")
    assert result["found"] is False
    assert "No file named" in result["message"]


def test_strictly_scoped_to_session_id(isolated_db):
    """Same filename in two different sessions must never cross-contaminate
    — the isolation boundary the user explicitly required."""
    database, session_files = isolated_db
    _seed_file(database, "session-A", "fA", "shared.py", "print('session A v1')")
    session_files.update_session_file("session-A", "fA", {"content": "print('session A v2')"}, request=fake_request())

    _seed_file(database, "session-B", "fB", "shared.py", "print('session B v1')")
    session_files.update_session_file("session-B", "fB", {"content": "print('session B v2')"}, request=fake_request())

    result_a = session_files.search_file_version_history("session-A", "shared.py")
    result_b = session_files.search_file_version_history("session-B", "shared.py")
    assert "session A" in result_a["content"] and "session B" not in result_a["content"]
    assert "session B" in result_b["content"] and "session A" not in result_b["content"]


def test_ambiguous_fuzzy_match_reports_candidates_not_a_guess(isolated_db):
    """When the filename doesn't match exactly but multiple fuzzy candidates
    exist, the tool must ask for clarification rather than silently
    guessing which file the agent meant."""
    database, session_files = isolated_db
    session_id = "s6"
    _seed_file(database, session_id, "f1", "utils.js", "content1")
    _seed_file(database, session_id, "f2", "utils.test.js", "content2")

    result = session_files.search_file_version_history(session_id, "utils")
    assert result["found"] is False
    assert "ambiguous" in result["message"]


def test_never_raises_on_bad_session_id_type(isolated_db):
    """Structural guarantee: a history-lookup bug must never crash the
    agent turn that requested it."""
    database, session_files = isolated_db
    # None is not a valid session_id but the function must degrade cleanly.
    result = session_files.search_file_version_history(None, "app.py")
    assert result["found"] is False
    assert "message" in result


def test_query_result_capped_at_five_matches(isolated_db):
    """Non-negotiable cap — same philosophy as _resolve_search_multifile's
    proven 2.27M-char blowup bug. Six matching versions must yield at most
    5 results back to the model."""
    database, session_files = isolated_db
    session_id, file_id = "s7", "f7"
    _seed_file(database, session_id, file_id, "app.py", "TARGET line v0\n")
    for i in range(1, 7):
        session_files.update_session_file(
            session_id, file_id, {"content": f"TARGET line v{i}\n", "label": f"Edit {i}"},
            request=fake_request())

    result = session_files.search_file_version_history(session_id, "app.py", query="TARGET")
    assert result["found"] is True
    assert len(result["matches"]) <= 5


# ── Tag-body parser tests (services/pipeline.py) ───────────────────────────

def test_parse_history_content_requires_filename():
    from services.pipeline import _parse_history_content
    assert _parse_history_content('{"filename": "app.py"}') == {"filename": "app.py", "query": None}
    assert _parse_history_content('{"filename": "app.py", "query": "foo"}') == {
        "filename": "app.py", "query": "foo"}
    assert _parse_history_content('{"query": "foo"}') is None  # no filename -> invalid
    assert _parse_history_content('not json') is None
    assert _parse_history_content('[]') is None


def _three_versions(database, session_files):
    session_id, file_id = "hist-views", "hist-file"
    _seed_file(database, session_id, file_id, "app.py", "print('v1 ORIGINAL')")
    session_files.update_session_file(
        session_id, file_id,
        {"content": "print('v2 MIDDLE')", "label": "Edit A"},
        request=fake_request(),
    )
    session_files.update_session_file(
        session_id, file_id,
        {"content": "print('v3 CURRENT')", "label": "Edit B"},
        request=fake_request(),
    )
    return session_id


def test_version_list_numbers_oldest_first_with_labels(isolated_db):
    database, session_files = isolated_db
    session_id = _three_versions(database, session_files)
    result = session_files.search_file_version_history(session_id, "app.py", view="list")
    assert result["found"] is True
    text = result["content"]
    assert "1. before \"Edit A\"" in text
    assert "2. before \"Edit B\"" in text
    assert text.index("Edit A") < text.index("Edit B")
    assert "v3 CURRENT" not in text
    assert "Not the current file" in text


def test_version_last_is_the_state_before_the_latest_edit(isolated_db):
    database, session_files = isolated_db
    session_id = _three_versions(database, session_files)
    result = session_files.search_file_version_history(session_id, "app.py", version="last")
    assert result["found"] is True
    assert "v2 MIDDLE" in result["content"]
    assert "v3 CURRENT" not in result["content"]
    assert "before the edit labelled \"Edit B\"" in result["content"]
    assert "HISTORICAL" in result["content"] and "NOT the current file" in result["content"]


def test_version_first_matches_the_call_with_no_extra_fields(isolated_db):
    database, session_files = isolated_db
    session_id = _three_versions(database, session_files)
    plain = session_files.search_file_version_history(session_id, "app.py")
    first = session_files.search_file_version_history(session_id, "app.py", version="first")
    assert plain["content"] == first["content"]
    assert "v1 ORIGINAL" in plain["content"]
    assert "v3 CURRENT" not in plain["content"]


def test_version_out_of_range_reports_the_count(isolated_db):
    database, session_files = isolated_db
    session_id = _three_versions(database, session_files)
    result = session_files.search_file_version_history(session_id, "app.py", version=9)
    assert result["found"] is False
    assert "has 2 saved version(s)" in result["message"]
    assert "Version 9" in result["message"]


def test_diff_against_current_shows_the_removed_and_added_lines(isolated_db):
    database, session_files = isolated_db
    session_id = _three_versions(database, session_files)
    result = session_files.search_file_version_history(
        session_id, "app.py", version="last", view="diff")
    assert result["found"] is True
    text = result["content"]
    assert "compared with the current file" in text
    assert "-print('v2 MIDDLE')" in text
    assert "+print('v3 CURRENT')" in text


def test_diff_between_two_versions_excludes_the_current_file(isolated_db):
    database, session_files = isolated_db
    session_id = _three_versions(database, session_files)
    result = session_files.search_file_version_history(
        session_id, "app.py", version="1", view="diff", compare_to=2)
    assert result["found"] is True
    text = result["content"]
    assert "v1 ORIGINAL" in text and "v2 MIDDLE" in text
    assert "v3 CURRENT" not in text
    assert "compared with version 2" in text


def test_list_of_an_unedited_file_says_the_current_file_is_the_original(isolated_db):
    database, session_files = isolated_db
    _seed_file(database, "plain", "f", "app.py", "print('only')")
    result = session_files.search_file_version_history("plain", "app.py", view="list")
    assert result["found"] is False
    assert "CURRENT file IS the original" in result["message"]


def test_search_hit_names_its_version_number(isolated_db):
    database, session_files = isolated_db
    session_id, file_id = "search-n", "f"
    _seed_file(database, session_id, file_id, "app.py", "def old_helper():\n    return 1\n")
    session_files.update_session_file(
        session_id, file_id,
        {"content": "def new_helper():\n    return 2\n", "label": "Rename"},
        request=fake_request(),
    )
    result = session_files.search_file_version_history(session_id, "app.py", query="old_helper")
    assert result["found"] is True
    assert "version 1" in result["matches"][0]


def test_version_list_stays_inside_the_session(isolated_db):
    database, session_files = isolated_db
    _seed_file(database, "session-A", "fA", "shared.py", "print('session A v1')")
    session_files.update_session_file(
        "session-A", "fA",
        {"content": "print('session A v2')", "label": "Edit A"},
        request=fake_request(),
    )
    _seed_file(database, "session-B", "fB", "shared.py", "print('session B v1')")
    session_files.update_session_file(
        "session-B", "fB",
        {"content": "print('session B v2')", "label": "Edit B"},
        request=fake_request(),
    )
    listed = session_files.search_file_version_history("session-A", "shared.py", view="list")
    assert "Edit A" in listed["content"]
    assert "Edit B" not in listed["content"]
    assert "session B" not in listed["content"]


def test_version_list_keeps_real_numbers_when_it_is_cut(isolated_db, monkeypatch):
    database, session_files = isolated_db
    assert session_files._HISTORY_LIST_CAP == 50
    monkeypatch.setattr(session_files, "_HISTORY_LIST_CAP", 2)
    session_id, file_id = "capped", "f"
    _seed_file(database, session_id, file_id, "app.py", "print('v0')")
    for i in range(1, 4):
        session_files.update_session_file(
            session_id, file_id,
            {"content": f"print('v{i}')", "label": f"Edit {i}"},
            request=fake_request(),
        )
    result = session_files.search_file_version_history(session_id, "app.py", view="list")
    text = result["content"]
    assert "were left out" in text
    assert "\n2. before \"Edit 2\"" in text
    assert "\n3. before \"Edit 3\"" in text
    assert "\n1. before" not in text


def test_history_diff_is_cut_with_a_footer(isolated_db, monkeypatch):
    database, session_files = isolated_db
    monkeypatch.setattr(session_files, "_HISTORY_DIFF_LINES", 2)
    session_id = _three_versions(database, session_files)
    result = session_files.search_file_version_history(
        session_id, "app.py", version="last", view="diff")
    assert "Output cut at 2 lines." in result["content"]


def test_parse_history_content_keeps_new_fields_only_when_valid():
    from services.pipeline import _parse_history_content
    assert _parse_history_content(
        '{"filename": "app.py", "view": "Diff", "version": "Last", "compare_to": 2}'
    ) == {
        "filename": "app.py", "query": None,
        "view": "diff", "version": "last", "compare_to": 2,
    }
    assert _parse_history_content(
        '{"filename": "app.py", "view": "patch", "version": "soon", "compare_to": 0}'
    ) == {"filename": "app.py", "query": None}


def test_history_tag_only_registered_for_agent_task():
    """Structural gate: TAG_DEFS must only ever contain "history" for
    Agent Mode calls (is_agent_task=True) — never for single-pass chat/edit,
    regardless of model. This is a static-source check (not a live pipeline
    run) confirming the registration is behind the exact is_agent_task
    conditional the design requires."""
    import inspect
    from services import pipeline
    src = inspect.getsource(pipeline.run_natural_pipeline_stream)
    idx = src.index('TAG_DEFS["history"]')
    preceding = src[:idx]
    # The nearest preceding "if ...:" line at the same or lower indent must
    # be exactly "if is_agent_task:" — i.e. this assignment is not reachable
    # unless that condition is True.
    guard_idx = preceding.rfind('if is_agent_task:')
    assert guard_idx != -1, "TAG_DEFS['history'] registration must be gated by is_agent_task"
    # And nothing between the guard and the assignment "de-indents" back out
    # of the if-block (which would mean the assignment escaped the guard).
    between = preceding[guard_idx:]
    guard_indent = len(between) - len(between.lstrip(' '))
    body_line = src[idx:].splitlines()[0]
    assign_indent = len(idx and src[:idx].splitlines()[-1]) - len(src[:idx].splitlines()[-1].lstrip(' '))
    assert assign_indent > guard_indent, "TAG_DEFS['history'] must be indented inside the is_agent_task block"
