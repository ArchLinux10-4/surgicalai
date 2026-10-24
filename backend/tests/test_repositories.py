"""Open Folder: local repositories, sandbox, apply-to-disk, and agent context."""
import os
import subprocess
import sys

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(path):
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "app.py").write_text("print('one')\n", encoding="utf-8")
    _git(path, "add", "app.py")
    _git(path, "commit", "-m", "init")
    _git(path, "remote", "add", "origin", "git@github.com:acme/app.git")
    return path


@pytest.fixture()
def env(monkeypatch, tmp_path):
    import database as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "repos.db")
    db.init_db()

    from routers import chat as chat_router
    from routers import repositories as repo_router

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.user_id = request.headers.get("X-Test-User", "user-a")
        request.state.is_admin = True
        return await call_next(request)

    app.include_router(chat_router.router, prefix="/api/chat")
    app.include_router(repo_router.router, prefix="/api/repositories")
    return {"client": TestClient(app), "db": db, "tmp": tmp_path}


def test_remote_parsing():
    from services.repositories import parse_git_remote
    assert parse_git_remote("git@github.com:acme/app.git") == {
        "host": "github.com", "owner": "acme", "repo": "app",
    }
    assert parse_git_remote("https://github.com/acme/app")["repo"] == "app"
    assert parse_git_remote("https://github.com/acme/app.git")["owner"] == "acme"
    assert parse_git_remote("ssh://git@github.com/acme/app.git")["host"] == "github.com"
    enterprise = parse_git_remote("https://git.example.com/acme/app.git")
    assert enterprise["host"] == "git.example.com"
    assert parse_git_remote("not a remote") is None


def test_schema_migration(env):
    db = env["db"]
    with db.get_db_ctx() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        session_cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()}
        file_cols = {row[1] for row in conn.execute("PRAGMA table_info(session_files)").fetchall()}
    assert "repositories" in tables
    assert "repository_id" in session_cols
    assert "repo_path" in file_cols
    assert "base_sha" in file_cols


def test_hosted_routes_404(env, monkeypatch):
    import database as db
    monkeypatch.setattr(db, "USE_POSTGRES", True)
    res = env["client"].get("/api/repositories")
    assert res.status_code == 404


def test_open_is_scoped_and_unique(env):
    client = env["client"]
    folder = _repo(env["tmp"] / "proj")
    link = env["tmp"] / "link"
    link.symlink_to(folder, target_is_directory=True)
    opened = client.post("/api/repositories/open", json={"path": str(folder)})
    assert opened.status_code == 200, opened.text
    again = client.post("/api/repositories/open", json={"path": str(link)})
    assert again.status_code == 200
    assert again.json()["id"] == opened.json()["id"]
    body = opened.json()
    assert body["is_git"] is True
    assert body["git_remote_url"].endswith("acme/app.git")
    assert body["suggested_github"] == {"owner": "acme", "repo": "app"}
    assert body["github_linked"] is False

    other = client.get("/api/repositories", headers={"X-Test-User": "user-b"})
    assert other.status_code == 200
    assert other.json() == []
    denied = client.delete(f"/api/repositories/{body['id']}", headers={"X-Test-User": "user-b"})
    assert denied.status_code == 404
    assert folder.exists()


def test_symlink_escape(env):
    from services.repositories import resolve_inside
    folder = env["tmp"] / "sandbox"
    folder.mkdir()
    outside = env["tmp"] / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    (folder / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException) as raised:
        resolve_inside(str(folder), "escape/secret.txt")
    assert raised.value.status_code == 403


def test_legacy_workspace_registers_custom_path_only(env):
    db = env["db"]
    client = env["client"]
    home = os.path.realpath(os.path.expanduser("~"))
    db.set_setting("workspace_path", home)
    assert client.get("/api/repositories").json() == []
    folder = _repo(env["tmp"] / "legacy")
    db.set_setting("workspace_path", str(folder))
    listed = client.get("/api/repositories").json()
    assert len(listed) == 1
    assert listed[0]["root_path"] == os.path.realpath(str(folder))


def test_session_inherits_github_link(env):
    client = env["client"]
    folder = _repo(env["tmp"] / "linked")
    opened = client.post("/api/repositories/open", json={"path": str(folder)}).json()
    patched = client.patch(f"/api/repositories/{opened['id']}", json={
        "github_owner": "acme", "github_repo": "app",
    })
    assert patched.status_code == 200, patched.text
    created = client.post("/api/chat/sessions", json={
        "title": "In repo", "repository_id": opened["id"],
    })
    assert created.status_code == 200, created.text
    assert created.json()["repository_id"] == opened["id"]
    listed = client.get("/api/chat/sessions").json()
    match = next(s for s in listed if s["id"] == created.json()["id"])
    assert match["repository_id"] == opened["id"]
    assert match["github_owner"] == "acme"
    assert match["github_repo"] == "app"
    assert match["github_branch"]


def test_remove_keeps_files_on_disk(env):
    client = env["client"]
    folder = _repo(env["tmp"] / "keep")
    opened = client.post("/api/repositories/open", json={"path": str(folder)}).json()
    res = client.delete(f"/api/repositories/{opened['id']}")
    assert res.status_code == 200
    assert (folder / "app.py").read_text(encoding="utf-8") == "print('one')\n"
    assert client.get("/api/repositories").json() == []


