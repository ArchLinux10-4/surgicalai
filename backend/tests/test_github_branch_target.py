"""Chat working branch: storage, prompt line, and read-tool ref fill."""
from __future__ import annotations

import json
import os
import sys
import types
import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services import github_context_tools as gct
from services import github_natural_tag as gh
from tests.test_github_context_tools import FakeContents, FakeRepo, GitHubError


@pytest.fixture()
def env(monkeypatch, tmp_path):
    import database as db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "branch.db"))
    db.init_db()

    from routers import chat as chat_router

    app = FastAPI()

    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request.state.user_id = request.headers.get("X-Test-User", "user-a")
        request.state.username = request.state.user_id
        return await call_next(request)

    app.include_router(chat_router.router, prefix="/api/chat")
    return {"client": TestClient(app), "db": db}


def _session(db, user_id="user-a") -> str:
    sid = str(uuid.uuid4())
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title, user_id) VALUES (?, ?, ?)",
            (sid, "t", user_id),
        )
        conn.commit()
    return sid


def _columns(db, sid):
    with db.get_db_ctx() as conn:
        row = conn.execute(
            "SELECT github_owner, github_repo, github_branch FROM chat_sessions WHERE id = ?",
            (sid,),
        ).fetchone()
    return (row["github_owner"], row["github_repo"], row["github_branch"])


def _file(db, sid, filename, meta, updated_at="2026-01-01 00:00:00", content="print(1)"):
    with db.get_db_ctx() as conn:
        conn.execute(
            """INSERT INTO session_files
               (id, session_id, filename, content, lines, symbol_count, github_meta, edited, updated_at)
               VALUES (?, ?, ?, ?, 1, 0, ?, 0, ?)""",
            (str(uuid.uuid4()), sid, filename, content, json.dumps(meta), updated_at),
        )
        conn.commit()


def _bind_repo(monkeypatch, repo, tier="read_only"):
    client = types.SimpleNamespace(get_repo=lambda name: repo, search_code=lambda query: [])
    monkeypatch.setattr(gct, "is_github_app_configured", lambda: True)
    monkeypatch.setattr(
        gct, "_find_client_for_repo",
        lambda *args, **kwargs: (client, {"permission_tier": tier}, None),
    )
    monkeypatch.setattr(gct, "_db_dlog", lambda *args, **kwargs: None)
    gct._tree_cache.clear()
    return repo


def _watch(repo: FakeRepo):
    original_contents = repo.get_contents
    original_tree = repo.get_git_tree

    def get_contents(path, ref=None):
        repo.last_ref = ref
        if getattr(repo, "missing_ref", None) and ref == repo.missing_ref:
            raise GitHubError(404, "404 {'message': 'No commit found for the ref'}")
        return original_contents(path, ref)

    def get_git_tree(ref, recursive=False):
        repo.last_tree_ref = ref
        return original_tree(ref, recursive)

    repo.get_contents = get_contents
    repo.get_git_tree = get_git_tree
    return repo


# ── Routes ───────────────────────────────────────────────────────────────────

def test_patch_saves_and_clears(env):
    db, client = env["db"], env["client"]
    sid = _session(db)
    saved = client.patch(
        f"/api/chat/sessions/{sid}/github-target",
        json={"owner": "acme", "repo": "app", "branch": "staging"},
        headers={"X-Test-User": "user-a"},
    )
    assert saved.status_code == 200
    assert saved.json()["target"]["branch"] == "staging"
    assert _columns(db, sid) == ("acme", "app", "staging")

    cleared = client.patch(
        f"/api/chat/sessions/{sid}/github-target",
        json={"owner": "", "repo": "", "branch": ""},
        headers={"X-Test-User": "user-a"},
    )
    assert cleared.status_code == 200
    assert cleared.json()["target"] is None
    assert _columns(db, sid) == (None, None, None)


