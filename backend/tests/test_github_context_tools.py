"""GitHub tool results and errors, with a fake repository and no network."""
import base64
import types
from datetime import date

import pytest

from services import github_context_tools as gct


class FakeUser:
    def __init__(self, login):
        self.login = login


class FakeContents:
    def __init__(self, text):
        raw = text.encode()
        self.encoding = "base64"
        self.content = base64.b64encode(raw).decode()
        self.decoded_content = raw
        self.size = len(raw)
        self.sha = "abc123"


class Page:
    def __init__(self, items, total=None):
        self._items = list(items)
        self.totalCount = len(self._items) if total is None else total

    def __iter__(self):
        return iter(self._items)


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class FakeRepo:
    default_branch = "main"
    private = False
    full_name = "acme/app"

    def __init__(self, files=None, prs=None, issues=None, branches=None, commits=None):
        self.files = files or {}
        self.prs = prs or []
        self.issues = issues or []
        self.branches = branches or []
        self.commits = commits or []
        self.tree_paths = list(self.files)

    def get_contents(self, path, ref=None):
        if path not in self.files:
            raise GitHubError(404, "404 {'message': 'Not Found'}")
        return FakeContents(self.files[path])

    def get_pulls(self, state="open", **kw):
        return Page(self.prs, total=getattr(self, "pr_total", None) or len(self.prs))

    def get_pull(self, number):
        for pr in self.prs:
            if pr.number == int(number):
                return pr
        raise GitHubError(404, "404 {'message': 'Not Found'}")

    def get_issues(self, state="open", **kw):
        return Page(self.issues)

    def get_issue(self, number):
        for issue in self.issues:
            if issue.number == int(number):
                return issue
        raise GitHubError(404, "404 {'message': 'Not Found'}")

    def get_branches(self):
        return Page(self.branches)

    def get_commits(self, **kw):
        self.last_commit_kwargs = kw
        if kw.get("sha") == "missing":
            raise GitHubError(404, "404 {'message': 'Branch not found'}")
        return Page(self.commits)

    def get_git_tree(self, ref, recursive=False):
        entries = []
        for path in self.tree_paths:
            entries.append(types.SimpleNamespace(path=path, type="blob", size=10))
        return types.SimpleNamespace(tree=entries, raw_data={"truncated": False}, sha="tree")

    def compare(self, base, head):
        files = [
            types.SimpleNamespace(filename="a.py", additions=40, deletions=5, patch="@@ -1 +1 @@\n-old\n+new\n"),
        ]
        return types.SimpleNamespace(ahead_by=1, behind_by=0, files=files)


def _pr(**kw):
    data = dict(
        number=12, title="Fix login", state="open", user=FakeUser("pat"),
        updated_at=date(2026, 10, 20), html_url="https://github.com/acme/app/pull/12",
        labels=[types.SimpleNamespace(name="bug")],
        base=types.SimpleNamespace(ref="main"),
        head=types.SimpleNamespace(ref="feature"),
        additions=40, deletions=5, changed_files=3, merged=False,
        body="Login form rejected valid passwords.",
        pull_request=None,
    )
    data.update(kw)
    pr = types.SimpleNamespace(**data)

    def get_files():
        patch = kw.get("patch", "@@ -1 +1 @@\n-old\n+new\n")
        return [types.SimpleNamespace(filename="a.py", additions=40, deletions=5, patch=patch)]

    def get_issue_comments():
        return [types.SimpleNamespace(user=FakeUser("pat"), body="Looks good")]

    def get_review_comments():
        return [types.SimpleNamespace(user=FakeUser("sam"), body="nit", path="a.py", line=4)]

    pr.get_files = get_files
    pr.get_issue_comments = get_issue_comments
    pr.get_review_comments = get_review_comments
    return pr


def _issue(**kw):
    data = dict(
        number=9, title="Crash on save", state="open", user=FakeUser("pat"),
        updated_at=date(2026, 10, 20), html_url="https://github.com/acme/app/issues/9",
        labels=[types.SimpleNamespace(name="bug")], body="Stack trace here.",
        pull_request=None,
    )
    data.update(kw)
    issue = types.SimpleNamespace(**data)
    issue.get_comments = lambda: [types.SimpleNamespace(user=FakeUser("sam"), body="I can reproduce")]
    return issue


