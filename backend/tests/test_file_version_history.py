"""
Tests for real, browsable file version history (session_file_versions).

Bug this fixes (proven with real logs + code, see conversation history):
The Settings panel claimed edits were "always reversible" via
`.surgicalai_backups/`, but that disk-backup mechanism only exists for the
legacy SurgicalPanel/on-disk `/api/surgical/apply` path
(services/surgical_editor.py `_backup_file`) and has zero frontend UI to
browse or restore it (`listBackups` in client.ts was dead code). The actual
in-app Undo button used by the main chat flow (InlineDiffCard /
MobileDiffCard) was backed by a single `previous_content` DB column that
gets overwritten on every edit — i.e. one level of undo, not "every
change", and it was artificially gated to `totalApplied === 1` in
InlineDiffCard specifically because of that single-slot limitation.

Fix: a real, append-only `session_file_versions` table snapshots the prior
content on every edit/undo/restore, with list + restore-by-id endpoints, so
a file can be brought back to ANY past state — making "always reversible"
literally true.

These tests exercise the router functions directly against a real
(temp, isolated) sqlite DB — no FastAPI TestClient needed since the router
functions are plain, synchronous functions.
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
    """Point database.py at a fresh temp sqlite DB for this test only."""
    tmp_home = tempfile.mkdtemp(prefix="surgicalai_test_home_")
    monkeypatch.setenv("HOME", tmp_home)
    # database module reads Path.home() at import time into module-level
    # constants, so it must be imported fresh AFTER HOME is patched.
    #
    # NOTE: deleting "routers.session_files" from sys.modules alone is NOT
    # enough — Python's import system also sets `session_files` as an
    # attribute directly on the parent `routers` package object, and
    # `from routers import session_files` will happily return that stale
    # attribute (bound to the previous test's `database` module) via
    # getattr() without consulting sys.modules again. The `routers` package
    # itself must be dropped too so the whole chain re-binds to the fresh
    # `database` module.
    for mod in list(sys.modules):
        if (mod == "database" or mod == "routers" or mod.startswith("routers.")
                or mod == "services.session_auth"):
            del sys.modules[mod]
    import database as _database
    _database.init_db()
    from routers import session_files as _session_files
    yield _database, _session_files
    shutil.rmtree(tmp_home, ignore_errors=True)


def _seed_file(database, session_id: str, file_id: str, filename: str, content: str):
    # Ownership gates require a chat_sessions row; user_id NULL matches the
    # legacy list_sessions visibility rule (any authed user may access).
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


def test_update_snapshots_previous_content_as_a_version(isolated_db):
    """Every update_session_file call must snapshot the PRIOR content into
    session_file_versions before overwriting — this is the core mechanism
    that makes multi-step history possible (not just a single slot)."""
    database, session_files = isolated_db
    session_id, file_id = "s1", "f1"
    _seed_file(database, session_id, file_id, "app.py", "print('v1')")

    session_files.update_session_file(session_id, file_id, {"content": "print('v2')", "label": "Edit A"}, request=fake_request())
    session_files.update_session_file(session_id, file_id, {"content": "print('v3')", "label": "Edit B"}, request=fake_request())

    versions = session_files.list_session_file_versions(session_id, file_id, request=fake_request())
    # Two edits => two snapshots of the states they replaced (v1, v2)
    assert len(versions) == 2, f"expected 2 snapshots, got {len(versions)}: {versions}"
    labels = [v["label"] for v in versions]
    assert "Edit A" in labels and "Edit B" in labels

    with database.get_db_ctx() as conn:
        row = conn.execute("SELECT content FROM session_files WHERE id = ?", (file_id,)).fetchone()
        assert row["content"] == "print('v3')"


def test_undo_restores_most_recent_version_regardless_of_change_count(isolated_db):
    """This directly disproves the old totalApplied === 1 assumption: undo
    must correctly restore the last saved state even after MANY edits have
    been applied to the file, not just when exactly one has."""
    database, session_files = isolated_db
    session_id, file_id = "s2", "f2"
    _seed_file(database, session_id, file_id, "app.py", "print('v1')")

    session_files.update_session_file(session_id, file_id, {"content": "print('v2')"}, request=fake_request())
    session_files.update_session_file(session_id, file_id, {"content": "print('v3')"}, request=fake_request())
    session_files.update_session_file(session_id, file_id, {"content": "print('v4')"}, request=fake_request())

    result = session_files.undo_session_file(session_id, file_id, request=fake_request())
    assert result["content"] == "print('v3')", "undo must restore the immediately-prior state (v3), not v1 or a stale slot"

    with database.get_db_ctx() as conn:
        row = conn.execute("SELECT content FROM session_files WHERE id = ?", (file_id,)).fetchone()
        assert row["content"] == "print('v3')"


def test_undo_with_no_history_returns_400_not_a_crash(isolated_db):
    """A brand-new, never-edited file has no version history — undo must
    fail cleanly (400) rather than crashing or silently no-op'ing."""
    database, session_files = isolated_db
    session_id, file_id = "s3", "f3"
    _seed_file(database, session_id, file_id, "fresh.py", "print('only version')")

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        session_files.undo_session_file(session_id, file_id, request=fake_request())
    assert exc_info.value.status_code == 400


