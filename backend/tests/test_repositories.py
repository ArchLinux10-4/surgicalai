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
    assert "git_cli" in text
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


def test_list_directories_uses_browse_root(env, monkeypatch):
    root = env["tmp"] / "mac-home"
    root.mkdir()
    (root / "projects").mkdir()
    monkeypatch.setenv("SURGICALAI_BROWSE_ROOT", str(root))
    res = env["client"].get("/api/repositories/fs")
    assert res.status_code == 200
    body = res.json()
    assert body["path"] == os.path.realpath(str(root))
    assert body["entries"] == [{
        "name": "projects",
        "path": os.path.realpath(str(root / "projects")),
    }]

    other = env["tmp"] / "other"
    other.mkdir()
    chosen = env["client"].get("/api/repositories/fs", params={"path": str(other)})
    assert chosen.status_code == 200
    assert chosen.json()["path"] == os.path.realpath(str(other))


def _diff_repo(path):
    """Two commits plus one dirty line, with origin/main on the first commit."""
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "app.py").write_text("print('base')\n", encoding="utf-8")
    _git(path, "add", "app.py")
    _git(path, "commit", "-m", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(path, "update-ref", "refs/remotes/origin/main", base)
    (path / "app.py").write_text("print('committed')\n", encoding="utf-8")
    _git(path, "add", "app.py")
    _git(path, "commit", "-m", "second")
    (path / "app.py").write_text("print('dirty')\n", encoding="utf-8")
    return path


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
    assert "cannot run a shell" in text
    assert "no open folder" in execute_repo_tool("", "", {"tool": "git_status"}).lower() or \
        "no open folder" in execute_repo_tool("missing", "u", {"tool": "git_status"}).lower()


def test_git_diff_default_splits_committed_and_uncommitted(tmp_path):
    from services.repositories import git_diff_text
    repo = _diff_repo(tmp_path / "repo")
    text = git_diff_text(str(repo))
    assert "origin/main" in text.splitlines()[0]
    assert "not fetched" in text.lower()
    committed, _, uncommitted = text.partition("\n\n")
    assert "app.py" in committed and "1 insertion" in committed and "1 deletion" in committed
    assert "app.py" in uncommitted
    assert "Uncommitted changes versus HEAD" in uncommitted


def test_git_diff_falls_back_to_main_when_origin_is_missing(tmp_path):
    from services.repositories import git_diff_text
    repo = _diff_repo(tmp_path / "repo")
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    text = git_diff_text(str(repo))
    assert "split from main " in text
    assert "origin/main" not in text


def test_git_diff_says_when_no_base_exists(tmp_path):
    from services.repositories import git_diff_text
    repo = _diff_repo(tmp_path / "repo")
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    _git(repo, "branch", "-m", "main", "feature")
    text = git_diff_text(str(repo))
    assert "No base ref found" in text
    assert "Uncommitted changes versus HEAD" in text


def test_git_diff_worktree_patch_and_stat(tmp_path):
    from services.repositories import git_diff_text
    repo = _diff_repo(tmp_path / "repo")
    patch = git_diff_text(str(repo), scope="worktree", view="patch", path="app.py")
    assert "print('dirty')" in patch and "print('committed')" in patch
    stat = git_diff_text(str(repo), scope="worktree", view="stat")
    assert "app.py" in stat and "Uncommitted changes versus HEAD (stat)" in stat


def test_git_diff_staged_and_unstaged_stay_separate(tmp_path):
    from services.repositories import git_diff_text
    repo = _diff_repo(tmp_path / "repo")
    (repo / "other.py").write_text("print('staged')\n", encoding="utf-8")
    _git(repo, "add", "other.py")
    unstaged = git_diff_text(str(repo), scope="unstaged")
    staged = git_diff_text(str(repo), scope="staged")
    assert "app.py" in unstaged and "other.py" not in unstaged
    assert "other.py" in staged and "app.py" not in staged


def test_git_diff_range_shows_only_what_is_between(tmp_path):
    from services.repositories import git_diff_text
    repo = _diff_repo(tmp_path / "repo")
    text = git_diff_text(str(repo), scope="range", base="origin/main", head="HEAD")
    assert "origin/main...HEAD" in text
    assert "app.py" in text
    assert "No uncommitted" not in text
    two = git_diff_text(str(repo), scope="range", base="HEAD", head="HEAD", dots="two")
    assert "HEAD..HEAD" in two
    assert "No changes in that range." in two


def test_git_diff_rejects_a_path_outside_the_folder(tmp_path):
    from services.repositories import git_diff_text
    repo = _diff_repo(tmp_path / "repo")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    for bad in ("../elsewhere/secret.txt", str(outside / "secret.txt")):
        text = git_diff_text(str(repo), path=bad)
        assert "Access denied" in text
        assert "secret" not in text
        assert str(outside) not in text


def test_git_diff_rejects_bad_refs_without_running_git(tmp_path, monkeypatch):
    import services.repositories as repositories
    repo = _diff_repo(tmp_path / "repo")
    calls = []
    real = repositories._git_raw

    def spy(root, args, timeout=20):
        calls.append(args)
        return real(root, args, timeout)

    monkeypatch.setattr(repositories, "_git_raw", spy)
    for bad in ("-p", "origin/main;id", "a b", "main\n--output=/tmp/x"):
        calls.clear()
        text = repositories.git_diff_text(str(repo), scope="range", base=bad)
        assert "not a valid git ref" in text or "cannot have spaces" in text
        assert calls == []


def test_git_diff_non_git_folder_and_detached_head(tmp_path):
    from services.repositories import git_diff_text
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_diff_text(str(plain)) == "This folder is not a git repository."

    repo = _diff_repo(tmp_path / "repo")
    _git(repo, "checkout", "--detach", "HEAD")
    text = git_diff_text(str(repo))
    assert "origin/main" in text
    assert "failed" not in text.lower()


def test_git_diff_caps_a_long_patch(tmp_path, monkeypatch):
    import services.repositories as repositories
    repo = _diff_repo(tmp_path / "repo")
    monkeypatch.setattr(repositories, "_PATCH_LINE_CAP", 3)
    monkeypatch.setattr(repositories, "_DIFF_LINE_CAP", 3)
    text = repositories.git_diff_text(str(repo), scope="worktree", view="patch", path="app.py")
    assert "Output cut at 3 lines." in text
    assert "Narrow with path" in text


def test_git_diff_reports_a_binary_file(tmp_path):
    from services.repositories import git_diff_text
    repo = _diff_repo(tmp_path / "repo")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    stat = git_diff_text(str(repo), scope="unstaged", view="stat")
    assert "Bin" in stat and "blob.bin" in stat
    patch = git_diff_text(str(repo), scope="unstaged", view="patch", path="blob.bin")
    assert "Binary files" in patch and "differ" in patch
    assert "\x00" not in patch


def test_git_diff_needs_an_open_folder():
    from services.repo_tools import execute_repo_tool
    text = execute_repo_tool("missing", "u", {"tool": "git_diff"})
    assert "no open folder" in text.lower()


def test_parse_repo_request_keeps_diff_fields_and_drops_unknown_ones():
    from services.repo_tools import parse_repo_request
    parsed = parse_repo_request(
        '{"tool":"git_diff","scope":"range","base":"origin/main","head":"HEAD",'
        '"view":"patch","dots":"two","path":"a.py","extra":"ignored"}'
    )
    assert parsed == {
        "tool": "git_diff", "path": "a.py", "terms": [],
        "scope": "range", "base": "origin/main", "head": "HEAD",
        "view": "patch", "dots": "two",
    }
    other = parse_repo_request('{"tool":"git_status","scope":"range"}')
    assert other == {"tool": "git_status", "path": "", "terms": []}
    bad = parse_repo_request('{"tool":"rm"}')
    assert "git_diff" in bad["_error"]