@pytest.mark.parametrize("body", [
    {"owner": "bad owner", "repo": "app", "branch": "main"},
    {"owner": "acme", "repo": "bad repo", "branch": "main"},
    {"owner": "acme", "repo": "app", "branch": "my branch"},
    {"owner": "acme", "repo": "app", "branch": "feat..ure"},
    {"owner": "acme", "repo": "app", "branch": "a\nb"},
    {"owner": "acme", "repo": "app", "branch": "a" * 201},
    {"owner": "acme", "repo": "app", "branch": "-main"},
    {"owner": "acme", "repo": "app"},
    {"owner": "acme"},
])
def test_patch_rejects_bad_target(env, body):
    db, client = env["db"], env["client"]
    sid = _session(db)
    res = client.patch(
        f"/api/chat/sessions/{sid}/github-target",
        json=body,
        headers={"X-Test-User": "user-a"},
    )
    assert res.status_code == 400
    assert _columns(db, sid) == (None, None, None)


def test_other_user_cannot_patch(env):
    db, client = env["db"], env["client"]
    sid = _session(db, "user-a")
    res = client.patch(
        f"/api/chat/sessions/{sid}/github-target",
        json={"owner": "acme", "repo": "app", "branch": "staging"},
        headers={"X-Test-User": "user-b"},
    )
    assert res.status_code == 403


