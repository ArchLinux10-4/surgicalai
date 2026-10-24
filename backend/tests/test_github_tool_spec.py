"""The prompt, the Grok schema, and the executors stay on one tool list."""
import inspect

from services import deploy_status
from services import github_context_tools as gct
from services import github_natural_tag as gh
from services.github_tool_spec import (
    ALL_TOOL_NAMES,
    GH_TOOLS,
    READ_TOOL_NAMES,
    anthropic_tool_schemas,
    grok_function_schema,
    render_tool_list,
)


def test_read_tools_include_lookups_and_exclude_pushes():
    assert "list_prs" in READ_TOOL_NAMES
    assert "get_pr_diff" in READ_TOOL_NAMES
    assert "get_pr" in READ_TOOL_NAMES
    assert "list_branches" in READ_TOOL_NAMES
    assert "list_commits" in READ_TOOL_NAMES
    assert "push_files" not in READ_TOOL_NAMES
    assert "push_session_file" not in READ_TOOL_NAMES


def test_grok_schema_enum_matches_the_catalogue():
    schema = grok_function_schema()
    tool = schema["function"]["parameters"]["properties"]["tool"]
    assert tool["enum"] == list(ALL_TOOL_NAMES)
    text = tool["description"]
    assert "path_prefix" in text
    assert "path?" not in text
    assert "provider" in text
    assert schema["function"]["parameters"]["required"] == ["tool"]


def test_prompt_and_schema_name_the_same_tools():
    prompt = gh.build_github_prompt_section(
        [{"account_login": "acme"}], known_repos=["acme/app"],
    )
    pushed = gh.build_github_prompt_section(
        [{"account_login": "acme", "permission_tier": "read_write"}],
        known_repos=["acme/app"], tier="read_write", wants_push=True,
    )
    listing = render_tool_list(include_write=True)
    for tool in GH_TOOLS:
        assert tool.name in listing
        if tool.write:
            assert tool.name in pushed
        else:
            assert tool.name in prompt


def test_every_tool_is_dispatched_and_every_arg_is_read():
    context_src = inspect.getsource(gct.execute_github_context_tool)
    natural_src = inspect.getsource(gh.execute_github_request)
    deploy_src = inspect.getsource(deploy_status.check_deploy_status)
    session_src = inspect.getsource(gh.push_session_file_from_db)
    combined = "\n".join([context_src, natural_src, deploy_src, session_src])
    for tool in GH_TOOLS:
        assert f'"{tool.name}"' in combined or f"'{tool.name}'" in combined
        for arg in tool.args:
            assert f'"{arg.name}"' in combined or f"'{arg.name}'" in combined, arg.name


def test_architect_schemas_cover_the_context_tools():
    names = [item["name"] for item in anthropic_tool_schemas()]
    assert names == [tool.name for tool in GH_TOOLS if tool.executor == "context"]
    listed = {item["name"]: item for item in gct.GITHUB_CONTEXT_TOOLS_V2}
    assert "get_pr" in listed
    assert "path_prefix" in listed["list_files"]["input_schema"]["properties"]
    assert "path" not in listed["list_files"]["input_schema"]["properties"]


def test_examples_normalize_without_being_rejected():
    for tool in GH_TOOLS:
        parsed = gh.parse_github_request(
            __import__("json").dumps({"tool": tool.name, "args": tool.example}),
            known_repos=["acme/app"],
        )
        assert parsed and "tool" in parsed
        assert parsed["tool"] == tool.name
        assert "_error" not in parsed
