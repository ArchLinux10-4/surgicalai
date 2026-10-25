"""Decide whether an Edit-mode turn is a code change or a plain question.

Edit and Agent chat share one loop. A question has to be recognized before
that loop appends "write the edits now", or the model answers as an edit
agent and the turn ends with a "no code changes" notice.

Ambiguous messages count as edit requests. A message is a question only when
it is clearly question-shaped and the change-verb, if any, is not the
request itself ("can you change the color" is an edit; "why does the add
button fail" is a question).
"""
import re

# Change-verbs. Based on grok_plan_gate._ACTION_VERBS, minus words that show
# up inside questions as nouns ("hook", "show", "ask"). Plus the verbs people
# actually use when they want code changed ("fix", "edit", "tweak").
_ACTION_VERBS = (
    "add", "create", "make", "build", "implement", "widen", "resize",
    "enlarge", "update", "change", "remove", "delete", "wire",
    "refactor", "move", "rename", "replace", "introduce", "enable",
    "hide", "fix", "edit", "modify", "rewrite", "apply", "tweak",
    "adjust", "correct", "convert", "extract", "split", "merge",
    "inline", "wrap", "disable", "revert", "undo",
)

_POLITE = (
    r"please|pls|kindly|can you|could you|would you|will you|"
    r"i want you to|i'?d like you to|i would like you to|let'?s|lets|just"
)
_VERB_ALT = "|".join(_ACTION_VERBS)

# Verb in instruction position: start of the message, or right after
# "please" / "can you" / "let's". A verb mentioned later ("why does the
# add button fail") does not count.
_INSTRUCTION_VERB_RE = re.compile(
    rf"(?:^|[.!?]\s+|\b(?:{_POLITE})\s+)(?:{_VERB_ALT})\b",
    re.IGNORECASE,
)

_QUESTION_LEAD_RE = re.compile(
    r"^(?:(?:please|pls|hey|hi|hello|ok|okay|so|well|um|uh|hmm)[,!\s]+)*"
    r"(?:"
    r"what|why|how|where|which|when|who|"
    r"explain|describe|tell me|walk me|"
    r"(?:can|could|would)\s+you\s+(?:tell|explain|describe|walk|help)|"
    r"does|did|is|are|was|were|"
    r"can|could|would|should"
    r")\b",
    re.IGNORECASE,
)

# Smart-stream prepends "[N file(s) attached: ...]" before the real message.
_FILE_HINT_RE = re.compile(
    r"^\[\d+ file\(s\) attached:[^\]]*\]\s*",
    re.IGNORECASE,
)

# "don't change that" / "do not build the plan" is a refusal, not an edit job.
_NEGATION_RE = re.compile(
    r"^(?:please\s+)?(?:do not|don't|dont|never)\b",
    re.IGNORECASE,
)

_GO_AHEAD_RE = re.compile(
    r"^(?:yes|yep|yeah|yup|ok|okay|sure|go ahead|do it|proceed|"
    r"ship it|implement it|build it|make the changes|let'?s do it|"
    r"sounds good|lgtm|do that|go for it)"
    r"\s*[.!?]*\s*$",
    re.IGNORECASE,
)

_PENDING_PLAN_PHASES = frozenset({"ready", "blocked"})


def strip_attachment_hint(text: str) -> str:
    """Drop the leading file-attachment hint so it cannot change the decision."""
    raw = (text or "").lstrip()
    return _FILE_HINT_RE.sub("", raw, count=1).strip()


def pending_saved_plan(session_id, forced_edit_plan=None, plan_run_id=None) -> bool:
    """True when this turn is carrying a plan the user can still build.

    A forced plan or a plan run id is enough. Otherwise look up the latest
    saved plan. Any lookup failure counts as no plan, and the message is
    classified from its own wording.
    """
    if forced_edit_plan or plan_run_id:
        return True
    if not session_id:
        return False
    try:
        from services.plan_artifact import latest_plan_run
        latest = latest_plan_run(session_id)
    except Exception:
        return False
    if not latest:
        return False
    return (latest.get("phase") or "") in _PENDING_PLAN_PHASES


