"""Plan-mode clarifying questions: parse, gate, prompt.

Plan mode may end a turn with a ``plan_questions`` JSON fence instead of the
``implementation_plan`` fence. The user answers (or skips) in the UI and the
answer arrives as an ordinary next Plan turn — no server-side pause state.

Limits are enforced here, by structure, not by prompt:

* at most ``MAX_QUESTIONS`` questions and ``MAX_OPTIONS`` options each;
* one round per prompt (``questions_allowed`` is False on the turn that
  answers a question turn, and while a plan is being implemented);
* any malformed fence parses to ``None`` so the caller falls back to the
  normal ``implementation_plan`` retry path.
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_QUESTIONS = 3
MAX_OPTIONS = 2
MAX_QUESTION_CHARS = 200
MAX_LABEL_CHARS = 60
MAX_DESC_CHARS = 140

_FENCE_RE = re.compile(r"```plan_questions\s*([\s\S]*?)```", re.IGNORECASE)


def _clip(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


class PlanOption(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str
    description: str = ""
    recommended: bool = False

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, v):
        return _clip(v, MAX_LABEL_CHARS)

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, v):
        return _clip(v, MAX_DESC_CHARS)

    @field_validator("recommended", mode="before")
    @classmethod
    def _recommended(cls, v):
        return v is True


class PlanQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    question: str
    options: list[PlanOption] = Field(default_factory=list)

    @field_validator("question", mode="before")
    @classmethod
    def _question(cls, v):
        return _clip(v, MAX_QUESTION_CHARS)


def parse_plan_questions(text: str) -> list[dict] | None:
    """Return normalized questions, or None if the fence is missing/unusable.

    Accepts ``{"questions": [...]}`` or a bare JSON array. Never raises.
    """
    if not (text or "").strip():
        return None
    m = _FENCE_RE.search(text)
    if not m:
        return None
    raw = (m.group(1) or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None

    items = payload.get("questions") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None

    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if len(out) >= MAX_QUESTIONS:
            break
        if not isinstance(item, dict):
            continue
        try:
            q = PlanQuestion.model_validate(item)
        except Exception:
            continue
        if not q.question:
            continue
        key = q.question.casefold()
        if key in seen:
            continue
        seen.add(key)

        options: list[dict] = []
        opt_seen: set[str] = set()
        has_recommended = False
        for opt in q.options:
            if len(options) >= MAX_OPTIONS:
                break
            if not opt.label or opt.label.casefold() in opt_seen:
                continue
            opt_seen.add(opt.label.casefold())
            recommended = opt.recommended and not has_recommended
            has_recommended = has_recommended or recommended
            options.append({
                "label": opt.label,
                "description": opt.description,
                "recommended": recommended,
            })
        out.append({
            "id": f"q{len(out) + 1}",
            "question": q.question,
            "options": options,
        })
    return out or None


def strip_plan_questions_fence(text: str) -> str:
    return _FENCE_RE.sub("", text or "").strip()


def questions_allowed(session_id: str) -> bool:
    """True when this Plan turn may ask clarifying questions.

    False when the latest assistant message was itself a question turn (this
    turn is the answer) or when a plan is currently being implemented. Reads
    the DB, never the client, so it cannot be spoofed. Fails closed.
    """
    try:
        from database import get_db_ctx
        from services.plan_artifact import latest_plan_run

        with get_db_ctx() as conn:
            row = conn.execute(
                "SELECT metadata FROM chat_messages "
                "WHERE session_id = ? AND role = 'assistant' "
                "ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row:
            raw = row["metadata"]
            if raw:
                try:
                    meta = json.loads(raw)
                except Exception:
                    meta = {}
                if isinstance(meta, dict) and meta.get("plan_questions"):
                    return False
        latest = latest_plan_run(session_id)
        if latest and latest.get("phase") == "implementing":
            return False
        return True
    except Exception:
        return False


_BLOCK_ALLOWED = (
    "\n\n[PLAN QUESTIONS]\n"
    "Default: do NOT ask. Read the attached code, the conversation and the "
    "project memory first, make the most reasonable assumption, and list it "
    "under `## Assumptions` in the plan.\n"
    "Ask a clarifying question ONLY if ALL of these are true:\n"
    "1. The answer changes which files, architecture or user-visible "
    "behaviour the plan touches.\n"
    "2. It cannot be found in the attached files, the conversation or the "
    "memory.\n"
    "3. A wrong guess would waste real work.\n"
    "Never ask about style, naming, whether to proceed, anything the user "
    "already stated, or anything you can decide yourself.\n"
    "Limits: at most 3 questions (prefer 1). Each question is self-contained "
    "and under 200 characters, with 1 or 2 mutually exclusive, concrete "
    "options; put the option you recommend first with \"recommended\": true. "
    "Do not add \"Other\" or \"Skip\" options — the UI provides free text and "
    "skip.\n"
    "Output contract — reply with EITHER:\n"
    "(A) the plan, ending with the ```implementation_plan fence; OR\n"
    "(B) one short sentence, then a single ```plan_questions fence and "
    "nothing after it. No plan, no steps, no implementation_plan fence in "
    "that reply.\n"
    "Never both. This section overrides any instruction above to always end "
    "with the implementation_plan fence, but only when you choose (B).\n"
    "Shape for (B):\n"
    "```plan_questions\n"
    "{\"questions\": [{\"question\": \"Should search extend the existing "
    "/items endpoint or get a new /search endpoint?\", \"options\": "
    "[{\"label\": \"Extend /items\", \"description\": \"Touches "
    "routes/items.py only\", \"recommended\": true}, {\"label\": \"New "
    "/search endpoint\", \"description\": \"Adds routes/search.py and a "
    "router entry\"}]}]}\n"
    "```\n"
    "Worth asking: two incompatible designs that change which files the plan "
    "edits. Not worth asking: variable naming or button colour — decide, then "
    "state the assumption."
)

_BLOCK_CLOSED = (
    "\n\n[PLAN QUESTIONS — CLOSED]\n"
    "The user has answered or skipped your clarifying questions in their "
    "latest message. Do not ask any more questions this turn and do not emit "
    "a plan_questions fence. Produce the plan now. Treat skipped items as "
    "'decide yourself' and record each under `## Assumptions`. If the latest "
    "message is instead an unrelated new request, plan it without asking."
)


def plan_questions_block(allowed: bool) -> str:
    """Prompt block appended last (recency) to the Plan directive."""
    return _BLOCK_ALLOWED if allowed else _BLOCK_CLOSED
