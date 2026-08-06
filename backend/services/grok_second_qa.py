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

import difflib
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
3b. You are shown a UNIFIED DIFF of the exact change (lines starting with \
"+" are added, "-" are removed, unprefixed lines are unchanged context). \
A finding that claims the requested change "is not present" or "is missing" \
is ONLY valid if you can cite the specific "+"/"-" line(s) proving it — \
never claim code is absent merely because you don't see it restated outside \
the diff; the diff IS the change.
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


# Root cause (proven via surgical_debug_ecce169c.jsonl, 2026-08-06): the old
# approach sent the FULL original/new symbol text, each independently capped
# to 60K chars (head+tail split). Real symbols in this app are routinely
# whole inline <script> blocks — one confirmed case was 145,268 chars — so
# the edited function (measured at ~68% into the symbol) landed squarely in
# the elided MIDDLE 85K chars. The second-pass verifier then correctly, and
# truthfully, reported "the change is not present in the shown code" — it
# wasn't hallucinating; the code was never shown to it. This is the
# well-documented "lost in the middle" / truncation-blind-spot failure mode
# for long-context LLM review (see e.g. context-management literature on
# lost-in-the-middle effects), and this exact codebase already hit and fixed
# an equivalent bug in `run_qa_agent` (raised 60K -> 200K + head/tail
# elision) — but a symbol can always eventually exceed any fixed raw-blob
# cap again, so a size cap alone is not a permanent fix.
#
# Fix: show a UNIFIED DIFF (with generous context) instead of two raw code
# blobs. A diff scales with the SIZE OF THE CHANGE, not the size of the
# symbol, so the edited lines are always included regardless of how large
# the surrounding symbol grows — this is the same "give the model only the
# relevant slice, not the whole undifferentiated blob" principle underlying
# retrieval-over-raw-context best practice. A conservative cap remains as a
# defensive fallback for the rare pathological case (e.g. a full-symbol
# rewrite producing a massive diff), using the same head+tail elision
# pattern already proven in `run_qa_agent`, plus a fallback to full-code
# review if diff generation ever produces nothing despite the code actually
# differing.
_MAX_DIFF_CHARS = 100_000
_MAX_FALLBACK_CODE_CHARS = 200_000  # matches run_qa_agent's proven cap


def _cap_with_elision(text: str, max_chars: int) -> tuple[str, bool]:
    """Head+tail elision (never a naive tail-only cut) — returns (capped_text, was_capped)."""
    if len(text) <= max_chars:
        return text, False
    half = max_chars // 2
    dropped = len(text) - (2 * half)
    return (
        text[:half]
        + f"\n\n... [{dropped} chars elided from the MIDDLE — head and tail shown in full] ...\n\n"
        + text[-half:]
    ), True


def _build_review_content(
    original_code: str, new_code: str, *, session_id: str, user_id: str, filename: str, symbol_path: str,
) -> str:
    """Builds the code content shown to the second-pass verifier as a unified
    diff, so the reviewed slice always scales with the size of the CHANGE
    rather than the size of the whole symbol. Falls back to capped full-code
    review only if diffing produces nothing despite the code differing."""
    orig_lines = (original_code or "").splitlines(keepends=True)
    new_lines = (new_code or "").splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        orig_lines, new_lines, fromfile="ORIGINAL", tofile="NEW (after change)", n=20,
    ))
    diff_text = "".join(diff_lines)
    hunk_count = sum(1 for l in diff_lines if l.startswith("@@"))

    if not diff_text.strip() and (original_code or "").strip() != (new_code or "").strip():
        # Defensive fallback: diffing produced nothing (e.g. line-ending-only
        # divergence) despite the code genuinely differing — never silently
        # show an empty diff. Fall back to full capped code, matching
        # run_qa_agent's proven 200K approach, so the verifier is never
        # shown less than before this fix.
        _dlog("grok_second_qa_diff_empty_fallback",
              session_id=session_id, user_id=user_id, filename=filename, symbol_path=symbol_path,
              original_len=len(original_code or ""), new_len=len(new_code or ""))
        orig_capped, _ = _cap_with_elision(original_code or "", _MAX_FALLBACK_CODE_CHARS)
        new_capped, _ = _cap_with_elision(new_code or "", _MAX_FALLBACK_CODE_CHARS)
        content = f"ORIGINAL CODE:\n```\n{orig_capped}\n```\n\nNEW CODE (after change):\n```\n{new_capped}\n```"
        _dlog("grok_second_qa_content_built",
              session_id=session_id, user_id=user_id, filename=filename, symbol_path=symbol_path,
              mode="full_code_fallback", content_len=len(content), hunk_count=0, diff_capped=False)
        return content

    diff_capped_text, was_capped = _cap_with_elision(diff_text, _MAX_DIFF_CHARS)
    content = f"UNIFIED DIFF OF THE CHANGE:\n```diff\n{diff_capped_text}\n```"
    _dlog("grok_second_qa_content_built",
          session_id=session_id, user_id=user_id, filename=filename, symbol_path=symbol_path,
          mode="unified_diff", content_len=len(content), hunk_count=hunk_count, diff_capped=was_capped,
          original_symbol_len=len(original_code or ""), new_symbol_len=len(new_code or ""))
    return content


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
        # Diff-based content (not raw capped blobs) — see _build_review_content
        # docstring for the proven root-cause evidence behind this approach.
        _review_content = _build_review_content(
            original_code, new_code,
            session_id=session_id, user_id=user_id, filename=filename, symbol_path=symbol_path,
        )

        user_prompt = (
            f"FILE: {filename}\nSYMBOL: {symbol_path}\n\n"
            f"REQUESTED CHANGE:\n{change_description}\n\n"
            f"{_review_content}\n\n"
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
        _all_blocking = [f for f in concrete_findings if f.get("severity") == "blocking"]
        # Secondary safety net (defense-in-depth, not the root-cause fix — see
        # _build_review_content above for that): the prompt already instructs
        # the model to only report a finding it can cite (rule 3, and the
        # overcorrection-avoidance mitigation cited in this file's own
        # docstring), but that was never enforced in code. Enforce it here so
        # a future uncited claim (from any cause) can never silently cap the
        # score — it's downgraded and logged instead of trusted at face value.
        blocking_findings = [f for f in _all_blocking if (f.get("cited_line_or_fragment") or "").strip()]
        _uncited_dropped = [f for f in _all_blocking if not (f.get("cited_line_or_fragment") or "").strip()]
        if _uncited_dropped:
            _dlog("grok_second_qa_uncited_finding_discarded",
                  session_id=session_id, user_id=user_id, filename=filename, symbol_path=symbol_path,
                  discarded_count=len(_uncited_dropped),
                  finding=_uncited_dropped[0].get("description", "")[:200])

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