@pytest.fixture
def fake_repo(monkeypatch):
    repo = FakeRepo(
        files={"a.py": "def hi():\n    return 1\n"},
        prs=[_pr()],
        issues=[_issue(), _issue(number=10, title="A pull request", pull_request={"url": "x"})],
        branches=[types.SimpleNamespace(name="main"), types.SimpleNamespace(name="feature")],
        commits=[types.SimpleNamespace(
            sha="abc1234ffff",
            author=FakeUser("pat"),
            commit=types.SimpleNamespace(
                message="Fix login\n\nmore",
                author=types.SimpleNamespace(date=date(2026, 10, 20)),
            ),
        )],
    )
    client = types.SimpleNamespace(get_repo=lambda name: repo, search_code=lambda query: [])
    monkeypatch.setattr(gct, "is_github_app_configured", lambda: True)
    monkeypatch.setattr(
        gct, "_find_client_for_repo",
        lambda user_id, owner, repo_name, dlog: (client, {"permission_tier": "read_only"}, None),
    )
    monkeypatch.setattr(gct, "_db_dlog", lambda *a, **k: None)
    gct._tree_cache.clear()
    return repo


def _run(tool, args, repo=None):
    return gct.execute_github_context_tool(tool, {"owner": "acme", "repo": "app", **args}, "u1")


def test_read_file_numbers_lines_and_keeps_the_header(fake_repo):
    result = _run("read_file", {"path": "a.py"})
    assert "showing lines 1-2" in result
    assert "    1| def hi():" in result
    assert not result.startswith("[")


def test_read_file_footer_names_the_next_start_line(fake_repo):
    fake_repo.files["big.py"] = "\n".join(f"line {i} {'x' * 80}" for i in range(1, 400))
    result = _run("read_file", {"path": "big.py"})
    assert "showing lines 1-" in result
    assert "TRUNCATED" in result
    assert "start_line=" in result
    assert '"path": "big.py"' in result
    assert not result.startswith("[")


def test_list_prs_shape_limit_and_footer(fake_repo):
    fake_repo.prs = [_pr(number=n, title=f"PR {n}") for n in range(1, 13)]
    fake_repo.pr_total = 37
    result = _run("list_prs", {"state": "open", "limit": 10})
    assert result.startswith("#1 [open] PR 1")
    assert "by @pat" in result
    assert "updated 2026-10-20" in result
    assert "labels: bug" in result
    assert "https://github.com/acme/app/pull/12" in result
    assert 'Showing 10 of 37. Call again with "limit":' in result
    assert not result.startswith("[")


def test_list_prs_empty_tells_the_model_what_to_try(fake_repo):
    fake_repo.prs = []
    result = _run("list_prs", {})
    assert 'No open pull requests. Call list_prs with "state": "all"' in result


def test_list_issues_skips_pull_requests_before_the_limit(fake_repo):
    result = _run("list_issues", {"limit": 10})
    assert "#9 [open] Crash on save" in result
    assert "A pull request" not in result


def test_get_pr_and_diff_lead_with_a_summary(fake_repo):
    detail = _run("get_pr", {"pr_number": 12})
    assert 'PR #12 [open] "Fix login" by @pat, main <- feature, +40/-5 in 3 files, updated 2026-10-20' in detail
    assert "Body:" in detail
    assert "Link: https://github.com/acme/app/pull/12" in detail
    diff = _run("get_pr_diff", {"pr_number": 12})
    assert diff.startswith("PR #12: Fix login")
    assert "1 file changed, +40/-5" in diff
    assert "Files:" in diff
    assert "[patch truncated]" not in diff


def test_long_patch_is_marked_truncated(fake_repo):
    fake_repo.prs = [_pr(patch="+" + ("x" * 5000))]
    diff = _run("get_pr_diff", {"pr_number": 12})
    assert "[patch truncated]" in diff


def test_get_issue_and_comments(fake_repo):
    detail = _run("get_issue", {"issue_number": 9})
    assert 'Issue #9 [open] "Crash on save" by @pat, labels: bug, updated 2026-10-20' in detail
    assert "Body:" in detail
    comments = _run("get_issue_comments", {"issue_number": 9})
    assert "sam: I can reproduce" in comments


def test_comment_cap_reports_older_comments(fake_repo):
    comments = [types.SimpleNamespace(user=FakeUser("sam"), body=f"note {i}") for i in range(42)]
    fake_repo.issues[0].get_comments = lambda: comments
    result = _run("get_issue_comments", {"issue_number": 9})
    assert "(12 older comments not shown)" in result
    assert "note 41" in result
    assert "note 0" not in result


def test_list_branches_marks_the_default(fake_repo):
    result = _run("list_branches", {})
    assert "main (default)" in result
    assert "feature" in result
    assert "protected" not in result.lower()


def test_list_commits_line(fake_repo):
    result = _run("list_commits", {"branch": "main", "limit": 5})
    assert "abc1234 2026-10-20 @pat: Fix login" in result
    assert fake_repo.last_commit_kwargs["sha"] == "main"


def test_diff_branches_summary_comes_first(fake_repo):
    result = _run("diff_branches", {"base": "main", "head": "feature"})
    assert "Comparing main...feature: 1 ahead, 0 behind" in result
    assert "1 file changed, +40/-5" in result
    assert result.index("Files:") < result.index("--- a.py")


