"""AI rescue tier for failed surgical edits (v3.5).

When the deterministic apply ladder in surgical_editor exhausts every
strategy (line replace → relocation → SEARCH/REPLACE → anchor → hunk-split),
the change lands in `failed_changes` with a detailed reason. Instead of
dropping it on the floor, this module makes ONE disciplined last-ditch
attempt: it shows Claude the current file, the intended edit, and the exact
failure reason, and asks for a strict SEARCH/REPLACE pair against the
CURRENT file content.

Guardrails (deterministic validation — the model proposes, code disposes):
- The returned `search` string must appear EXACTLY ONCE in the current file.
- `search` must differ from `replace`.
- The file must not shrink more than 20% from a single rescue.
- Any parse/validation/API failure keeps the change failed, with the rescue
  outcome appended to the original reason. Never a silent drop.
"""

import json
import re
from typing import Optional

RESCUE_MODEL = "claude-sonnet-5"   # same tier the pipeline uses for corrections
RESCUE_MAX_TOKENS = 8000           # replaces can be long — never starve the output
MAX_RESCUES_PER_BATCH = 5          # cost guard
_MAX_FILE_CHARS = 60_000           # beyond this, send a window around the best guess


def _locate_window(file_content: str, change) -> str:
    """Best-effort context window when the file is too large to send whole."""
    probe = ""
    sym = getattr(change, "symbol", None)
    if sym is not None:
        probe = getattr(sym, "name", "") or ""
    if not probe:
        oc = getattr(change, "original_code", "") or ""
        probe = oc.strip().splitlines()[0][:80] if oc.strip() else ""
    idx = file_content.find(probe) if probe else -1
    if idx < 0:
        # middle of the file as a fallback window
        idx = len(file_content) // 2
    start = max(0, idx - _MAX_FILE_CHARS // 2)
    end = min(len(file_content), idx + _MAX_FILE_CHARS // 2)
    return file_content[start:end]


def _build_prompt(file_path: str, file_content: str, change, reason: str) -> str:
    original_code = getattr(change, "original_code", "") or ""
    new_code = getattr(change, "new_code", "") or ""
    sym = getattr(change, "symbol", None)
    sym_name = (getattr(sym, "full_path", None) or getattr(sym, "name", "?")) if sym else "?"
    desc = getattr(change, "description", "") or ""

    if len(file_content) > _MAX_FILE_CHARS:
        shown = _locate_window(file_content, change)
        file_note = f"EXCERPT of the current file (window around the target area, file is {len(file_content)} chars total)"
    else:
        shown = file_content
        file_note = "FULL current content of the file"

    return f"""A surgical code edit failed to apply automatically. Your job is to rescue it.

FILE: {file_path}
TARGET SYMBOL: {sym_name}
EDIT INTENT: {desc or '(no description)'}

WHY THE AUTOMATIC APPLY FAILED (from the deterministic engine):
{reason}

THE EDIT AS ORIGINALLY GENERATED (may reference stale file content):
--- intended old code ---
{original_code[:6000]}
--- intended new code ---
{new_code[:6000]}

{file_note}:
<<<FILE
{shown}
FILE>>>

TASK: Determine whether this edit's INTENT can still be applied to the CURRENT
file above. The file may have drifted (other edits applied, lines moved,
whitespace changed) — that is usually why the engine failed.

Respond with ONLY a JSON object, no markdown fences, no prose:
{{
  "can_fix": true or false,
  "search": "exact text copied VERBATIM from the CURRENT file above (must be unique in the file — include enough surrounding lines to make it unambiguous)",
  "replace": "that same text with the intended edit applied",
  "explanation": "one sentence: what you did, or why it cannot be fixed"
}}

Rules:
- "search" MUST be an exact character-for-character substring of the current file. Copy it from the file content above — do not retype or reformat it.
- If the edit is ALREADY present in the current file (someone applied it another way), return can_fix=false with explanation "already applied".
- If the target code no longer exists or the intent no longer makes sense, return can_fix=false.
- Never invent code beyond the stated intent."""


def _parse_response(text: str) -> Optional[dict]:
    """Parse the model reply into a dict; tolerate accidental fences."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        # last resort: first {...} block
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
    return None


def rescue_failed_changes(
    file_path: str,
    file_content: str,
    all_changes: list,
    failed_changes: list,
    user_id: str = "",
    _client=None,  # test seam: pass a fake client with .messages.create
):
    """Attempt an AI rescue for each failed change.

    Returns (new_content, rescued: list[dict], still_failed: list[dict]).
    Every failed change ends up in exactly one of the two lists; still_failed
    entries keep the original reason plus the rescue outcome appended.
    """
    rescued: list = []
    still_failed: list = []

    if not failed_changes:
        return file_content, rescued, still_failed

    # Resolve a client once. If no key is configured, report that honestly.
    client = _client
    if client is None:
        try:
            from services.pipeline import _get_anthropic_key
            from anthropic import Anthropic
            client = Anthropic(api_key=_get_anthropic_key(user_id or ""))
        except Exception as e:
            for f in failed_changes:
                f = dict(f)
                f["reason"] = f.get("reason", "") + f" | AI rescue unavailable: {e}"
                still_failed.append(f)
            return file_content, rescued, still_failed

    by_id = {getattr(c, "id", None): c for c in all_changes}
    content = file_content

    for i, f in enumerate(failed_changes):
        f = dict(f)
        if i >= MAX_RESCUES_PER_BATCH:
            f["reason"] = f.get("reason", "") + " | AI rescue skipped: batch rescue limit reached"
            still_failed.append(f)
            continue

        change = by_id.get(f.get("change_id"))
        if change is None:
            f["reason"] = f.get("reason", "") + " | AI rescue skipped: change object not found"
            still_failed.append(f)
            continue

        try:
            prompt = _build_prompt(file_path, content, change, f.get("reason", ""))
            resp = client.messages.create(
                model=RESCUE_MODEL,
                max_tokens=RESCUE_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                getattr(b, "text", "") for b in resp.content
                if getattr(b, "type", "") == "text"
            )
            parsed = _parse_response(text)

            if not parsed:
                f["reason"] = f.get("reason", "") + " | AI rescue failed: unparseable model response"
                still_failed.append(f)
                continue

            explanation = str(parsed.get("explanation", ""))[:300]

            if not parsed.get("can_fix"):
                f["reason"] = f.get("reason", "") + f" | AI rescue declined: {explanation or 'model reported not fixable'}"
                f["rescue_verdict"] = "declined"
                still_failed.append(f)
                continue

            search = parsed.get("search")
            replace = parsed.get("replace")
            if not isinstance(search, str) or not isinstance(replace, str) or not search:
                f["reason"] = f.get("reason", "") + " | AI rescue failed: missing search/replace in response"
                still_failed.append(f)
                continue
            if search == replace:
                f["reason"] = f.get("reason", "") + f" | AI rescue no-op: {explanation}"
                still_failed.append(f)
                continue

            occurrences = content.count(search)
            if occurrences != 1:
                f["reason"] = (
                    f.get("reason", "")
                    + f" | AI rescue rejected: proposed search text matched {occurrences} times (must be exactly 1)"
                )
                still_failed.append(f)
                continue

            candidate = content.replace(search, replace, 1)
            if len(candidate) < len(content) * 0.8:
                f["reason"] = f.get("reason", "") + " | AI rescue rejected: would shrink file >20%"
                still_failed.append(f)
                continue

            content = candidate
            rescued.append({
                "change_id": f.get("change_id"),
                "symbol": f.get("symbol"),
                "explanation": explanation or "AI-rescued via exact search/replace",
            })

        except Exception as e:
            f["reason"] = f.get("reason", "") + f" | AI rescue errored: {str(e)[:200]}"
            still_failed.append(f)

    try:
        from services.pipeline import _dlog
        _dlog("edit_rescue_summary", file=file_path,
              attempted=min(len(failed_changes), MAX_RESCUES_PER_BATCH),
              rescued=len(rescued), still_failed=len(still_failed))
    except Exception:
        pass

    return content, rescued, still_failed
