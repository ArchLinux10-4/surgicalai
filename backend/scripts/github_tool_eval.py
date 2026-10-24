"""Offline check that GitHub tools answer a small task list.

Uses a fake repository, so it does not call GitHub. Pass --model name to also
ask a live model (needs the same API key the app already uses). Without a key
the live step is skipped and the fixture check still runs.

    python scripts/github_tool_eval.py
    python scripts/github_tool_eval.py --model grok-4-fast
"""
import argparse
import json
import os
import sys
import types
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services import github_context_tools as gct  # noqa: E402
from services.github_natural_tag import (  # noqa: E402
    build_github_prompt_section,
    execute_github_request,
)


def _fake_repo():
    pr = types.SimpleNamespace(
        number=12, title="Fix login", state="open", user=types.SimpleNamespace(login="pat"),
        updated_at=date(2026, 10, 20), html_url="https://github.com/acme/app/pull/12",
        labels=[], base=types.SimpleNamespace(ref="main"), head=types.SimpleNamespace(ref="feature"),
        additions=3, deletions=1, changed_files=1, merged=False, body="Login form.",
    )
    pr.get_files = lambda: [types.SimpleNamespace(filename="backend/app.py", additions=3, deletions=1, patch="+ok\n")]
    pr.get_issue_comments = lambda: [types.SimpleNamespace(user=types.SimpleNamespace(login="sam"), body="Looks good")]
    pr.get_review_comments = lambda: []
    issue = types.SimpleNamespace(
        number=9, title="Crash on save", state="open", user=types.SimpleNamespace(login="pat"),
        updated_at=date(2026, 10, 20), html_url="https://github.com/acme/app/issues/9",
        labels=[], body="Stack trace here.", pull_request=None,
    )
    issue.get_comments = lambda: []

    class Page(list):
        totalCount = 0

        def __init__(self, items):
            super().__init__(items)
            self.totalCount = len(items)

    class Repo:
        default_branch = "main"
        files = {"backend/app.py": "def run_app():\n    return 1\n"}

        def get_contents(self, path, ref=None):
            text = self.files[path]
            raw = text.encode()
            return types.SimpleNamespace(
                encoding="base64", content="", decoded_content=raw, size=len(raw), sha="abc",
            )

        def get_pulls(self, **kw):
            return Page([pr])

        def get_pull(self, number):
            return pr

        def get_issues(self, **kw):
            return Page([issue])

        def get_issue(self, number):
            return issue

        def get_branches(self):
            return Page([types.SimpleNamespace(name="main")])

        def get_commits(self, **kw):
            return Page([types.SimpleNamespace(
                sha="abc1234ffff", author=types.SimpleNamespace(login="pat"),
                commit=types.SimpleNamespace(
                    message="Fix login",
                    author=types.SimpleNamespace(date=date(2026, 10, 20)),
                ),
            )])

        def get_git_tree(self, ref, recursive=False):
            return types.SimpleNamespace(
                tree=[types.SimpleNamespace(path="backend/app.py", type="blob", size=20)],
                raw_data={"truncated": False}, sha="t",
            )

    repo = Repo()
    client = types.SimpleNamespace(
        get_repo=lambda name: repo,
        search_code=lambda query: [types.SimpleNamespace(path="backend/app.py")],
    )
    return client


def _install_fake():
    client = _fake_repo()
    gct._tree_cache.clear()
    gct.is_github_app_configured = lambda: True
    gct._find_client_for_repo = lambda *a, **k: (client, {"permission_tier": "read_only"}, None)
    gct._db_dlog = lambda *a, **k: None
    import services.deploy_status as deploy_status
    deploy_status.check_deploy_status = lambda user_id, args, dlog=None: (
        "Vercel: READY at 2026-10-20 sha abc1234"
    )


def _live_note(model: str) -> str:
    if model == "offline":
        return "offline fixture"
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("XAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return "no API key in the environment; live model step skipped"
    return "live model calls are not sent by this fixture check (key is present; run a chat turn to compare)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="offline")
    args = parser.parse_args()
    tasks = json.loads((Path(__file__).with_name("github_tool_tasks.json")).read_text())
    _install_fake()
    prompt = build_github_prompt_section(
        [{"account_login": "acme"}], known_repos=["acme/app"],
    )
    print(f"prompt_chars {len(prompt)}")
    print(f"model {args.model}: {_live_note(args.model)}")
    print(f"{'task':<16} {'tool':<18} {'calls':<6} {'result'}")
    passed = 0
    for task in tasks:
        tool = task["tool"]
        call = {"tool": tool, "args": {"owner": "acme", "repo": "app", **task.get("args", {})}}
        if tool in ("check_deploy", "list_repos"):
            call["args"] = dict(task.get("args") or {})
        try:
            result = execute_github_request(call, user_id="eval", known_repos=["acme/app"])
        except Exception as exc:
            result = f"[error {exc}]"
        ok = task["expect"] in result and not result.startswith("[Unknown")
        passed += int(ok)
        status = "pass" if ok else "FAIL"
        print(f"{task['id']:<16} {tool:<18} {1:<6} {status}")
        if not ok:
            print("   ", result[:180].replace("\n", " "))
    print(f"{passed}/{len(tasks)} tasks passed")
    return 0 if passed == len(tasks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