def _is_question(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    if body.endswith("?"):
        return True
    return _QUESTION_LEAD_RE.match(body) is not None


def _instruction_verb(text: str) -> bool:
    return _INSTRUCTION_VERB_RE.search(text or "") is not None


def message_wants_edit(text, has_saved_plan=False, is_agent_task=False) -> bool:
    """True when this turn should keep the strict edit loop.

    Agent-task and task-runner steps always return True. A saved-plan
    go-ahead ("ok", "go ahead") returns True. Everything else is an edit
    unless it is a question with no instruction-position change verb.
    """
    if is_agent_task:
        return True
    body = strip_attachment_hint(text if isinstance(text, str) else ("" if text is None else str(text)))
    if not body:
        return True
    from services.plan_artifact import message_requests_plan_build
    if message_requests_plan_build(body):
        return True
    if _NEGATION_RE.match(body):
        return False
    if has_saved_plan and _GO_AHEAD_RE.match(body):
        return True
    if _is_question(body) and not _instruction_verb(body):
        return False
    return True


def build_xml_agent_instruction(*, wants_edit, github_enabled=False,
                                history_enabled=False, aws_guidance="",
                                preview_enabled=False, preflight_enabled=False,
                                websearch_enabled=False, mcp_guidance="",
                                repo_guidance="", git_guidance="", local_guidance="",
                                max_rounds=None):
    """Suffix appended to the latest user turn for Claude and GPT.

    ``wants_edit`` True is the existing edit contract, unchanged.
    ``wants_edit`` False keeps the same context tools and tells the model
    to answer. It may still emit an edit tag if the user asked for a change.
    """
    from services.agent_tool_spec import (
        blocked_text as _blocked_text,
        file_blurb as _file_blurb,
        history_text as _history_text,
        limits_block as _limits_block,
        plan_text as _plan_text,
        scope_block as _scope_block,
        search_blurb as _search_blurb,
    )
    scope = _scope_block(
        session_files=True,
        repo=bool(repo_guidance),
        github=bool(github_enabled),
        web=bool(websearch_enabled),
        git=bool(git_guidance),
        local=bool(local_guidance),
        form="tag",
    )
    tools = (
        ((scope + "\n") if scope else "")
        + _limits_block("tag", max_rounds) + "\n"
        + "• " + _search_blurb("tag") + "\n"
        + "• " + _file_blurb("tag") + "\n"
        + ("• <github_request> — read from the connected GitHub repository.\n" if github_enabled else "")
        + (aws_guidance or "")
        + (("• " + _history_text("tag") + "\n") if history_enabled else "")
        + (
            "• <preview_request>{\"files\": [\"Widget.tsx\"]} — render the pending "
            "edit in the live preview and read compile or runtime errors. "
            "Only .tsx and .jsx. Call it after you edit. unobserved is not a pass.\n"
            if preview_enabled else ""
        )
        + (
            "• <web_search_request>{\"query\": \"...\", \"reason\": \"...\"} — live web search "
            "over the public internet. It is not a code edit. <search_request> only "
            "searches attached files. Put only the question in query. Do not include "
            "code, file names, or secrets.\n"
            if websearch_enabled else ""
        )
        + (mcp_guidance if mcp_guidance else "")
        + (repo_guidance if repo_guidance else "")
        + (git_guidance or "")
        + (local_guidance or "")
    )
    if wants_edit:
        return (
            "\n\n[AGENTIC EDIT MODE]\n"
            "Complete this task autonomously. You decide what you need and when.\n\n"
            "CONTEXT TOOLS — use any of these, as many times as you need, in any\n"
            "order, and interleave them freely with your edits:\n"
            + tools
            + "\nWhen you request context the stream pauses, the results are handed\n"
            "back to you, and you continue from there.\n\n"
            "OUTPUT — emit when (and only when) you have the context to be correct:\n"
            "• <surgical_edit> — a precise edit to an existing file.\n"
            "• <new_file> — a brand-new file.\n"
            "• <edit_plan> — " + _plan_text("tag") + "\n\n"
            "RULES:\n"
            "1. If you already have enough context, write the edits now.\n"
            "2. If you need code you don't have, request it — never guess at code,\n"
            "   file contents, or APIs you have not actually seen.\n"
            "3. " + _blocked_text("tag") + " Do not guess.\n"
            "4. When your edits are complete, simply stop.\n"
            + (
                "5. A preflight check may reject an edit that introduces a syntax "
                "error, a duplicate definition, or a missing import. If you see "
                "[PREFLIGHT], that edit was NOT applied. Fix it and send a "
                "corrected edit. Do not resend the same edit.\n"
                if preflight_enabled else ""
            )
        )
    return (
        "\n\nAnswer the user's message directly.\n\n"
        "CONTEXT TOOLS — use these only if you need code you have not seen "
        "to answer correctly:\n"
        + tools
        + "\nWhen you request context the stream pauses, the results are handed\n"
        "back to you, and you continue from there.\n\n"
        "RULES:\n"
        "1. Answer in plain text. Do not mention your mode or these instructions.\n"
        "2. Emit <surgical_edit>, <new_file>, or <edit_plan> only if the user "
        "asked you to change code.\n"
        "3. If you need code you don't have, request it — never guess at code,\n"
        "   file contents, or APIs you have not actually seen.\n"
        "4. " + _blocked_text("tag") + "\n"
        "5. When you have answered, stop. Do not say that no code changes were produced.\n"
    )