def test_restore_arbitrary_past_version_not_just_the_last_one(isolated_db):
    """The core "always reversible" claim: a user must be able to jump back
    to ANY past version, not only the single most recent one."""
    database, session_files = isolated_db
    session_id, file_id = "s4", "f4"
    _seed_file(database, session_id, file_id, "app.py", "print('v1')")

    session_files.update_session_file(session_id, file_id, {"content": "print('v2')", "label": "Edit A"}, request=fake_request())
    session_files.update_session_file(session_id, file_id, {"content": "print('v3')", "label": "Edit B"}, request=fake_request())
    session_files.update_session_file(session_id, file_id, {"content": "print('v4')", "label": "Edit C"}, request=fake_request())

    versions = session_files.list_session_file_versions(session_id, file_id, request=fake_request())
    # newest first: Edit C (snapshot of v3), Edit B (snapshot of v2), Edit A (snapshot of v1)
    edit_a_version = next(v for v in versions if v["label"] == "Edit A")

    result = session_files.restore_session_file_version(session_id, file_id, edit_a_version["id"], request=fake_request())
    assert result["content"] == "print('v1')", "must be able to restore all the way back to the original content, not just one step"

    with database.get_db_ctx() as conn:
        row = conn.execute("SELECT content FROM session_files WHERE id = ?", (file_id,)).fetchone()
        assert row["content"] == "print('v1')"


def test_restore_is_itself_reversible(isolated_db):
    """Restoring to an old version must not be a destructive dead-end — the
    state it replaces (the most recent content) must be snapshotted too, so
    the user can restore forward again if the restore was a mistake."""
    database, session_files = isolated_db
    session_id, file_id = "s5", "f5"
    _seed_file(database, session_id, file_id, "app.py", "print('v1')")

    session_files.update_session_file(session_id, file_id, {"content": "print('v2')", "label": "Edit A"}, request=fake_request())
    versions = session_files.list_session_file_versions(session_id, file_id, request=fake_request())
    v1_version = versions[0]

    session_files.restore_session_file_version(session_id, file_id, v1_version["id"], request=fake_request())
    # Current content is now back to v1. v2 (what we just replaced) must
    # have been snapshotted so it's not lost.
    versions_after = session_files.list_session_file_versions(session_id, file_id, request=fake_request())
    contents = set()
    for v in versions_after:
        # fetch each version's content directly to check v2 survived
        with database.get_db_ctx() as conn:
            row = conn.execute("SELECT content FROM session_file_versions WHERE id = ?", (v["id"],)).fetchone()
            contents.add(row["content"])
    assert "print('v2')" in contents, "the content being replaced by a restore must be preserved as its own version"


def test_versions_list_ordered_newest_first(isolated_db):
    database, session_files = isolated_db
    session_id, file_id = "s6", "f6"
    _seed_file(database, session_id, file_id, "app.py", "print('v1')")

    session_files.update_session_file(session_id, file_id, {"content": "print('v2')", "label": "First"}, request=fake_request())
    session_files.update_session_file(session_id, file_id, {"content": "print('v3')", "label": "Second"}, request=fake_request())

    versions = session_files.list_session_file_versions(session_id, file_id, request=fake_request())
    assert versions[0]["label"] == "Second", "most recently created snapshot must be first"
    assert versions[1]["label"] == "First"


def test_versions_endpoint_404s_for_unknown_file(isolated_db):
    database, session_files = isolated_db
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        session_files.list_session_file_versions("no-such-session", "no-such-file", request=fake_request())
    assert exc_info.value.status_code == 404