def test_missing_argument_includes_an_example(fake_repo):
    result = _run("read_file", {})
    assert result.startswith("[")
    assert "read_file needs 'path'" in result
    assert "Example:" in result


def test_unknown_tool_suggests_the_closest_name():
    result = gct.gh_error("unknown_tool", name="list_pull_requests")
    assert result.startswith("[Unknown GitHub tool 'list_pull_requests'")
    assert "Did you mean 'list_prs'" in result or "Did you mean 'list_" in result
    assert "<github_request>" in result


def test_error_catalogue_phrases():
    assert "could not be parsed" in gct.gh_error("bad_json", reason="not valid JSON")
    assert "ONE JSON object" in gct.gh_error("bad_json", reason="not valid JSON")
    assert "needs 'query'" in gct.gh_error("missing_arg", tool="search_code", arg="query")
    blocked = gct.gh_error(
        "repo_inaccessible", owner="nope", repo="missing", repos=["acme/app", "acme/web"],
    )
    assert "Repos you CAN use: acme/app, acme/web" in blocked
    assert "Use one of these exactly" in blocked
    assert "rate limit" in gct.gh_error("rate_limited").lower()
    assert "Do not retry" in gct.gh_error("rate_limited")
    assert "reconnect GitHub in Settings" in gct.gh_error("auth")
    generic = gct.gh_error("generic", tool="get_pr", message="boom {raw}")
    assert "{" not in generic
    assert "get_pr" in generic


def test_not_found_translations(fake_repo):
    missing_pr = _run("get_pr", {"pr_number": 99})
    assert "PR #99 was not found in acme/app" in missing_pr
    assert 'list_prs (state "all")' in missing_pr
    fake_repo.tree_paths = ["src/app.py", "src/util.py"]
    missing_file = _run("read_file", {"path": "src/ap.py"})
    assert "'src/ap.py' does not exist on main" in missing_file
    assert 'path_prefix "src"' in missing_file
    assert "src/app.py" in missing_file
    missing_branch = _run("list_commits", {"branch": "missing"})
    assert "Branch 'missing' not found" in missing_branch
    assert "list_branches" in missing_branch


def test_rate_limit_and_auth_are_not_raw_exceptions(fake_repo):
    def _pulls(**kw):
        raise GitHubError(429, "rate limit exceeded")
    fake_repo.get_pulls = _pulls
    assert "Do not retry" in _run("list_prs", {})

    def _issues(**kw):
        raise GitHubError(401, "bad credentials")
    fake_repo.get_issues = _issues
    assert "reconnect GitHub in Settings" in _run("list_issues", {})


def test_inaccessible_repo_lists_real_repos(monkeypatch):
    monkeypatch.setattr(gct, "is_github_app_configured", lambda: True)
    monkeypatch.setattr(gct, "list_github_app_installations", lambda user_id: [{"installation_id": 1}])

    class Client:
        def get_repo(self, name):
            raise GitHubError(404, "404 not found")

    monkeypatch.setattr(gct, "get_installation_client", lambda installation_id: Client())
    monkeypatch.setattr(
        "services.github_natural_tag._list_repos",
        lambda user_id, dlog: "acme/app  (default branch: main, private)\nacme/web  (default branch: main, public)\n",
    )
    result = gct.execute_github_context_tool("list_prs", {"owner": "nope", "repo": "missing"}, "u1")
    assert "Repos you CAN use: acme/app, acme/web" in result


def test_registered_session_file_is_not_line_numbered(fake_repo, monkeypatch):
    raw = "def hi():\n    return 1\n"
    fake_repo.files["src/a.py"] = raw
    client = types.SimpleNamespace(get_repo=lambda name: fake_repo)
    monkeypatch.setattr(
        gct, "_find_client_for_repo",
        lambda user_id, owner, repo_name, dlog: (client, {"installation_id": 7, "permission_tier": "read_only"}, None),
    )
    stored = {}

    class Conn:
        def execute(self, sql, params=()):
            stored["sql"] = sql
            stored["params"] = params
            return self

        def fetchone(self):
            return None

        def commit(self):
            return None

    class Ctx:
        def __enter__(self):
            return Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("database.get_db_ctx", lambda: Ctx())
    from services.github_natural_tag import fetch_and_register_github_file
    entry = fetch_and_register_github_file(
        {"tool": "read_file", "args": {"owner": "acme", "repo": "app", "path": "src/a.py"}},
        "u1", "s1",
    )
    assert entry["content"] == raw
    assert "    1|" not in entry["content"]
    shown = _run("read_file", {"path": "src/a.py"})
    assert "    1| def hi():" in shown
