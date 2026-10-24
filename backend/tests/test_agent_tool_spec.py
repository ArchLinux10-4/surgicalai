"""Tool wording stays shared, gated, and free of the old contradictions."""
import json
import re
from pathlib import Path

from services.agent_tool_spec import (
    DEFAULT_MAX_TURNS,
    MAX_FILES_PER_REQUEST,
    MAX_SEARCH_TERMS,
    limits_block,
    repo_text,
    scope_block,
)
from services.edit_intent import build_xml_agent_instruction
from services.grok_agent_tools import (
    TOOL_AWS_CLI,
    TOOL_REPORT_BLOCKED,
    TOOL_REQUEST_FILE,
    TOOL_REQUEST_GITHUB,
    TOOL_REQUEST_HISTORY,
    TOOL_REQUEST_MCP,
    TOOL_REQUEST_PREVIEW,
    TOOL_REQUEST_REPO,
    TOOL_REQUEST_SEARCH,
    TOOL_REQUEST_WEB_SEARCH,
    TOOL_WRITE_EDIT_PLAN,
    TOOL_WRITE_NEW_FILE,
    TOOL_WRITE_SURGICAL_EDIT,
    _context_body,
    build_grok_agent_instruction,
    build_grok_agent_tools,
    build_grok_system_suffix,
)
from services.pipeline import (
    NATURAL_SYSTEM,
    _NATURAL_HEAD,
    _NATURAL_TAG,
    _NATURAL_TAIL,
    _ask_plan_build_tool_instructions,
    _assemble_natural,
    grok_natural_system,
)

_GOLDEN = Path(__file__).resolve().parent / "golden" / "tool_text_baseline.json"

_TOOL_NAMES = {
    TOOL_WRITE_SURGICAL_EDIT,
    TOOL_WRITE_NEW_FILE,
    TOOL_WRITE_EDIT_PLAN,
    TOOL_REQUEST_FILE,
    TOOL_REQUEST_SEARCH,
    TOOL_REQUEST_GITHUB,
    TOOL_REQUEST_HISTORY,
    TOOL_REQUEST_PREVIEW,
    TOOL_REQUEST_WEB_SEARCH,
    TOOL_REQUEST_MCP,
    TOOL_REQUEST_REPO,
    TOOL_AWS_CLI,
    TOOL_REPORT_BLOCKED,
}

_TAG_NAMES = {
    "surgical_edit",
    "new_file",
    "edit_plan",
    "search_request",
    "file_request",
    "github_request",
    "history_request",
    "preview_request",
    "web_search_request",
    "mcp_request",
    "repo_request",
    "aws_cli",
    "blocked",
}

_FLAG_KEYS = (
    "github_enabled",
    "history_enabled",
    "aws_enabled",
    "preview_enabled",
    "websearch_enabled",
    "mcp_enabled",
    "repo_enabled",
)


def _mentioned(text):
    return {name for name in _TOOL_NAMES if re.search(r"\b" + re.escape(name) + r"\b", text)}


def _tags(text):
    return set(re.findall(r"<([a-z_]+)>", text)) & _TAG_NAMES


def test_natural_system_is_head_tag_and_tail():
    assert NATURAL_SYSTEM == _NATURAL_HEAD + _NATURAL_TAG + _NATURAL_TAIL


def test_github_and_web_text_is_unchanged():
    from services.github_natural_tag import build_github_prompt_section
    from services.github_tool_spec import grok_function_schema, render_selection_table, render_tool_list
    from services.grok_agent_tools import _schema_request_web_search
    from services.openai_web_search import (
        CLAUDE_WEB_SEARCH_ON,
        GPT_WEB_SEARCH_ON,
        GROK_WEB_SEARCH_ON,
        INVALID_NOTE,
    )

    saved = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    current = {
        "render_selection_table": render_selection_table(),
        "render_tool_list_read": render_tool_list(include_write=False),
        "render_tool_list_write": render_tool_list(include_write=True),
        "grok_function_schema": grok_function_schema(),
        "build_github_prompt_section": build_github_prompt_section(
            [{"account_login": "acme"}],
            known_repos=["acme/app"],
            tier="read_only",
            wants_push=False,
            wants_deploy=False,
            wants_edit=False,
        ),
        "GPT_WEB_SEARCH_ON": GPT_WEB_SEARCH_ON,
        "CLAUDE_WEB_SEARCH_ON": CLAUDE_WEB_SEARCH_ON,
        "GROK_WEB_SEARCH_ON": GROK_WEB_SEARCH_ON,
        "INVALID_NOTE": INVALID_NOTE,
        "request_web_search_schema": _schema_request_web_search(),
    }
    assert current == saved


def test_function_name_parity_across_modes_and_flags():
    # A reduced grid: each flag on alone, all on, and all off. Enough to catch
    # a tool that is described but not offered, or the reverse.
    grids = []
    grids.append({key: False for key in _FLAG_KEYS})
    grids.append({key: True for key in _FLAG_KEYS})
    for key in _FLAG_KEYS:
        row = {name: False for name in _FLAG_KEYS}
        row[key] = True
        grids.append(row)
    for mode in ("edit", "agent", "ask", "plan"):
        for flags in grids:
            suffix_flags = dict(flags)
            suffix_flags["preflight_enabled"] = False
            text = (
                build_grok_system_suffix(mode, **suffix_flags)
                + "\n"
                + build_grok_agent_instruction(mode, **{
                    k: flags[k] for k in flags
                    if k in (
                        "github_enabled", "history_enabled", "preview_enabled",
                        "websearch_enabled", "mcp_enabled", "repo_enabled",
                    )
                })
            )
            offered = {
                tool["function"]["name"]
                for tool in build_grok_agent_tools(mode, **flags)
            }
            assert _mentioned(text) == offered, (mode, flags, _mentioned(text) ^ offered)