def test_environment_block_only_for_scoped_local_chats(env, monkeypatch):
    import database as db
    from routers.chat import _load_effective_memory
    from services.environment_context import environment_block
    client = env["client"]
    plain = client.post("/api/chat/sessions", json={"title": "Loose"}).json()
    with db.get_db_ctx() as conn:
        assert _load_effective_memory(conn, plain["id"]) is None
        assert environment_block(conn, plain["id"]) == ""
    folder = _repo(env["tmp"] / "envrepo")
    opened = client.post("/api/repositories/open", json={"path": str(folder)}).json()
    scoped = client.post("/api/chat/sessions", json={
        "title": "Scoped", "repository_id": opened["id"],
    }).json()
    with db.get_db_ctx() as conn:
        text = _load_effective_memory(conn, scoped["id"])
    assert text.startswith("ENVIRONMENT")
    assert os.path.realpath(str(folder)) in text
    assert "cannot run shell commands" in text
    assert "Current branch:" in text
    monkeypatch.setattr(db, "USE_POSTGRES", True)
    assert environment_block(None, scoped["id"]) == ""


def test_stage_apply_drift_undo_and_auto_apply(env):
    from services.repo_apply import apply_files, content_sha, maybe_auto_apply, sync_undo_to_disk
    client = env["client"]
    db = env["db"]
    folder = _repo(env["tmp"] / "applyrepo")
    opened = client.post("/api/repositories/open", json={"path": str(folder)}).json()
    session = client.post("/api/chat/sessions", json={
        "title": "Apply", "repository_id": opened["id"],
    }).json()
    staged = client.post(f"/api/repositories/{opened['id']}/stage", json={
        "session_id": session["id"], "path": "app.py",
    })
    assert staged.status_code == 200, staged.text
    file_id = staged.json()["id"]
    original = "print('one')\n"
    edited = "print('two')\n"
    with db.get_db_ctx() as conn:
        conn.execute("UPDATE session_files SET content = ? WHERE id = ?", (edited, file_id))
        conn.commit()
    result = apply_files(session["id"], "user-a", [file_id], force=False)
    assert result["applied"] and not result["conflicts"]
    assert (folder / "app.py").read_text(encoding="utf-8") == edited
    with db.get_db_ctx() as conn:
        sha = conn.execute("SELECT base_sha FROM session_files WHERE id = ?", (file_id,)).fetchone()["base_sha"]
    assert sha == content_sha(edited)
    assert list((folder / ".surgicalai_backups").glob("app.py.*.bak"))

    (folder / "app.py").write_text("print('disk')\n", encoding="utf-8")
    with db.get_db_ctx() as conn:
        conn.execute("UPDATE session_files SET content = ? WHERE id = ?", ("print('four')\n", file_id))
        conn.commit()
    drifted = apply_files(session["id"], "user-a", [file_id], force=False)
    assert drifted["conflicts"][0]["reason"] == "drift"
    assert (folder / "app.py").read_text(encoding="utf-8") == "print('disk')\n"
    forced = apply_files(session["id"], "user-a", [file_id], force=True)
    assert forced["applied"]
    assert (folder / "app.py").read_text(encoding="utf-8") == "print('four')\n"

    replaced = "print('four')\n"
    restored = "print('two')\n"
    with db.get_db_ctx() as conn:
        conn.execute(
            "UPDATE session_files SET content = ?, base_sha = ? WHERE id = ?",
            (restored, content_sha(replaced), file_id),
        )
        conn.commit()
    assert sync_undo_to_disk(session["id"], file_id, replaced) == "written"
    assert (folder / "app.py").read_text(encoding="utf-8") == restored

    (folder / "app.py").write_text(restored, encoding="utf-8")
    with db.get_db_ctx() as conn:
        conn.execute(
            "UPDATE repositories SET auto_apply = 1 WHERE id = ?",
            (opened["id"],),
        )
        conn.execute(
            "UPDATE session_files SET content = ?, base_sha = ? WHERE id = ?",
            ("print('auto')\n", content_sha(restored), file_id),
        )
        conn.commit()
    auto = maybe_auto_apply(session["id"], file_id)
    assert auto["ran"] is True
    assert (folder / "app.py").read_text(encoding="utf-8") == "print('auto')\n"


def test_repo_tools_are_opt_in():
    from services.grok_agent_tools import TOOL_REQUEST_REPO, build_grok_agent_tools
    from services.repo_tools import execute_repo_tool, prompt_section
    off = {t["function"]["name"] for t in build_grok_agent_tools("ask")}
    on = {t["function"]["name"] for t in build_grok_agent_tools("edit", repo_enabled=True)}
    assert TOOL_REQUEST_REPO not in off
    assert TOOL_REQUEST_REPO in on
    assert "write_surgical_edit" not in {t["function"]["name"] for t in build_grok_agent_tools("ask", repo_enabled=True)}
    text = prompt_section({"name": "demo", "root_path": "/tmp/demo"})
    assert "<repo_request>" in text
    assert "cannot run shell commands" in text
    assert "no open folder" in execute_repo_tool("", "", {"tool": "git_status"}).lower() or \
        "no open folder" in execute_repo_tool("missing", "u", {"tool": "git_status"}).lower()
