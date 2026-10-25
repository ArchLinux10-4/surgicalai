"""Search-term cap and the shared tool footer."""
import json

from services.agent_tool_spec import limits_block
from services.limits import MAX_SEARCH_TERMS
from services.repo_tools import parse_repo_request
from services.tool_footer import tool_footer


def test_prompt_and_parser_share_the_search_cap():
    assert str(MAX_SEARCH_TERMS) in limits_block()
    parsed = parse_repo_request(json.dumps({
        "tool": "search_repo",
        "terms": ["a", "b", "c", "d", "e", "f"],
    }))
    assert parsed["terms"] == ["a", "b", "c", "d", "e"]


def test_tool_footer_continues_and_names_the_next_line():
    text = tool_footer(
        "repo",
        "FILE: app.py",
        continues_at=120,
        start_line=80,
        end_line=119,
        total=400,
    )
    assert text.endswith("\nContinue.")
    assert "Lines 80-119 of 400. Continue from line 120." in text
    assert "stop" not in text.lower()
    assert "Use these tools as many times as the task needs, one per turn." in limits_block()
