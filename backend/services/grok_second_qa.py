"""
grok_second_qa.py — Second, Grok-only QA verification pass.

WHY THIS FILE EXISTS (do not merge into pipeline.py's QA code):
Grok 4.5-generated diffs already pass through the existing `run_qa_agent`
(Sonnet-based) review like every other model's output. Real-world usage
showed Grok 4.5 diffs still slip through with more subtle logic mistakes
than Sonnet/GPT diffs, even after an 8+/10 first-pass score. Per user
request, this adds ONE additional, independent verification round —
Grok-only, run only after the existing QA already passed — to maximize
accuracy on that final check. It intentionally does NOT touch, wrap, or
modify `run_qa_agent` / `run_qa_for_changes` (existing, working QA code).

DESIGN — grounded in evidence, not guessing:
1. Chain-of-Verification (CoVe) pattern (Dhuliawala et al., "Chain-of-
   Verification Reduces Hallucination in Large Language Models", Meta AI —
   https://arxiv.org/abs/2309.11495): instead of one holistic "rate this"
   pass, the reviewer (a) drafts specific verification questions about the
   diff, (b) answers each independently against the real code, (c) only
   then synthesizes a final verdict. This structure measurably reduces
   hallucinated judgments vs. a single-shot review.
2. Fault-awareness / overcorrection avoidance (arXiv "Are LLMs Reliable
   Code Reviewers? Systematic Overcorrection in LLM Code Review",
   https://arxiv.org/html/2603.00539v1): LLM reviewers that are told to
   just "look harder" tend to invent problems in already-correct code.
   The mitigation documented there: force every finding to cite a
   specific line/mechanism; discard vague/stylistic-only findings.

BEHAVIOR:
- Only ever RAISES scrutiny — never re-blocks a passed change unless it
  finds a concrete, cited defect (avoids overcorrection false-rejects).
- Never raises exceptions; on any failure, returns the original
  `qa_result` untouched (fails open, never breaks the pipeline).
- Appends its structured findings onto `qa_result["grok_second_pass"]`
  for visibility in the QA report, regardless of outcome.
"""

import json
import re

from anthropic import AsyncAnthropic


def _dlog(event: str, **kwargs):
    """Same contract as pipeline.py's _dlog — separate import to avoid a
    circular import (pipeline.py will import this module, not vice versa)."""
    try:
        from services.pipeline import _dlog as _pipeline_dlog
        _pipeline_dlog(event, **kwargs)
    except Exception:
        pass  # Never let logging break this pass


_SECOND_QA_MODEL = "claude-sonnet-5"