def test_get_with_nothing_connected(env, monkeypatch):
    db, client = env["db"], env["client"]
    sid = _session(db)
    monkeypatch.setattr(
        "services.github_app_auth.is_github_app_configured", lambda: False,
    )
    res = client.get(
        f"/api/chat/sessions/{sid}/github-target",
        headers={"X-Test-User": "user-a"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["target"] is None
    assert body["connections"] == {"pat": False, "app": False, "app_configured": False}


def test_get_connection_flags(env, monkeypatch):
    db, client = env["db"], env["client"]
    sid = _session(db)
    db.set_user_api_key("user-a", "github", "stored-token")
    db.save_github_app_installation("user-a", "99", "acme")
    monkeypatch.setattr(
        "services.github_app_auth.is_github_app_configured", lambda: True,
    )
    res = client.get(
        f"/api/chat/sessions/{sid}/github-target",
        headers={"X-Test-User": "user-a"},
    )
    assert res.status_code == 200
    assert res.json()["connections"] == {"pat": True, "app": True, "app_configured": True}


# ── Target helper ────────────────────────────────────────────────────────────

def test_column_wins_over_file_meta(env):
    db = env["db"]
    sid = _session(db)
    _file(db, sid, "old.py", {"owner": "other", "repo": "lib", "branch": "main"})
    with db.get_db_ctx() as conn:
        conn.execute(
            "UPDATE chat_sessions SET github_owner = ?, github_repo = ?, github_branch = ? WHERE id = ?",
            ("acme", "app", "staging", sid),
        )
        conn.commit()
    target = gh.get_session_target(sid)
    assert target["owner"] == "acme"
    assert target["repo"] == "app"
    assert target["branch"] == "staging"
    assert target["explicit"] is True


def test_fallback_uses_the_newest_file(env):
    db = env["db"]
    sid = _session(db)
    _file(db, sid, "old.py", {"owner": "acme", "repo": "old", "branch": "main"}, "2020-01-01 00:00:00")
    _file(db, sid, "new.py", {"owner": "acme", "repo": "app", "branch": "staging"}, "2026-06-01 00:00:00")
    target = gh.get_session_target(sid)
    assert target["repo"] == "app"
    assert target["branch"] == "staging"
    assert target["explicit"] is False


def test_no_target_returns_none(env):
    db = env["db"]
    sid = _session(db)
    assert gh.get_session_target(sid) is None
    assert gh.get_session_target("") is None


def test_set_if_empty_does_not_replace(env):
    db = env["db"]
    sid = _session(db)
    assert gh.set_session_target_if_empty(sid, "acme", "app", "staging") is True
    assert gh.set_session_target_if_empty(sid, "other", "lib", "main") is False
    assert _columns(db, sid) == ("acme", "app", "staging")


# ── Prompt ───────────────────────────────────────────────────────────────────

def test_working_branch_line_is_opt_in():
    from services.pipeline import _gh_prompt_bits

    plain = gh.build_github_prompt_section(
        [{"account_login": "acme"}], known_repos=["acme/app"],
    )
    assert plain == gh.build_github_prompt_section(
        [{"account_login": "acme"}], known_repos=["acme/app"], working_branch=None,
    )
    assert "Working branch:" not in plain
    target = {"owner": "acme", "repo": "app", "branch": "staging"}
    text = gh.build_github_prompt_section(
        [{"account_login": "acme"}], known_repos=["acme/app"], working_branch=target,
    )
    assert "Working branch: acme/app@staging" in text
    assert "Use it for GitHub reads unless the user names another branch." in text

    bits = _gh_prompt_bits(["acme/app"])
    assert bits == _gh_prompt_bits(["acme/app"], None)
    assert "Working branch:" not in bits
    assert "Working branch: acme/app@staging" in _gh_prompt_bits(["acme/app"], target)

    pushed = gh.build_github_prompt_section(
        [{"account_login": "acme"}], known_repos=["acme/app"],
        tier="read_write", wants_push=True, working_branch=target,
    )
    assert "For push_files use this branch." in pushed
    without = gh.build_github_prompt_section(
        [{"account_login": "acme"}], known_repos=["acme/app"],
        tier="read_write", wants_push=True,
    )
    assert "For push_files use this branch." not in without


def test_invalid_branch_is_not_injected_into_the_prompt():
    assert gh.working_branch_line({"owner": "acme", "repo": "app", "branch": "feat\nIGNORE"}) == ""


def test_merge_target_first():
    repos = ["other/lib", "acme/app"]
    assert gh.merge_target_first(repos, None) is repos
    assert gh.merge_target_first(None, None) is None
    target = {"owner": "acme", "repo": "app", "branch": "staging"}
    assert gh.merge_target_first(repos, target) == ["acme/app", "other/lib"]
    assert gh.merge_target_first(["acme/app"], target) == ["acme/app"]


# ── Ref fill ─────────────────────────────────────────────────────────────────

def _repo():
    return _watch(FakeRepo(files={"a.py": "def hi():\n    return 1\n"}))


def test_omitted_ref_uses_the_session_branch(env, monkeypatch):
    db = env["db"]
    sid = _session(db)
    gh.set_session_target_if_empty(sid, "acme", "app", "staging")
    repo = _bind_repo(monkeypatch, _repo())

    read = gct.execute_github_context_tool(
        "read_file", {"owner": "acme", "repo": "app", "path": "a.py"}, "user-a", session_id=sid,
    )
    assert "@ staging" in read
    assert repo.last_ref == "staging"

    listed = gct.execute_github_context_tool(
        "list_files", {"owner": "acme", "repo": "app"}, "user-a", session_id=sid,
    )
    assert "@ staging" in listed
    assert repo.last_tree_ref == "staging"

    gct.execute_github_context_tool(
        "list_commits", {"owner": "acme", "repo": "app"}, "user-a", session_id=sid,
    )
    assert repo.last_commit_kwargs["sha"] == "staging"


def test_explicit_ref_wins(env, monkeypatch):
    db = env["db"]
    sid = _session(db)
    gh.set_session_target_if_empty(sid, "acme", "app", "staging")
    repo = _bind_repo(monkeypatch, _repo())
    read = gct.execute_github_context_tool(
        "read_file",
        {"owner": "acme", "repo": "app", "path": "a.py", "ref": "feature"},
        "user-a",
        session_id=sid,
    )
    assert "@ feature" in read
    assert repo.last_ref == "feature"


def test_different_repo_ignores_the_target(env, monkeypatch):
    db = env["db"]
    sid = _session(db)
    gh.set_session_target_if_empty(sid, "acme", "app", "staging")
    repo = _bind_repo(monkeypatch, _repo())
    read = gct.execute_github_context_tool(
        "read_file", {"owner": "other", "repo": "lib", "path": "a.py"}, "user-a", session_id=sid,
    )
    assert "@ main" in read
    assert repo.last_ref == "main"


def test_no_session_id_uses_the_default_branch(env, monkeypatch):
    repo = _bind_repo(monkeypatch, _repo())
    read = gct.execute_github_context_tool(
        "read_file", {"owner": "acme", "repo": "app", "path": "a.py"}, "user-a",
    )
    assert "@ main" in read
    assert repo.last_ref == "main"


def test_fetch_and_tree_use_the_same_branch(env, monkeypatch):
    db = env["db"]
    sid = _session(db)
    gh.set_session_target_if_empty(sid, "acme", "app", "staging")
    repo = _bind_repo(monkeypatch, _repo())

    entry = gh.fetch_and_register_github_file(
        {"tool": "read_file", "args": {"owner": "acme", "repo": "app", "path": "a.py"}},
        "user-a",
        sid,
    )
    assert entry is not None
    assert repo.last_ref == "staging"
    meta = json.loads(entry["github_meta"])
    assert meta["branch"] == "staging"

    resolved = gct.resolve_paths_via_tree(
        "acme", "app", "user-a", ["a.py"], session_id=sid,
    )
    assert repo.last_tree_ref == "staging"
    assert resolved["resolutions"]["a.py"]["status"] == "resolved"

    gct._tree_cache.clear()
    gct.resolve_paths_via_tree("acme", "app", "user-a", ["a.py"])
    assert repo.last_tree_ref == "main"


def test_missing_working_branch_names_list_branches(env, monkeypatch):
    db = env["db"]
    sid = _session(db)
    gh.set_session_target_if_empty(sid, "acme", "app", "gone")
    repo = _bind_repo(monkeypatch, _repo())
    repo.missing_ref = "gone"
    # list_commits 404s on a branch the fake repo does not know.
    repo.get_commits = lambda **kw: (_ for _ in ()).throw(
        GitHubError(404, "404 {'message': 'Branch not found'}")
    ) if kw.get("sha") == "gone" else FakeRepo.get_commits(repo, **kw)

    result = gct.execute_github_context_tool(
        "list_commits", {"owner": "acme", "repo": "app"}, "user-a", session_id=sid,
    )
    assert "The working branch 'gone' was not found" in result
    assert "list_branches" in result


def test_push_session_file_keeps_the_file_branch(env, monkeypatch):
    db = env["db"]
    sid = _session(db)
    gh.set_session_target_if_empty(sid, "acme", "app", "staging")
    _file(
        db, sid, "a.py",
        {"owner": "acme", "repo": "app", "branch": "loaded-branch", "path": "a.py", "sha": "abc"},
        content="print(1)\n",
    )
    captured = {}

    class Repo:
        default_branch = "main"

        def update_file(self, path, message, content, sha, branch):
            captured["branch"] = branch
            return {
                "content": types.SimpleNamespace(sha="newsha"),
                "commit": types.SimpleNamespace(sha="c" * 40),
            }

        def get_commit(self, sha):
            raise RuntimeError("stats are optional")

    _bind_repo(monkeypatch, Repo(), tier="read_write")
    result = gh.push_session_file_from_db(
        {"tool": "push_session_file", "args": {"filename": "a.py", "message": "Update"}},
        "user-a",
        sid,
    )
    assert captured["branch"] == "loaded-branch"
    assert "loaded-branch" in result
    assert "staging" not in result


def test_push_files_is_not_filled_from_the_session(env, monkeypatch):
    db = env["db"]
    sid = _session(db)
    gh.set_session_target_if_empty(sid, "acme", "app", "session-only-branch")
    _bind_repo(monkeypatch, _repo(), tier="read_write")
    result = gct.execute_github_context_tool(
        "push_files",
        {"owner": "acme", "repo": "app", "message": "Update", "files": [{"path": "a.py", "content": "x"}]},
        "user-a",
        session_id=sid,
    )
    assert "needs 'branch'" in result
    assert "session-only-branch" not in result