def test_tag_names_match_enabled_flags():
    on = build_xml_agent_instruction(
        wants_edit=True, github_enabled=True, history_enabled=True,
        preview_enabled=True, websearch_enabled=True,
        repo_guidance="repo on",
        mcp_guidance="<mcp_request>{}",
    )
    assert _tags(on) <= _TAG_NAMES
    assert "github_request" in _tags(on)
    assert "history_request" in _tags(on)
    assert "preview_request" in _tags(on)
    assert "repo_request" in _tags(on)

    off = build_xml_agent_instruction(wants_edit=True)
    found = _tags(off)
    assert "github_request" not in found
    assert "history_request" not in found
    assert "preview_request" not in found
    assert "repo_request" not in found
    assert "web_search_request" not in found
    assert "file_request" in found
    assert "search_request" in found


def test_ask_plan_hides_tools_that_are_off():
    off = _ask_plan_build_tool_instructions(
        has_files=False, gh_enabled=False, gh_known_repos=[], max_rounds=DEFAULT_MAX_TURNS,
    )
    assert "file_request" not in off
    assert "search_request" not in off
    assert "history_request" not in off
    assert "repo_request" not in off
    assert "github_request" not in off
    on = _ask_plan_build_tool_instructions(
        has_files=True, gh_enabled=False, gh_known_repos=[], max_rounds=DEFAULT_MAX_TURNS,
        repo_enabled=True,
    )
    assert "file_request" in on
    assert "repo_request" in on
    assert "not in this chat yet" in on
    assert "Where each tool looks" in on


def test_scope_block_needs_two_places():
    assert scope_block(session_files=True, form="tag") == ""
    assert scope_block(github=True, form="function") == ""
    both = scope_block(session_files=True, repo=True, form="function")
    assert "request_file" in both and "request_repo" in both
    assert "Where each tool looks" in both


def test_gating_phrases_follow_flags():
    bare = build_grok_system_suffix("edit")
    assert "request_repo" not in bare
    assert "request_preview" not in bare
    assert "HISTORICAL" not in bare
    full = build_grok_system_suffix(
        "edit", repo_enabled=True, preview_enabled=True, history_enabled=True,
    )
    assert "request_repo" in full
    assert "not in this chat yet" in full
    assert "request_preview" in full
    assert "HISTORICAL" in full


def test_claude_has_one_anchor_rule_and_gpt_has_the_other():
    assert NATURAL_SYSTEM.count("Lines are tried first") == 1
    assert "ALWAYS include" not in NATURAL_SYSTEM
    gpt = _assemble_natural("gpt")
    gemini = _assemble_natural("gemini")
    assert gpt == gemini
    assert gpt.count("ALWAYS include") == 1
    assert "Lines are tried first" not in gpt


def test_grok_prompt_has_no_tool_tags_and_no_ignore():
    text = (
        grok_natural_system()
        + build_grok_system_suffix(
            "edit", github_enabled=True, history_enabled=True, repo_enabled=True,
            preview_enabled=True, websearch_enabled=True, mcp_enabled=True,
            aws_enabled=True,
        )
        + build_grok_agent_instruction(
            "edit", github_enabled=True, history_enabled=True, repo_enabled=True,
            preview_enabled=True, websearch_enabled=True, mcp_enabled=True,
        )
    )
    assert "IGNORE" not in text
    # aws_agent_guidance() is unchanged and names <aws_cli> so it can tell
    # Grok to call the function instead. Every other tool tag stays out.
    leaked = _tags(text) - {"aws_cli"}
    assert leaked == set(), leaked


def test_git_diff_is_described_for_both_forms():
    tag = repo_text("tag")
    function = repo_text("function")
    for text in (tag, function):
        assert "git_diff" in text
        assert "diff_branches" in text
        assert "does not fetch" in text.lower()
    assert '<repo_request>{"tool":"git_diff"}</repo_request>' in tag
    assert "<repo_request>" not in function

    from services.grok_agent_tools import _schema_request_repo
    enum = _schema_request_repo()["function"]["parameters"]["properties"]["tool"]["enum"]
    assert "git_diff" in enum
    props = _schema_request_repo()["function"]["parameters"]["properties"]
    for field in ("scope", "base", "head", "view", "dots"):
        assert field in props
    assert _schema_request_repo()["function"]["parameters"]["required"] == ["tool"]

    _kind, body = _context_body("request_repo", {
        "tool": "git_diff", "scope": "range", "base": "origin/main",
        "head": "HEAD", "view": "patch", "dots": "two",
    })
    assert _kind == "repo"
    assert json.loads(body) == {
        "tool": "git_diff", "path": "", "terms": [],
        "scope": "range", "base": "origin/main", "head": "HEAD",
        "view": "patch", "dots": "two",
    }
    _kind, plain = _context_body("request_repo", {"tool": "git_status"})
    assert json.loads(plain) == {"tool": "git_status", "path": "", "terms": []}


def test_limits_use_the_real_constants():
    text = limits_block("tag", DEFAULT_MAX_TURNS)
    assert str(MAX_FILES_PER_REQUEST) in text
    assert str(MAX_SEARCH_TERMS) in text
    assert str(DEFAULT_MAX_TURNS) in text
    assert str(DEFAULT_MAX_TURNS) in NATURAL_SYSTEM
    assert "max 4 search rounds" not in NATURAL_SYSTEM
