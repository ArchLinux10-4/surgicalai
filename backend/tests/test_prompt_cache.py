"""Prompt-cache markers for repeated Claude and OpenAI prefixes.

No live API calls. The helpers are pure, and the wiring checks read source.
"""
import pathlib
import sys

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

from services.prompt_cache import (  # noqa: E402
    claude_cache_control,
    claude_system_blocks,
    openai_prompt_cache_kwargs,
)


def test_claude_cache_control_is_ephemeral_without_ttl():
    marker = claude_cache_control()
    assert marker == {"type": "ephemeral"}
    assert "ttl" not in marker


def test_claude_system_blocks_always_one_cached_block():
    blocks = claude_system_blocks("hello")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "hello"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in blocks[0]["cache_control"]

    empty = claude_system_blocks("")
    assert len(empty) == 1
    assert empty[0]["text"] == ""
    assert empty[0]["cache_control"] == {"type": "ephemeral"}


def test_openai_prompt_cache_key_is_session_scoped_for_gpt_only():
    expected = {"prompt_cache_key": "surgicalai-sess-1"}
    for model in ("gpt-5.5", "gpt-5.6-sol", "gpt-6-sol"):
        got = openai_prompt_cache_kwargs(model, "sess-1")
        assert got == expected
        assert "prompt_cache_options" not in got

    for model in (
        "grok-4.7",
        "gemini-2.5-pro",
        "models/gemini-2.5-pro",
        "claude-sonnet-5-5",
        "ollama:qwen",
        "",
    ):
        got = openai_prompt_cache_kwargs(model, "sess-1")
        assert got == {}
        assert "prompt_cache_options" not in got

    assert openai_prompt_cache_kwargs("gpt-5.5", "") == {}
    assert openai_prompt_cache_kwargs("gpt-5.5", None) == {}
    assert "prompt_cache_options" not in openai_prompt_cache_kwargs("gpt-5.5", "")


def test_live_call_sites_use_the_cache_helpers():
    """Source checks so a later edit cannot drop the breakpoints silently."""
    import inspect

    from services import pipeline
    from services import gpt_reasoning

    chat_src = inspect.getsource(pipeline.run_chat_stream)
    assert "claude_system_blocks(" in chat_src
    assert '"cache_control": claude_cache_control()' in chat_src

    natural_src = inspect.getsource(pipeline.run_natural_pipeline_stream)
    assert '"cache_control": claude_cache_control()' in natural_src
    assert natural_src.count('"cache_control": {"type": "ephemeral"}') == 2

    surgical_src = inspect.getsource(pipeline.analyze_and_plan_stream)
    assert surgical_src.count("claude_system_blocks(CLAUDE_EDITOR_SYSTEM)") == 2
    assert '"cache_control": claude_cache_control()' not in surgical_src

    chat_create_src = inspect.getsource(pipeline._chat_create)
    assert "openai_prompt_cache_kwargs(model, session_id)" in chat_create_src

    responses_src = inspect.getsource(gpt_reasoning._build_responses_kwargs)
    assert '"prompt_cache_key"' in responses_src
    handled_at = responses_src.find("_handled")
    assert handled_at != -1
    assert '"prompt_cache_key"' in responses_src[handled_at:]
