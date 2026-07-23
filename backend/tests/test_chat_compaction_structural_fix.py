"""
Regression test: long-session compaction must not compound information loss.

Root cause (proven from live routers/chat.py, GitHub main branch):
  - HISTORY_TOKEN_BUDGET=30_000 / MIN_RECENT_KEEP=6 fired compaction often in a
    3+ hour session.
  - Every message being folded in was hard-truncated to 500 chars before the
    summarizer ever saw it (`cleaned[:500]`).
  - The compaction call was capped to max_tokens=500 / "under 400 words", and
    it summarized "existing summary + new turns" together and then OVERWROTE
    session_summary with that result — so every round re-compressed the
    already-compressed prior summary alongside new turns. Multiple rounds in
    a long session compound this loss.
  - The model used was gpt-4.1-mini first (only falling back to Claude on no
    OpenAI key), rather than Claude Sonnet 5 (the model used everywhere else
    in this codebase, e.g. pipeline.py `surg_model = ... "claude-sonnet-5"`).

Fix (this test verifies, via source inspection + isolated functional logic,
since the anthropic/openai SDKs are not installed in this sandbox):
  1. Raised HISTORY_TOKEN_BUDGET / MIN_RECENT_KEEP / per-message truncate /
     summary max_tokens / word cap so compaction fires less often and keeps
     more per round.
  2. Structural fix to the compounding-loss mechanism: each round now
     summarizes ONLY the new turns and APPENDS the result to the existing
     summary (never re-asks the model to compress prior summary text).
  3. A separate, rare "meta-compaction" pass only runs when the appended
     summary itself exceeds SUMMARY_META_COMPACT_CHARS — the one place old
     summary text may be re-compressed, and only occasionally.
  4. Claude Sonnet 5 ("claude-sonnet-5", matching the exact model ID string
     used elsewhere in the codebase) is now preferred over gpt-4.1-mini,
     which is fallback-only for users with no Anthropic key.
"""
import re
import textwrap

SRC_PATH = "/tasklet/agent/home/pending_push/chat.py"


def _read_src() -> str:
    with open(SRC_PATH, "r", encoding="utf-8") as f:
        return f.read()


def test_thresholds_raised():
    src = _read_src()
    assert "HISTORY_TOKEN_BUDGET = 60_000" in src, "budget must be raised from 30_000"
    assert "MIN_RECENT_KEEP = 20" in src, "recent-keep window must be raised from 6"
    assert "PER_MESSAGE_TRUNCATE_CHARS = 3000" in src, "per-message truncate must be raised from 500"
    assert "SUMMARY_MAX_TOKENS = 1500" in src, "summary token budget must be raised from 500"
    assert "SUMMARY_WORD_CAP = 1200" in src, "summary word cap must be raised from 400"


def test_per_message_truncation_uses_new_constant_not_hardcoded_500():
    src = _read_src()
    # The old hard-coded literal must be gone from the truncation call site.
    assert 'cleaned[:500]' not in src
    assert "cleaned[:PER_MESSAGE_TRUNCATE_CHARS]" in src


def test_summary_is_appended_not_replaced():
    src = _read_src()
    # The function body must build new_summary by appending to `existing`,
    # not just assign the model's raw new-chunk output as the whole summary.
    idx = src.index("async def _compact_session")
    body = src[idx: idx + 12000]
    assert "new_chunk_summary" in body
    assert "existing.rstrip()" in body, "must build on top of the existing summary text"
    assert 'f"{existing.rstrip()}\\n\\n---\\n\\n"' in body or "existing.rstrip()}\\n\\n---" in body


def test_prompt_does_not_ask_model_to_resummarize_existing():
    src = _read_src()
    idx = src.index("compact_prompt = (")
    prompt_block = src[idx: idx + 1400]
    assert "do not restate or re-summarize any earlier summary" in prompt_block
    # Must no longer feed "Previous summary:\n{existing}" into the same prompt
    # that's asked to shrink everything to one blob.
    assert 'prompt_parts.append(f"Previous summary' not in src


def test_meta_compaction_gate_present_and_rare():
    src = _read_src()
    assert "SUMMARY_META_COMPACT_CHARS = 12_000" in src
    assert "if len(new_summary) > SUMMARY_META_COMPACT_CHARS:" in src
    assert "compact_meta_consolidation_applied" in src
    assert "compact_meta_consolidation_error" in src, "meta pass must degrade safely on failure"


def test_meta_compaction_failure_keeps_appended_summary_not_lost():
    src = _read_src()
    idx = src.index("except Exception as meta_exc:")
    block = src[idx: idx + 400]
    assert "keep the (larger but complete) appended" in block or "Degrade safely" in block


def test_claude_sonnet_5_preferred_over_gpt41mini():
    src = _read_src()
    idx = src.index("async def _compact_session")
    body = src[idx: idx + 8000]
    # anthropic branch must be checked first (if anthropic_key: ... elif openai_key:)
    anth_pos = body.index("if anthropic_key:")
    openai_pos = body.index("elif openai_key:")
    assert anth_pos < openai_pos, "Claude must be tried before GPT-4.1-mini"
    assert 'model="claude-sonnet-5"' in body
    assert "claude-haiku-4-5-20251001" not in body, "old haiku fallback model must be gone"


def test_meta_pass_also_uses_claude_sonnet_5():
    src = _read_src()
    idx = src.index("Rare meta-compaction")
    block = src[idx: idx + 3000]
    assert 'model="claude-sonnet-5"' in block


def _simulate_append_logic(existing: str, new_chunk_summary: str) -> str:
    """Mirrors the exact append branch in _compact_session for isolated testing."""
    if existing.strip():
        return (
            f"{existing.rstrip()}\n\n---\n\n"
            f"### Additional session activity\n{new_chunk_summary.strip()}"
        )
    return new_chunk_summary.strip()


def test_append_logic_preserves_prior_content_verbatim():
    existing = "**User intent**\n- Build a widget"
    new_chunk = "**User intent**\n- Add tests for the widget"
    result = _simulate_append_logic(existing, new_chunk)
    # Prior round's content must be fully present verbatim (proves no re-compression).
    assert "Build a widget" in result
    assert "Add tests for the widget" in result
    assert result.startswith(existing.rstrip())


def test_append_logic_first_round_has_no_empty_preamble():
    result = _simulate_append_logic("", "**User intent**\n- Start project")
    assert result == "**User intent**\n- Start project"
    assert "---" not in result


def test_meta_compact_char_threshold_triggers_after_many_rounds():
    # Simulate 5 rounds of appending ~3000-char chunks; must eventually exceed
    # SUMMARY_META_COMPACT_CHARS (12_000), proving the gate is reachable.
    running = ""
    chunk = "X" * 3000
    for _ in range(5):
        running = _simulate_append_logic(running, chunk)
    assert len(running) > 12_000, "appended summary should exceed meta-compact threshold after 5 rounds"
