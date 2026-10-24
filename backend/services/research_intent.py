"""Detect an obvious request to search the web, and the hint used when Research is off.

The detector is narrow on purpose. A loose match ("research", "web search"
as a noun) would nag on ordinary coding requests. Code blocks and traceback
lines are removed first so a pasted log cannot fire it.

The hint is plain text with no tag or tool-call syntax, so the model is not
invited to emit a search call that would not run.
"""
import re

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE = re.compile(r"`[^`\n]{1,200}`")
_TRACEBACK_START = "Traceback (most recent call last)"
_ERROR_LINE = re.compile(r"^(?:[A-Za-z0-9_.]+)?(?:Error|Exception|Warning):")

# Imperative phrasing aimed at the agent. Bare nouns ("web search",
# "research") are intentionally absent.
_REQUESTS = (
    re.compile(r"\bsearch(?:ing)?\s+the\s+(?:web|internet)\b", re.I),
    re.compile(r"\bsearch(?:ing)?\s+online\b", re.I),
    re.compile(r"\bgoogle\s+(?:it|this|that|them)\b", re.I),
    re.compile(r"\blook\s+(?:it|this|that|them)\s+up\s+online\b", re.I),
    re.compile(r"\bresearch(?:\s+(?:this|it|that))?\s+online\b", re.I),
    re.compile(r"\bdo(?:\s+some|\s+a\s+bit\s+of)?\s+(?:web|online|internet)\s+research\b", re.I),
    re.compile(r"\bbrowse\s+the\s+web\b", re.I),
    re.compile(r"\bcheck\s+online\b", re.I),
    re.compile(r"\buse\s+web\s+search\b", re.I),
    re.compile(r"\bwith\s+web\s+search\b", re.I),
)

HINT = (
    "The user asked for web research, but web search is turned off for this message. "
    "You do have a web search tool. Do not claim you searched, do not invent sources, "
    "and do not emit any search tags. Tell the user to turn on the Research toggle "
    "(magnifier icon next to the message box) and send the message again. Then still "
    "help as far as you can from your own knowledge and the attached code, and say "
    "that anything time-sensitive may be out of date."
)


def _prose(text: str) -> str:
    """Drop fenced code, inline code, and traceback lines. First 2000 chars."""
    sample = _FENCE.sub(" ", text[:2000])
    sample = _INLINE.sub(" ", sample)
    kept = []
    in_traceback = False
    for line in sample.splitlines():
        stripped = line.strip()
        if stripped.startswith(_TRACEBACK_START):
            in_traceback = True
            continue
        if in_traceback:
            if not stripped:
                in_traceback = False
            continue
        if stripped.startswith('File "') and "line " in stripped:
            continue
        if _ERROR_LINE.match(stripped):
            continue
        kept.append(line)
    return "\n".join(kept)


def message_requests_web_research(text) -> bool:
    """True when the user clearly asks the agent to search the web."""
    if not isinstance(text, str) or not text.strip():
        return False
    prose = _prose(text)
    return any(pattern.search(prose) for pattern in _REQUESTS)


def research_off_hint(user_text, *, flag_on, offered, is_agent_task) -> str:
    """Hint text, or "" when the hint must not be added.

    Fires only when the user asked for web research, Research is off, the
    checkbox exists for this model, this is not a generated task-runner
    prompt, and the ``research_off_hint`` kill switch is not off.
    """
    try:
        if flag_on or not offered or is_agent_task:
            return ""
        if not message_requests_web_research(user_text):
            return ""
        from database import get_setting
        flag = str(get_setting("research_off_hint", "on") or "on").strip().lower()
        if flag == "off":
            return ""
        return HINT
    except Exception:
        return ""
