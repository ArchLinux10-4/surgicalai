"""Argument cleanup, paging, and the short GitHub prompt."""
import json

import pytest

from services.github_natural_tag import (
    build_github_prompt_section,
    maybe_resume_read_file,
    message_mentions_deploy,
    message_mentions_push,
    normalize_github_args,
    note_read_file_page,
    parse_github_request,
    shape_github_result,
)


# Before this change the read-only prompt was 6030 characters and 78 lines.
_OLD_PROMPT_CHARS = 6030


def test_shape_github_result_keeps_the_paging_footer():
    footer = "[TRUNCATED — 10 more lines. Call read_file again with start_line=301 to continue.]"
    text = ("x" * 20000) + "\n" + footer
    shaped = shape_github_result(text, 14000)
    assert shaped.endswith(footer)
    assert len(shaped) <= 14000
    assert "Output shortened" in shaped
    assert shape_github_result("short", 14000) == "short"


def test_page_parser_and_resume_differ_by_model():
    progress = {}
    header = "File: a.py @ main (400 lines, 9000 bytes) — showing lines 1-80"
    assert note_read_file_page(header) == 80
    progress["acme|app|a.py|"] = 80
    grok = maybe_resume_read_file(
        {"owner": "acme", "repo": "app", "path": "a.py"}, progress, rewrite=True,
    )
    claude = maybe_resume_read_file(
        {"owner": "acme", "repo": "app", "path": "a.py"}, progress, rewrite=False,
    )
    assert grok["start_line"] == 81
    assert "start_line" not in claude
    already = maybe_resume_read_file(
        {"owner": "acme", "repo": "app", "path": "a.py", "start_line": 81},
        progress, rewrite=True,
    )
    assert already["start_line"] == 81


@pytest.mark.parametrize("tool,args,known,expect", [
    ("list_prs", {"repo": "acme/app"}, None, {"owner": "acme", "repo": "app"}),
    ("list_prs", {"repo": "https://github.com/acme/app"}, None, {"owner": "acme", "repo": "app"}),
    ("list_prs", {"repo": "https://github.com/acme/app.git"}, None, {"owner": "acme", "repo": "app"}),
    ("get_pr", {"owner": "acme", "repo": "app", "pr_number": "#42"}, None, {"pr_number": 42}),
    ("get_pr", {"owner": "acme", "repo": "app", "pr": "42"}, None, {"pr_number": 42}),
    ("get_pr", {"owner": "acme", "repo": "app", "number": 7}, None, {"pr_number": 7}),
    ("get_issue", {"owner": "acme", "repo": "app", "issue": "#9"}, None, {"issue_number": 9}),
    ("list_prs", {"owner": "acme", "repo": "app", "state": "merged"}, None, {"state": "closed"}),
    ("list_prs", {"owner": "acme", "repo": "app", "state": "MERGED"}, None, {"state": "closed"}),
    ("list_prs", {"owner": "acme", "repo": "app", "state": "weird"}, None, {"state": "open"}),
    ("read_file", {"owner": "acme", "repo": "app", "path": "/src/./a.py"}, None, {"path": "src/a.py"}),
    ("read_file", {"owner": "acme", "repo": "app", "path": "https://github.com/acme/app/blob/main/src/a.py"}, None, {"path": "src/a.py"}),
    ("list_prs", {"repo": "REPO_NAME"}, ["acme/app"], {"owner": "acme", "repo": "app"}),
    ("list_prs", {"repo": "..."}, ["acme/app"], {"owner": "acme", "repo": "app"}),
    ("list_prs", {"owner": "owner", "repo": "<repo>"}, ["acme/app", "acme/web"], {"owner": "acme", "repo": "app"}),
    ("list_prs", {}, ["acme/app"], {"owner": "acme", "repo": "app"}),
    ("read_file", {"owner": "acme", "repo": "app", "start_line": "15"}, None, {"start_line": 15}),
])
def test_normalize_github_args_table(tool, args, known, expect):
    cleaned = normalize_github_args(tool, args, known)
    for key, value in expect.items():
        assert cleaned.get(key) == value, (key, cleaned)


def test_missing_repo_is_not_guessed_when_several_are_known():
    cleaned = normalize_github_args("list_prs", {}, ["acme/app", "acme/web"])
    assert not cleaned.get("owner")
    assert not cleaned.get("repo")


def test_unknown_tool_returns_a_suggestion_not_none():
    parsed = parse_github_request('{"tool": "list_pull_requests", "args": {}}')
    assert parsed["_error"]
    assert "Did you mean" in parsed["_error"]
    assert "list_prs" in parsed["_error"]
    assert parse_github_request("not json") is None
    ok = parse_github_request(
        '{"tool": "list_prs", "args": {"repo": "REPO_NAME"}}',
        known_repos=["acme/app"],
    )
    assert ok["args"]["owner"] == "acme"
    assert ok["args"]["repo"] == "app"


def test_prompt_is_shorter_and_uses_the_known_repo():
    text = build_github_prompt_section(
        [{"account_login": "acme"}], known_repos=["acme/app"],
    )
    assert len(text) < 6500
    assert len(text) < _OLD_PROMPT_CHARS - 1000
    assert text.count("\n") < 60
    assert '"repo":"app"' in text
    assert "REPO_NAME" not in text
    assert "PUSHING:" not in text
    assert "DEPLOYS:" not in text
    assert "<surgical_edit>" not in text
    pushed = build_github_prompt_section(
        [{"account_login": "acme"}], known_repos=["acme/app"],
        tier="read_write", wants_push=True, wants_deploy=True, wants_edit=True,
    )
    assert "PUSHING:" in pushed
    assert "push_session_file" in pushed
    assert "DEPLOYS:" in pushed
    assert "<surgical_edit>" in pushed


def test_push_and_deploy_intent():
    assert message_mentions_push("please push it")
    assert message_mentions_push("commit that file")
    assert not message_mentions_push("what is in my last PR?")
    assert message_mentions_deploy("did it deploy?")
    assert message_mentions_deploy("the vercel build failed")
    assert not message_mentions_deploy("what is in my last PR?")


def test_prompt_without_a_known_repo_says_to_list_repos_first():
    text = build_github_prompt_section([{"account_login": "acme"}])
    assert "list_repos" in text
    assert "<repo name>" in text


def test_parse_tolerates_a_json_fence():
    raw = '```json\n{"tool": "list_repos", "args": {}}\n```'
    parsed = parse_github_request(raw)
    assert parsed["tool"] == "list_repos"
    assert json.dumps(parsed["args"]) == "{}"