_COVE_SYSTEM_PROMPT = """You are a second, independent code-verification pass. \
A first QA review already scored this change safe (8/10 or higher). Your job \
is NOT to re-review holistically — it is to run a Chain-of-Verification check:

1. Write 3-5 SPECIFIC verification questions about this exact change (not \
generic ones). Good questions probe things like: "does the new code path \
in `<function>` actually get invoked anywhere?", "does the described DB \
column update happen before or after the value is read elsewhere?", "does \
this change reference a symbol/import that doesn't exist in the shown code?".
2. Answer EACH question by reasoning only against the exact code shown below \
— do not assume standard library behavior you cannot verify from the shown \
code.
3. Only report a finding if you can cite a SPECIFIC line number or exact \
code fragment as the mechanism of the defect. Do NOT report style \
preferences, naming nitpicks, or "could be cleaner" observations — those are \
noise, not defects, and the first pass already covers style. If you cannot \
cite a concrete mechanism, do not report it.
4. Respond with ONLY a JSON object (no prose, no markdown fences):
{
  "verification_questions": ["q1", "q2", ...],
  "verification_answers": ["a1", "a2", ...],
  "concrete_findings": [
    {"description": "...", "cited_line_or_fragment": "...", "severity": "blocking|minor"}
  ],
  "verdict": "confirmed_safe" | "concrete_issue_found"
}
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in second-pass QA response")
    return json.loads(match.group(0))


async def run_grok_second_qa_pass(
    qa_result: dict,
    *,
    original_code: str,
    new_code: str,
    change_description: str,
    filename: str,
    symbol_path: str,
    session_id: str = "",
    user_id: str = "",
) -> dict:
    """
    Runs a second, Grok-only CoVe-style verification pass on a change that
    already passed the standard QA (`run_qa_agent`). Call this ONLY after
    the standard QA has already run and returned a non-blocked verdict, and
    ONLY when the generating model was Grok.

    Never raises. On any internal error, returns `qa_result` unchanged (plus
    an error marker) so the pipeline is never affected by this extra pass.
    """
    _dlog("grok_second_qa_start",
          session_id=session_id, user_id=user_id,
          filename=filename, symbol_path=symbol_path,
          first_pass_verdict=qa_result.get("verdict"),
          first_pass_score=qa_result.get("qa_score"))

    # Only run the second pass on changes the first pass already accepted —
    # this is an *additional* accuracy check, not a replacement gate.
    if qa_result.get("verdict") == "blocked":
        _dlog("grok_second_qa_skipped_already_blocked",
              session_id=session_id, user_id=user_id, filename=filename)
        return qa_result

    try:
        # Cap code shown to keep this pass fast + focused (same 200K-char
        # ceiling philosophy as run_qa_agent, but a tighter cap is fine here
        # since this pass targets the already-narrowed change, not full-file
        # review).
        _MAX_CHARS = 60_000

        def _cap(_c: str) -> str:
            if len(_c) <= _MAX_CHARS:
                return _c
            half = _MAX_CHARS // 2
            return _c[:half] + "\n... [elided for second-pass review] ...\n" + _c[-half:]

        user_prompt = (
            f"FILE: {filename}\nSYMBOL: {symbol_path}\n\n"
            f"REQUESTED CHANGE:\n{change_description}\n\n"
            f"ORIGINAL CODE:\n```\n{_cap(original_code)}\n```\n\n"
            f"NEW CODE (after change):\n```\n{_cap(new_code)}\n```\n\n"
            f"FIRST-PASS QA SUMMARY: {qa_result.get('summary', '(none)')}"
        )

        from services.pipeline import _get_anthropic_key  # reuse existing key resolver

        client = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
        resp = await client.messages.create(
            model=_SECOND_QA_MODEL,
            max_tokens=2000,
            system=_COVE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        parsed = _extract_json(raw_text)

        concrete_findings = parsed.get("concrete_findings", []) or []
        blocking_findings = [f for f in concrete_findings if f.get("severity") == "blocking"]

        qa_result["grok_second_pass"] = {
            "verification_questions": parsed.get("verification_questions", []),
            "verification_answers": parsed.get("verification_answers", []),
            "concrete_findings": concrete_findings,
            "verdict": parsed.get("verdict", "confirmed_safe"),
        }

        if blocking_findings:
            qa_result["verdict"] = "blocked"
            qa_result["qa_score"] = min(qa_result.get("qa_score") or 10, 5)
            qa_result["summary"] = (
                (qa_result.get("summary") or "") +
                f" | Second-pass verification found a concrete issue: {blocking_findings[0]['description']}"
            ).strip(" |")
            _dlog("grok_second_qa_escalated",
                  session_id=session_id, user_id=user_id, filename=filename,
                  finding=blocking_findings[0].get("description", "")[:200],
                  cited=blocking_findings[0].get("cited_line_or_fragment", "")[:200])
        else:
            _dlog("grok_second_qa_confirmed_safe",
                  session_id=session_id, user_id=user_id, filename=filename,
                  questions_asked=len(parsed.get("verification_questions", [])))

        return qa_result

    except Exception as exc:
        _dlog("grok_second_qa_error",
              session_id=session_id, user_id=user_id, filename=filename,
              error_type=type(exc).__name__, error=str(exc)[:300])
        # Fail open — never let this extra pass block or alter the pipeline.
        qa_result.setdefault("grok_second_pass", {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        return qa_result
