"""Prompt-cache markers for repeated Claude and OpenAI prefixes.

Claude: top-level cache_control moves the breakpoint as a conversation grows.
OpenAI: prompt_cache_key is a routing hint for models before GPT-5.6.
Grok keeps using x-grok-conv-id on the client. Gemini implicit caching needs
no flag. This module does not call any provider.
"""


def claude_cache_control() -> dict:
    """5-minute ephemeral breakpoint. Do not add a ttl key."""
    return {"type": "ephemeral"}


def claude_system_blocks(text: str) -> list:
    """One cached system block. Empty text still returns one block."""
    return [{
        "type": "text",
        "text": text or "",
        "cache_control": claude_cache_control(),
    }]


def openai_prompt_cache_kwargs(model: str, session_id: str = "") -> dict:
    """Routing key for a repeated OpenAI prefix. {} for everyone else.

    An empty session returns {} on purpose. A shared anon key would mix
    unrelated prompts onto one OpenAI route.
    """
    sid = (session_id or "").strip()
    mid = (model or "").strip().lower()
    if not sid or not mid:
        return {}
    if (
        mid.startswith("grok-")
        or mid.startswith("gemini-")
        or mid.startswith("models/gemini")
        or mid.startswith("claude-")
        or mid.startswith("ollama:")
    ):
        return {}
    return {"prompt_cache_key": f"surgicalai-{sid}"}
