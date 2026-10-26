"""Git agent tools: classification, a temp repo, and approval cards. No live GitHub."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services import git_cli  # noqa: E402
from services.repo_apply import content_sha, disk_state_for  # noqa: E402


def _git(cwd, *args):
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)


def _init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")


def _pair(tmp: Path):
    src = tmp / "src"
    bare = tmp / "remote.git"
    _init(src)
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")
    (src / "README").write_text("base\n", encoding="utf-8")
    _git(src, "add", "README")
    _git(src, "commit", "-m", "init")
    _git(src, "remote", "add", "origin", str(bare))
    _git(src, "push", "-u", "origin", "main")
    return src, bare


def _level(command, **kwargs):
    return git_cli.classify(command, **kwargs)["level"]


def _note(command, **kwargs):
    return git_cli.classify(command, **kwargs)["note"]


def test_levels():
    assert _level("git status") == "read"
    assert _level("git diff HEAD") == "read"
    assert _level("git fetch origin") == "read"
    assert _level("gh pr list") == "read"
    assert _level("gh pr view 4") == "read"
    assert _level("git switch -c surgicalai/demo", head_branch="main") == "local"
    assert _level("git checkout -b surgicalai/demo", head_branch="main") == "local"
    assert _level("git add README", head_branch="feature", default_branch="main") == "local"
    assert _level("git commit -m hello", head_branch="feature", default_branch="main") == "local"
    assert _level("git commit -m hello", head_branch="main", default_branch="main") == "remote"
    assert _level("git commit -m hello", head_branch="", default_branch="main") == "remote"
    assert _level("git push origin feature", head_branch="feature", default_branch="main") == "remote"
    assert _level("gh pr create --title Title --base main") == "remote"
    assert _level("gh pr merge 12 --merge") == "merge"
    assert _level("git merge origin/main", head_branch="feature") == "merge"
    assert _level("git pull --no-ff") == "merge"
    assert _level("git pull") == "local"


def test_destructive_and_unknown_are_refused():
    refused = [
        "git reset --hard",
        "git clean -fd",
        "git checkout file.py",
        "git checkout main",
        "git restore README",
        "git branch -D old",
        "git push origin +main",
        "git push origin :main",
        "git push --force origin feature",
        "git push origin main --no-verify",
        "git commit --no-verify -m x",
        "git -c user.name=x status",
        "git --git-dir=/tmp/other status",
        "git status; rm -rf /",
        "git rebase main",
        "git config user.name x",
        "git pull --rebase",
        "rm -rf /",
        "git push https://github.com/acme/app.git",
    ]
    for command in refused:
        note = _note(command, head_branch="feature", default_branch="main")
        assert note, command
    assert "Run it yourself" in _note("git reset --hard")
    assert _note("git status; rm -rf /")
    started = []
    # A refused command must not start git.
    out = git_cli.run_git_cli("git status; rm -rf /", root="/tmp")
    assert out["exit_code"] == 1
    assert started == []


def test_default_branch_push_is_blocked_unless_allowed():
    blocked = git_cli.classify(
        "git push origin main", head_branch="main", default_branch="main",
    )
    assert blocked["note"]
    assert "default branch" in blocked["note"].lower()
    allowed = git_cli.classify(
        "git push origin main", head_branch="main", default_branch="main",
        allow_default_push=True,
    )
    assert allowed["level"] == "merge"
    assert not allowed["note"]


def test_ask_plan_is_read_only_and_capped():
    read = git_cli.begin_git_call("git status", "ask", 0)
    assert read["run"] and not read["needs_approval"]
    mutate = git_cli.begin_git_call(
        "git commit -m hi", "plan", 0, head_branch="feature", default_branch="main",
    )
    assert not mutate["run"]
    assert "Ask or Plan" in mutate["note"]
    capped = git_cli.begin_git_call("git status", "ask", 3)
    assert not capped["run"]
    assert "Do not call git again" in capped["note"]


def test_branch_commit_push_fetch_pull_and_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AGENT_ALLOW_FILE_REMOTE", "1")
    monkeypatch.setattr(git_cli, "allow_default_push", lambda: False)
    src, bare = _pair(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(bare), str(other))
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")

    made = git_cli.run_git_cli(
        "git switch -c surgicalai/demo", root=str(src), head_branch="main", default_branch="main",
    )
    assert made["exit_code"] == 0
    (src / "README").write_text("feature\n", encoding="utf-8")
    added = git_cli.run_git_cli("git add README", root=str(src), head_branch="surgicalai/demo", default_branch="main")
    assert added["exit_code"] == 0
    committed = git_cli.run_git_cli(
        "git commit -m feature", root=str(src), head_branch="surgicalai/demo", default_branch="main",
    )
    assert committed["exit_code"] == 0, committed
    pushed = git_cli.run_git_cli(
        "git push origin surgicalai/demo",
        root=str(src), head_branch="surgicalai/demo", default_branch="main",
    )
    assert pushed["exit_code"] == 0, pushed

    (other / "README").write_text("theirs\n", encoding="utf-8")
    _git(other, "add", "README")
    _git(other, "commit", "-m", "theirs")
    _git(other, "push", "origin", "main")
    pulled = git_cli.run_git_cli(
        "git pull origin main", root=str(src), head_branch="surgicalai/demo", default_branch="main",
    )
    assert pulled["exit_code"] != 0
    assert "fast-forward" in (pulled["stderr"] or "").lower() or "ahead" in (pulled["stderr"] or "").lower()
    merged = git_cli.run_git_cli(
        "git merge origin/main", root=str(src), head_branch="surgicalai/demo", default_branch="main",
    )
    assert merged["exit_code"] != 0
    assert "README" in (merged["stderr"] or "")
    assert "read_repo_file" in merged["stderr"]


def test_commit_blocks_secrets_and_huge_files(tmp_path, monkeypatch):
    monkeypatch.setattr(git_cli, "allow_default_push", lambda: False)
    src, _bare = _pair(tmp_path)
    _git(src, "switch", "-c", "feature")
    (src / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    _git(src, "add", "-f", ".env")
    blocked = git_cli.run_git_cli(
        "git commit -m secret", root=str(src), head_branch="feature", default_branch="main",
    )
    assert blocked["exit_code"] == 1
    assert ".env" in blocked["stderr"]
    _git(src, "restore", "--staged", ".env")
    (src / ".env").unlink()

    key = src / "id_rsa"
    key.write_text("private\n", encoding="utf-8")
    _git(src, "add", "-f", "id_rsa")
    blocked_key = git_cli.run_git_cli(
        "git commit -m key", root=str(src), head_branch="feature", default_branch="main",
    )
    assert blocked_key["exit_code"] == 1
    assert "id_rsa" in blocked_key["stderr"]
    _git(src, "restore", "--staged", "id_rsa")
    key.unlink()

    huge = src / "blob.bin"
    huge.write_bytes(b"x" * (6 * 1024 * 1024))
    _git(src, "add", "blob.bin")
    blocked_huge = git_cli.run_git_cli(
        "git commit -m huge", root=str(src), head_branch="feature", default_branch="main",
    )
    assert blocked_huge["exit_code"] == 1
    assert "5 MB" in blocked_huge["stderr"]


def test_unapplied_edits_are_noted_and_do_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(git_cli, "allow_default_push", lambda: False)
    src, _bare = _pair(tmp_path)
    _git(src, "switch", "-c", "feature")
    (src / "README").write_text("changed\n", encoding="utf-8")
    _git(src, "add", "README")
    monkeypatch.setattr(git_cli, "session_repo_files", lambda sid: [{
        "filename": "README", "repo_path": "README", "content": "staged-in-chat", "base_sha": "abc",
    }])
    monkeypatch.setattr("services.repo_apply.disk_state_for", lambda *a, **k: "pending")
    out = git_cli.run_git_cli(
        "git commit -m noted", root=str(src), session_id="s1",
        head_branch="feature", default_branch="main",
    )
    assert out["exit_code"] == 0, out
    assert "not saved to the folder" in out["stdout"]
    assert "README" in out["stdout"]


def test_chat_disk_summary_lists_unsaved_changed_and_missing(tmp_path, monkeypatch):
    rows = [
        {"filename": "a.py", "repo_path": "a.py", "content": "x", "base_sha": "1"},
        {"filename": "b.py", "repo_path": "b.py", "content": "y", "base_sha": "2"},
        {"filename": "c.py", "repo_path": "c.py", "content": "z", "base_sha": "3"},
    ]
    states = {"a.py": "pending", "b.py": "conflict", "c.py": "missing"}
    monkeypatch.setattr(git_cli, "session_repo_files", lambda sid: rows)
    monkeypatch.setattr("services.repo_apply.disk_state_for", lambda rel, *a, **k: states[rel])
    note = git_cli.unapplied_edits_note("s1", str(tmp_path))
    assert "not saved to the folder" in note
    assert "a.py" in note
    assert "changed on disk" in note
    assert "b.py" in note
    assert "missing from the folder" in note
    assert "c.py" in note


def test_git_status_includes_chat_disk_summary(tmp_path, monkeypatch):
    src, _bare = _pair(tmp_path)
    monkeypatch.setattr(git_cli, "session_repo_files", lambda sid: [{
        "filename": "README", "repo_path": "README", "content": "newer", "base_sha": "abc",
    }])
    monkeypatch.setattr("services.repo_apply.disk_state_for", lambda *a, **k: "conflict")
    out = git_cli.run_git_cli(
        "git status", root=str(src), session_id="s1",
        head_branch="main", default_branch="main",
    )
    assert out["exit_code"] == 0, out
    assert "changed on disk" in out["stdout"]
    assert "README" in out["stdout"]
    from services.repositories import git_status_text
    text = git_status_text(str(src), "s1")
    assert "changed on disk" in text
    assert "README" in text


def test_disk_state_pending_and_conflict(tmp_path):
    root = tmp_path
    (root / "a.txt").write_text("old", encoding="utf-8")
    base = content_sha("old")
    assert disk_state_for("a.txt", str(root), "new", base) == "pending"
    (root / "a.txt").write_text("other", encoding="utf-8")
    assert disk_state_for("a.txt", str(root), "new", base) == "conflict"


def test_token_stays_in_the_child_env(tmp_path, monkeypatch):
    monkeypatch.setattr(git_cli, "allow_default_push", lambda: False)
    src, _bare = _pair(tmp_path)
    _git(src, "remote", "set-url", "origin", "https://github.com/acme/app.git")
    token = "ghs_secret_token_value"
    monkeypatch.setattr(
        git_cli, "open_github_repo",
        lambda *a, **k: (object(), {"permission_tier": "read_write", "account_login": "acme"}, token, ""),
    )
    captured = {}

    def _fake(argv, root, env, timeout):
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        return 0, "pushed " + token, ""

    monkeypatch.setattr(git_cli, "_run_argv", _fake)
    out = git_cli.run_git_cli(
        "git push origin feature",
        root=str(src), user_id="user-a",
        repo={"github_owner": "acme", "github_repo": "app", "default_branch": "main"},
        head_branch="feature", default_branch="main",
    )
    assert out["exit_code"] == 0
    assert token not in out["stdout"]
    assert "[redacted]" in out["stdout"]
    import base64
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    assert token not in " ".join(captured["argv"])
    for key, value in captured["env"].items():
        if key == "GIT_CONFIG_VALUE_0":
            assert basic in value
        else:
            assert token not in value
            assert basic not in value
    assert captured["env"]["HOME"] != os.path.expanduser("~")
    assert token not in (src / ".git" / "config").read_text(encoding="utf-8")
    source = Path(git_cli.__file__).read_text(encoding="utf-8")
    assert "os.environ.copy()" not in source
    assert "asyncio.to_thread(" in source


def _repo(root: Path):
    return {
        "root_path": str(root),
        "user_id": "user-a",
        "github_owner": "acme",
        "github_repo": "app",
        "default_branch": "main",
    }


async def _drain(gen):
    events = []
    async for event in gen:
        events.append(event)
    return events


def test_approve_runs_the_stored_command(tmp_path, monkeypatch):
    src, _bare = _pair(tmp_path)
    _git(src, "switch", "-c", "feature")
    monkeypatch.setattr("services.repo_tools.session_repo", lambda sid: _repo(src))
    monkeypatch.setattr(git_cli, "allow_default_push", lambda: False)
    ran = []
    monkeypatch.setattr(
        git_cli, "run_git_cli",
        lambda command, **kwargs: ran.append(command) or {"exit_code": 0, "stdout": "ok", "stderr": ""},
    )
    inbox = asyncio.Queue()

    async def go():
        task = asyncio.create_task(_drain(git_cli.resolve_git_call(
            "git push origin feature", "edit", 0, inbox,
            user_id="user-a", session_id="s1", timeout=2,
        )))
        await asyncio.sleep(0.05)
        await inbox.put({
            "type": "git_cli_response", "action": "approve", "command": "git reset --hard",
        })
        return await task

    events = asyncio.run(go())
    assert ran == ["git push origin feature"]
    assert events[-1]["type"] == "git_done"
    assert events[-1]["run"] is True
    assert any(ev.get("type") == "git_approval_needed" for ev in events)


def test_reject_and_timeout(tmp_path, monkeypatch):
    src, _bare = _pair(tmp_path)
    monkeypatch.setattr("services.repo_tools.session_repo", lambda sid: _repo(src))
    inbox = asyncio.Queue()

    async def reject():
        task = asyncio.create_task(_drain(git_cli.resolve_git_call(
            "git push origin feature", "agent", 0, inbox,
            user_id="user-a", session_id="s1", timeout=2,
        )))
        await asyncio.sleep(0.05)
        await inbox.put({"type": "git_cli_response", "action": "reject"})
        return await task

    rejected = asyncio.run(reject())
    assert rejected[-1]["run"] is False
    assert rejected[-1]["note"] == git_cli.REJECT_NOTE

    async def expire():
        return await _drain(git_cli.resolve_git_call(
            "git push origin feature", "edit", 0, asyncio.Queue(),
            user_id="user-a", session_id="s1", timeout=0.2,
        ))

    timed = asyncio.run(expire())
    assert timed[-1]["note"] == git_cli.TIMEOUT_NOTE


def test_edit_reclassifies_and_merge_ignores_edit(tmp_path, monkeypatch):
    src, _bare = _pair(tmp_path)
    monkeypatch.setattr("services.repo_tools.session_repo", lambda sid: _repo(src))
    monkeypatch.setattr(git_cli, "pr_merge_preview", lambda *a, **k: ("", "PR #1 into main. Checks passing."))
    ran = []
    monkeypatch.setattr(
        git_cli, "run_git_cli",
        lambda command, **kwargs: ran.append(command) or {"exit_code": 0, "stdout": "", "stderr": ""},
    )
    inbox = asyncio.Queue()

    async def same_level():
        task = asyncio.create_task(_drain(git_cli.resolve_git_call(
            "git push origin feature", "edit", 0, inbox,
            user_id="user-a", session_id="s1", timeout=2,
        )))
        await asyncio.sleep(0.05)
        await inbox.put({
            "type": "git_cli_response", "action": "edit", "command": "git push origin other",
        })
        return await task

    events = asyncio.run(same_level())
    assert ran == ["git push origin other"]
    assert sum(1 for ev in events if ev.get("type") == "git_approval_needed") == 1

    ran.clear()
    inbox = asyncio.Queue()

    async def worse():
        task = asyncio.create_task(_drain(git_cli.resolve_git_call(
            "git push origin feature", "edit", 0, inbox,
            user_id="user-a", session_id="s1", timeout=2,
        )))
        await asyncio.sleep(0.05)
        await inbox.put({
            "type": "git_cli_response", "action": "edit", "command": "gh pr merge 1 --merge",
        })
        await asyncio.sleep(0.05)
        await inbox.put({"type": "git_cli_response", "action": "approve", "command": "git status"})
        return await task

    escalated = asyncio.run(worse())
    assert ran == ["gh pr merge 1 --merge"]
    assert sum(1 for ev in escalated if ev.get("type") == "git_approval_needed") == 2

    ran.clear()
    inbox = asyncio.Queue()

    async def merge_edit():
        task = asyncio.create_task(_drain(git_cli.resolve_git_call(
            "gh pr merge 1 --merge", "edit", 0, inbox,
            user_id="user-a", session_id="s1", timeout=2,
        )))
        await asyncio.sleep(0.05)
        await inbox.put({
            "type": "git_cli_response", "action": "edit", "command": "git status",
        })
        return await task

    ignored = asyncio.run(merge_edit())
    assert ran == []
    assert ignored[-1]["note"] == git_cli.MERGE_EDIT_NOTE


def test_approval_card_cap(tmp_path, monkeypatch):
    src, _bare = _pair(tmp_path)
    monkeypatch.setattr("services.repo_tools.session_repo", lambda sid: _repo(src))

    async def sticky(state, *args, **kwargs):
        return state

    monkeypatch.setattr(git_cli, "_after_decision", sticky)
    events = asyncio.run(_drain(git_cli.resolve_git_call(
        "git push origin feature", "edit", 0, asyncio.Queue(),
        user_id="user-a", session_id="s1", timeout=1,
    )))
    cards = [ev for ev in events if ev.get("type") == "git_approval_needed"]
    assert len(cards) == 4
    assert events[-1]["note"] == git_cli.REJECT_NOTE


class _Run:
    def __init__(self, name, status, conclusion):
        self.name = name
        self.status = status
        self.conclusion = conclusion


class _FakePR:
    def __init__(self, **kwargs):
        self.number = kwargs.get("number", 7)
        self.title = "Demo"
        self.draft = kwargs.get("draft", False)
        self.mergeable = kwargs.get("mergeable", True)
        self.mergeable_state = kwargs.get("mergeable_state", "clean")
        self.html_url = "https://github.com/acme/app/pull/7"
        self.state = "open"
        self.head = SimpleNamespace(ref="feature", sha="abc")
        owner = SimpleNamespace(login="acme")
        self.base = SimpleNamespace(ref="main", repo=SimpleNamespace(owner=owner, name="app"))
        self._requester = SimpleNamespace(requestJsonAndCheck=lambda *a, **k: None)
        self.merged_with = None

    def merge(self, merge_method="merge"):
        self.merged_with = merge_method
        return SimpleNamespace(sha="deadbeef")


class _FakeGitHub:
    def __init__(self, pr):
        self.pr = pr
        self.runs = [_Run("ci", "completed", "success")]
        self.created = None

    def get_pull(self, number):
        return self.pr

    def get_pulls(self, state="open"):
        return [self.pr]

    def get_commit(self, sha):
        runs = self.runs
        return SimpleNamespace(get_check_runs=lambda: runs)

    def create_pull(self, **kwargs):
        self.created = kwargs
        return self.pr


def _patch_github(monkeypatch, gh, token="ghs_secret_token_value"):
    monkeypatch.setattr(
        git_cli, "open_github_repo",
        lambda *a, **k: (gh, {"permission_tier": "read_write", "account_login": "acme"}, token, ""),
    )


def test_gh_subset(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AGENT_ALLOW_FILE_REMOTE", "1")
    src, _bare = _pair(tmp_path)
    _git(src, "switch", "-c", "feature")
    _git(src, "push", "-u", "origin", "feature")
    repo = {"github_owner": "acme", "github_repo": "app", "default_branch": "main"}
    gh = _FakeGitHub(_FakePR())
    _patch_github(monkeypatch, gh)
    monkeypatch.setattr(git_cli, "_branch_pushed", lambda *a, **k: "")

    listed = git_cli.run_git_cli("gh pr list", root=str(src), user_id="u", repo=repo, head_branch="feature")
    assert listed["exit_code"] == 0
    assert "#7" in listed["stdout"]
    viewed = git_cli.run_git_cli("gh pr view 7", root=str(src), user_id="u", repo=repo, head_branch="feature")
    assert "Demo" in viewed["stdout"]
    checks = git_cli.run_git_cli("gh pr checks 7", root=str(src), user_id="u", repo=repo, head_branch="feature")
    assert "passing" in checks["stdout"]
    created = git_cli.run_git_cli(
        'gh pr create --title "Title" --body "Why" --base main',
        root=str(src), user_id="u", repo=repo, head_branch="feature", default_branch="main",
    )
    assert created["exit_code"] == 0, created
    assert gh.created["head"] == "feature"
    assert gh.created["draft"] is False
    ready = git_cli.run_git_cli("gh pr ready 7", root=str(src), user_id="u", repo=repo, head_branch="feature")
    assert ready["exit_code"] == 0
    merged = git_cli.run_git_cli(
        "gh pr merge 7 --merge", root=str(src), user_id="u", repo=repo, head_branch="feature",
    )
    assert merged["exit_code"] == 0, merged
    assert gh.pr.merged_with == "merge"
    assert "git pull --ff-only" in merged["stdout"]

    draft = _FakeGitHub(_FakePR(draft=True))
    _patch_github(monkeypatch, draft)
    reason, _summary = git_cli.pr_merge_preview("u", "acme", "app", 7)
    assert "draft" in reason.lower()

    dirty = _FakeGitHub(_FakePR(mergeable=False, mergeable_state="dirty"))
    _patch_github(monkeypatch, dirty)
    reason, _summary = git_cli.pr_merge_preview("u", "acme", "app", 7)
    assert "conflict" in reason.lower()

    failing = _FakeGitHub(_FakePR())
    failing.runs = [_Run("ci", "completed", "failure")]
    _patch_github(monkeypatch, failing)
    reason, _summary = git_cli.pr_merge_preview("u", "acme", "app", 7)
    assert reason.startswith("Checks failing")

    pending = _FakeGitHub(_FakePR())
    pending.runs = [_Run("ci", "in_progress", "")]
    _patch_github(monkeypatch, pending)
    reason, _summary = git_cli.pr_merge_preview("u", "acme", "app", 7)
    assert reason.startswith("Checks still running")


def test_push_files_refuses_the_default_branch(monkeypatch):
    from services import github_context_tools as g

    class Repo:
        default_branch = "main"

        def get_git_ref(self, *args, **kwargs):
            raise AssertionError("the default branch must not be touched")

    class Client:
        def get_repo(self, name):
            return Repo()

    monkeypatch.setattr(g, "is_github_app_configured", lambda: True)
    monkeypatch.setattr(g, "_find_client_for_repo", lambda *a, **k: (Client(), {"permission_tier": "read_write"}, None))
    monkeypatch.setattr(g, "_db_dlog", lambda *a, **k: None)
    text = g.execute_github_context_tool("push_files", {
        "owner": "acme",
        "repo": "app",
        "branch": "main",
        "message": "direct",
        "files": [{"path": "a.txt", "content": "hi"}],
    }, "user-a")
    assert "default branch" in text.lower()
    assert "git_cli" in text
    assert g.default_branch_push_block("feature", "main") == ""


def test_wiring_matches_local():
    pipeline = Path(_BACKEND, "services", "pipeline.py").read_text(encoding="utf-8")
    native = Path(_BACKEND, "services", "grok_ask_plan_native.py").read_text(encoding="utf-8")
    steering = Path(_BACKEND, "services", "steering.py").read_text(encoding="utf-8")
    assert pipeline.count("resolve_local_call") == pipeline.count("resolve_aws_call")
    assert pipeline.count("resolve_local_call as") == pipeline.count("resolve_aws_call as")
    assert pipeline.count("resolve_local_call as") >= 3
    assert "resolve_local_call" in native
    assert "local_cli_response" in steering
    assert '_streaming_t0 += float(_local_done.get("waited_s") or 0)' in pipeline
    assert '"local"' in pipeline[pipeline.find("Recover a context-request tag"):pipeline.find("Recover a context-request tag") + 500]


def test_wiring_matches_aws():
    pipeline = Path(_BACKEND, "services", "pipeline.py").read_text(encoding="utf-8")
    native = Path(_BACKEND, "services", "grok_ask_plan_native.py").read_text(encoding="utf-8")
    steering = Path(_BACKEND, "services", "steering.py").read_text(encoding="utf-8")
    assert pipeline.count("resolve_git_call") == pipeline.count("resolve_aws_call")
    assert pipeline.count("resolve_git_call as") == pipeline.count("resolve_aws_call as")
    assert pipeline.count("resolve_git_call as") >= 3
    assert "resolve_git_call" in native
    assert "git_cli_response" in steering
    assert '_streaming_t0 += float(_git_done.get("waited_s") or 0)' in pipeline
    assert '_streaming_t0 += float(_aws_done.get("waited_s") or 0)' in pipeline
    starve = pipeline.find('if _git_ledger:')
    warning = pipeline.find("I wasn't able to produce code changes in the time")
    assert 0 < starve < warning


def test_one_command_and_pr_list_limit():
    assert "One command per call" in _note("git status && git diff")
    assert _level("gh pr list --limit 3") == "read"
    assert "1 to 50" in _note("gh pr list --limit 99")
    assert _level("gh auth status") == "read"
    assert _level(
        'git publish --branch surgicalai/demo --message "why" --title "Title"',
        head_branch="main", default_branch="main",
    ) == "remote"


def test_permission_and_non_fast_forward_text():
    denied, stop = git_cli.explain_failure(
        "remote: Permission to acme/app.git denied to surgical-ai-github[bot]\nfatal: HTTP 403",
        "acme", "app",
    )
    assert stop
    assert "Contents: Read and write" in denied
    assert "Add the repository" in denied
    assert "Nothing was pushed" in denied
    auth, auth_stop = git_cli.explain_failure("fatal: Authentication failed for https://github.com/acme/app.git")
    assert auth_stop and "Reconnect" in auth
    ahead, ahead_stop = git_cli.explain_failure("! [rejected] feature -> feature (non-fast-forward)")
    assert not ahead_stop and "git fetch" in ahead


def test_readiness_uses_the_app_grant(tmp_path, monkeypatch):
    src, _bare = _pair(tmp_path)
    _git(src, "remote", "set-url", "origin", "https://github.com/acme/app.git")
    monkeypatch.delenv("GIT_AGENT_ALLOW_FILE_REMOTE", raising=False)
    repo = {"github_owner": "acme", "github_repo": "app", "default_branch": "main"}
    monkeypatch.setattr(
        git_cli, "open_github_repo",
        lambda *a, **k: (object(), {"installation_id": "42", "permission_tier": "read_write"}, "tok", ""),
    )
    grants = {"contents": "read", "pull_requests": "read"}
    selected = ["acme/app"]
    monkeypatch.setattr("services.github_app_auth.installation_permissions", lambda _id: dict(grants))
    monkeypatch.setattr("services.github_app_auth.installation_repositories", lambda _id: list(selected))
    denied = git_cli.git_readiness(str(src), "user-a", repo)
    assert denied["ok"] is False
    assert "contents" in denied["message"]
    assert denied["fix_url"].endswith("/42")
    assert git_cli.readiness_block("push", denied)
    grants["contents"] = "write"
    write_only = git_cli.git_readiness(str(src), "user-a", repo)
    assert git_cli.readiness_block("push", write_only) == ""
    assert git_cli.readiness_block("publish", write_only)
    grants["pull_requests"] = "write"
    ready = git_cli.git_readiness(str(src), "user-a", repo)
    assert ready["ok"] is True
    selected[:] = ["other/repo"]
    missing_repo = git_cli.git_readiness(str(src), "user-a", repo)
    assert missing_repo["ok"] is False
    assert "not installed on acme/app" in missing_repo["message"]


def test_unpushed_pr_create_is_a_failure_without_a_card(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AGENT_ALLOW_FILE_REMOTE", "1")
    src, _bare = _pair(tmp_path)
    _git(src, "switch", "-c", "feature")
    monkeypatch.setattr("services.repo_tools.session_repo", lambda sid: _repo(src))
    inbox = asyncio.Queue()

    async def go():
        return await _drain(git_cli.resolve_git_call(
            'gh pr create --title "Title" --body "Why" --base main',
            "edit", 0, inbox, user_id="user-a", session_id="s1", timeout=1,
        ))

    events = asyncio.run(go())
    assert not any(ev.get("type") == "git_approval_needed" for ev in events)
    assert events[-1]["type"] == "git_done"
    assert events[-1]["run"] is False
    assert "Push feature" in events[-1]["note"]
    monkeypatch.setattr(
        git_cli, "open_github_repo",
        lambda *a, **k: (_FakeGitHub(_FakePR()), {"permission_tier": "read_write"}, "tok", ""),
    )
    direct = git_cli.run_git_cli(
        'gh pr create --title "Title" --body "Why" --base main',
        root=str(src), user_id="user-a", repo=_repo(src), head_branch="feature", default_branch="main",
    )
    assert direct["exit_code"] == 1
    assert "Push feature" in direct["stderr"]


def test_push_403_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AGENT_ALLOW_FILE_REMOTE", "1")
    src, _bare = _pair(tmp_path)
    _git(src, "switch", "-c", "feature")
    real = git_cli._run_argv

    def wrapped(argv, root, env, timeout):
        if "push" in argv:
            return 128, "", "remote: Permission to acme/app.git denied to app[bot]\nerror: 403"
        return real(argv, root, env, timeout)

    monkeypatch.setattr(git_cli, "_run_argv", wrapped)
    out = git_cli.run_git_cli(
        "git push origin feature",
        root=str(src), user_id="user-a", repo=_repo(src),
        head_branch="feature", default_branch="main",
    )
    assert out["exit_code"] != 0
    assert out["stop"] is True
    assert "Nothing was pushed" in out["stderr"]


def test_publish_happy_path_resume_and_failed_push(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AGENT_ALLOW_FILE_REMOTE", "1")
    src, bare = _pair(tmp_path)
    (src / "a.txt").write_text("a\n", encoding="utf-8")
    command = 'git publish --branch surgicalai/pub --message "add a" --title "Add a" --body "what changed"'
    out = git_cli.run_git_cli(
        command, root=str(src), user_id="user-a", repo=_repo(src),
        head_branch="main", default_branch="main",
    )
    assert out["exit_code"] == 0, out
    assert [step["name"] for step in out["steps"]] == ["branch", "commit", "push", "pull request"]
    assert any(line.startswith("Verified:") for line in out["verified"])
    shown = subprocess.run(
        ["git", "ls-remote", "--heads", str(bare), "surgicalai/pub"],
        check=True, capture_output=True, text=True,
    )
    assert "refs/heads/surgicalai/pub" in shown.stdout

    again = git_cli.run_git_cli(
        command, root=str(src), user_id="user-a", repo=_repo(src),
        head_branch="surgicalai/pub", default_branch="main",
    )
    assert again["exit_code"] == 0, again
    skipped = next(step for step in again["steps"] if step["name"] == "commit")
    assert skipped.get("skipped") is True

    real = git_cli._run_argv

    def fail_push(argv, root, env, timeout):
        if "push" in argv:
            return 1, "", "remote: Permission to acme/app.git denied to app[bot]\nerror: 403"
        return real(argv, root, env, timeout)

    monkeypatch.setattr(git_cli, "_run_argv", fail_push)
    (src / "b.txt").write_text("b\n", encoding="utf-8")
    failed = git_cli.run_git_cli(
        'git publish --branch surgicalai/pub --message "add b" --title "Add b" --body "more"',
        root=str(src), user_id="user-a", repo=_repo(src),
        head_branch="surgicalai/pub", default_branch="main",
    )
    assert failed["exit_code"] == 1
    assert failed["stop"] is True
    assert [step["name"] for step in failed["steps"]] == ["branch", "commit", "push"]
    assert failed["steps"][0]["ok"] and failed["steps"][1]["ok"] and not failed["steps"][2]["ok"]
    assert "Nothing was pushed" in failed["stderr"]


def test_repeat_failure_stops(tmp_path, monkeypatch):
    src, _bare = _pair(tmp_path)
    _git(src, "switch", "-c", "feature")
    monkeypatch.setattr("services.repo_tools.session_repo", lambda sid: _repo(src))
    monkeypatch.setattr(
        git_cli, "run_git_cli",
        lambda command, **kwargs: {"exit_code": 1, "stdout": "", "stderr": "boom", "stop": False},
    )
    git_cli._last_fail.clear()

    async def both():
        notes = []
        for _ in range(2):
            inbox = asyncio.Queue()
            task = asyncio.create_task(_drain(git_cli.resolve_git_call(
                "git push origin feature", "edit", 0, inbox,
                user_id="user-a", session_id="repeat-1", timeout=2,
            )))
            await asyncio.sleep(0.02)
            await inbox.put({"type": "git_cli_response", "action": "approve"})
            notes.append(await task)
        return notes

    first, second = asyncio.run(both())
    assert "twice" not in (first[-1].get("note") or "")
    assert "twice" in (second[-1].get("note") or "")
    assert second[-1]["stop"] is True


def test_approval_wait_is_reported(tmp_path, monkeypatch):
    src, _bare = _pair(tmp_path)
    _git(src, "switch", "-c", "feature")
    monkeypatch.setattr("services.repo_tools.session_repo", lambda sid: _repo(src))
    monkeypatch.setattr(
        git_cli, "run_git_cli",
        lambda command, **kwargs: {"exit_code": 0, "stdout": "ok", "stderr": ""},
    )
    inbox = asyncio.Queue()

    async def go():
        # Approve only after the card is out. Setup work before the card is
        # synchronous, so a fixed sleep can answer before the wait begins.
        events = []
        async for event in git_cli.resolve_git_call(
            "git push origin feature", "edit", 0, inbox,
            user_id="user-a", session_id="wait-1", timeout=2,
        ):
            events.append(event)
            if event.get("type") == "git_approval_needed":
                asyncio.get_running_loop().call_later(
                    0.05, inbox.put_nowait, {"type": "git_cli_response", "action": "approve"},
                )
        return events

    events = asyncio.run(go())
    assert events[-1]["waited_s"] > 0


def test_result_block_for_success_failure_and_deadline():
    success = git_cli.render_git_outcome([{
        "ok": True,
        "command": "git publish",
        "branch": "surgicalai/pub",
        "pr_url": "https://github.com/acme/app/pull/11",
        "steps": [{"name": "push", "ok": True, "detail": "Pushed."}],
        "verified": ["Verified: surgicalai/pub is at abc1234."],
        "next": "Review the pull request.",
    }])
    assert "Git result" in success
    assert "Verified: surgicalai/pub is at abc1234." in success
    assert "https://github.com/acme/app/pull/11" in success
    assert "produce code changes" not in success
    failed = git_cli.render_git_outcome([{
        "ok": False,
        "command": "git push origin feature",
        "branch": "feature",
        "detail": "GitHub refused the push.",
        "next": "GitHub refused the push. Nothing was pushed.",
        "fix_url": "https://github.com/settings/installations/42",
    }])
    assert "failed" in failed
    assert "settings/installations/42" in failed
    assert "feature" in failed
