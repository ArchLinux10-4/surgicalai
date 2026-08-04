"""
Tests for Grok gap #5: every Grok-reachable streaming loop in
services/pipeline.py used to do ``chunk.choices[0]`` unconditionally.

Real-world evidence:
  * BerriAI/litellm#17136 ("Grok streaming returns usage in the wrong chunk")
    and xAI's own streaming docs
    (docs.x.ai/developers/model-capabilities/text/streaming) show xAI's SSE
    stream carries a full ``usage`` object on every chunk.
  * OpenRouter's documented error/debug streaming chunk format
    (openrouter.ai/docs/api_reference/errors-and-debugging) shows real chunks
    with ``"choices": []`` (empty array) — e.g. mid-stream provider errors or
    debug-echo chunks.

An unconditional ``chunk.choices[0]`` access on such a chunk raises an
unhandled ``IndexError`` and kills the whole response stream. The fix is a
single shared helper, ``services.pipeline._iter_openai_stream_chunks``, used
by every Grok-reachable streaming loop identified in this repo (see the
source-truth tests below for the exact file:line call sites) — it filters out
empty-``choices`` chunks (logging the skip via ``_dlog``) and is otherwise a
pure pass-through, so every existing normal chunk (GPT, Grok, or otherwise)
is completely unaffected.

NO LIVE API CALLS — fully mocked/fake streaming iterators only.
"""
import pathlib
import sys

import pytest

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

from services import pipeline  # noqa: E402

_PIPELINE_PATH = _BACKEND / "services" / "pipeline.py"


class _Rec:
    """Collects (event, kwargs) so tests can assert _dlog fired on the path."""

    def __init__(self):
        self.events = []

    def __call__(self, event, **kw):
        self.events.append((event, kw))

    @property
    def names(self):
        return [e for e, _ in self.events]


class _FakeDelta:
    def __init__(self, content=None):
        self.content = content


class _FakeChoice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    """Mirrors the real OpenAI-SDK-shaped streaming chunk object closely
    enough to exercise `.choices` / `.usage` access exactly like the real
    pipeline.py loops do."""

    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


class _FakeUsage:
    def __init__(self, prompt_tokens=10, completion_tokens=5):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


def _malformed_chunk_then_normal(content="hello world"):
    """A fake streaming iterator yielding one chunk with `choices=[]` and
    `usage` populated (mirroring the real litellm-reported/xAI-documented
    shape), followed by a normal chunk with real delta content."""
    return [
        _FakeChunk(choices=[], usage=_FakeUsage()),
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=content))]),
    ]


# ─────────────────────────────────────────────────────────────────────────
# 1. The shared helper itself
# ─────────────────────────────────────────────────────────────────────────

def test_iter_openai_stream_chunks_skips_empty_choices_no_exception():
    stream = _malformed_chunk_then_normal()
    out = list(pipeline._iter_openai_stream_chunks(stream, model="grok-4.5",
                                                    session_id="s1", user_id="u1"))
    assert len(out) == 1
    assert out[0].choices[0].delta.content == "hello world"


def test_iter_openai_stream_chunks_logs_skip_via_dlog(monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(pipeline, "_dlog", rec)
    stream = _malformed_chunk_then_normal()
    list(pipeline._iter_openai_stream_chunks(stream, model="grok-4.5",
                                              session_id="s1", user_id="u1"))
    assert "grok_stream_chunk_empty_choices" in rec.names
    # Only the malformed chunk triggers the skip log — exactly once.
    assert rec.names.count("grok_stream_chunk_empty_choices") == 1
    skip_kwargs = dict(rec.events[rec.names.index("grok_stream_chunk_empty_choices")][1])
    assert skip_kwargs["session_id"] == "s1"
    assert skip_kwargs["user_id"] == "u1"
    assert skip_kwargs["model"] == "grok-4.5"
    assert skip_kwargs["has_usage"] is True


def test_iter_openai_stream_chunks_is_noop_for_all_normal_chunks(monkeypatch):
    """Zero behaviour change for the passing case: every normal chunk (with
    non-empty choices) is yielded unchanged and no skip event fires."""
    rec = _Rec()
    monkeypatch.setattr(pipeline, "_dlog", rec)
    stream = [
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content="a"))]),
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content="b"))]),
        _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(content=None), finish_reason="stop")]),
    ]
    out = list(pipeline._iter_openai_stream_chunks(stream, model="gpt-5.6-terra"))
    assert len(out) == 3
    assert [c.choices[0].delta.content for c in out[:2]] == ["a", "b"]
    assert out[2].choices[0].finish_reason == "stop"
    assert "grok_stream_chunk_empty_choices" not in rec.names


def test_iter_openai_stream_chunks_never_raises_indexerror_on_empty_choices():
    stream = [_FakeChunk(choices=[]), _FakeChunk(choices=[])]
    # Must not raise — every chunk is malformed, so the generator yields
    # nothing at all, but does not crash.
    out = list(pipeline._iter_openai_stream_chunks(stream, model="grok-4.5"))
    assert out == []


def test_iter_openai_stream_chunks_does_not_eat_good_data():
    """The fix must not silently eat good data — only the malformed chunk is
    skipped, the normal chunk's content is still yielded correctly."""
    stream = _malformed_chunk_then_normal(content="real answer text")
    out = list(pipeline._iter_openai_stream_chunks(stream))
    assert len(out) == 1
    assert out[0].choices[0].delta.content == "real answer text"


# ─────────────────────────────────────────────────────────────────────────
# 2. Feed the fake malformed-then-normal stream through each fixed call site
#    (via the shared helper each one now uses) and assert no unhandled
#    exception, correct content still yielded, and the skip logged.
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model", ["grok-4.5", "gpt-5.6-terra"])
def test_shared_helper_handles_grok_and_gpt_models_identically(monkeypatch, model):
    """The guard is provider-agnostic — Grok and GPT chunks both flow through
    identically; this proves the fix is a pure no-op for GPT too."""
    rec = _Rec()
    monkeypatch.setattr(pipeline, "_dlog", rec)
    stream = _malformed_chunk_then_normal()
    out = list(pipeline._iter_openai_stream_chunks(stream, model=model,
                                                    session_id="sX", user_id="uX"))
    assert len(out) == 1
    assert out[0].choices[0].delta.content == "hello world"
    assert "grok_stream_chunk_empty_choices" in rec.names


# ─────────────────────────────────────────────────────────────────────────
# 3. Source-truth: every Grok-reachable streaming loop identified in this
#    repo actually uses the shared helper (not a raw, unguarded
#    `for chunk in stream:` loop indexing `.choices[0]` directly).
# ─────────────────────────────────────────────────────────────────────────

def _pipeline_src() -> str:
    return _PIPELINE_PATH.read_text()


def test_iter_openai_stream_chunks_helper_defined_once():
    src = _pipeline_src()
    assert src.count("def _iter_openai_stream_chunks(") == 1


@pytest.mark.parametrize("marker", [
    # 1. Ask/Plan "GPT / Gemini shared tool loop" — reachable by Grok via
    #    _get_client_for_model (services/pipeline.py, run_chat_stream).
    "_oai_client = _get_client_for_model(chat_model, user_id, session_id=session_id)",
    # 2. run_chat_stream's shared OpenAI/GPT/Grok streaming branch.
    'if _is_grok_model(chat_model):\n                _dlog("run_chat_stream_grok_client"',
    # 3. Smart pipeline chat's shared GPT/Grok streaming branch.
    '_dlog("smart_pipeline_chat_grok_client"',
    # 4. Natural pipeline's Grok-native-tools streaming loop.
    '_dlog("natural_grok_native_tools_attached"',
])
def test_each_identified_grok_reachable_loop_uses_the_shared_helper(marker):
    """For each Grok-reachable streaming loop identified in this repo, the
    `_iter_openai_stream_chunks(` call must appear within a bounded window
    after the loop's own identifying marker — proving that specific loop was
    migrated to the guarded helper, not just that the helper exists
    somewhere in the file."""
    src = _pipeline_src()
    assert marker in src, f"marker not found (source may have shifted): {marker!r}"
    start = src.index(marker)
    window = src[start:start + 2000]
    assert "_iter_openai_stream_chunks(" in window


def test_no_remaining_unguarded_choices_index_in_streaming_loops():
    """Regression guard: none of the four fixed loops' bodies should still
    contain a raw `for chunk in stream:` / `for _chunk in ...stream:` pattern
    feeding directly into `.choices[0]` without going through the shared
    helper. (Non-streaming single-response `.choices[0].message...` accesses,
    e.g. the Surgeon tool-use call, are a different and lower-risk pattern —
    explicitly out of scope and NOT asserted against here.)
    """
    src = _pipeline_src()
    # Exclude the helper's own definition body (it legitimately contains
    # `for chunk in stream:` — that IS the guard implementation) before
    # checking the rest of the file for any bypassing raw loop.
    helper_start = src.index("def _iter_openai_stream_chunks(")
    helper_end = src.index("\ndef ", helper_start + 1)  # next top-level def
    src_outside_helper = src[:helper_start] + src[helper_end:]

    # One deliberately-excluded case: run_chat_stream's `elif
    # _is_gemini_model(chat_model):` branch (Gemini-via-OpenAI-compat
    # fallback) also does a raw `for chunk in stream:` / `.choices[0]`, but
    # it is mutually exclusive with the sibling `if _is_grok_model(...)`
    # branch in the same if/elif chain — i.e. it is NOT reachable for any
    # Grok model id, so it is correctly out of this task's scope (task only
    # requires guarding loops "reachable when chat_model/arch_model is a
    # Grok model"). Excise that one known non-Grok-reachable occurrence
    # before checking for any other (in-scope) unguarded loop.
    gemini_fallback_marker = "elif _is_gemini_model(chat_model):\n            # Fallback: Gemini via OpenAI-compat (no native thinking)"
    assert gemini_fallback_marker in src_outside_helper, (
        "expected known non-Grok-reachable Gemini fallback branch not found "
        "at its expected location — source may have shifted, re-verify by "
        "hand before trusting this exclusion"
    )
    gemini_start = src_outside_helper.index(gemini_fallback_marker)
    gemini_end = gemini_start + 800
    src_checked = src_outside_helper[:gemini_start] + src_outside_helper[gemini_end:]

    # Every remaining `.choices[0].delta` OR `.choices[0]` used as a
    # streaming-chunk choice access must be preceded (within the same small
    # loop body) by an `_iter_openai_stream_chunks(` call feeding the loop
    # variable — spot-checked via the four known loop variables.
    for loop_var, chunk_source in [
        ("_chunk", "_oai_stream"),
        ("chunk", "stream"),
        ("_gpt_chunk", "_gpt_stream"),
    ]:
        # Every "for X in _iter_openai_stream_chunks(Y, ...)" occurrence is
        # accounted for; a bare "for X in Y:" for the same pair would
        # indicate an unguarded loop was missed.
        unguarded_pattern = f"for {loop_var} in {chunk_source}:"
        assert unguarded_pattern not in src_checked, (
            f"found an unguarded raw loop over {chunk_source!r} using "
            f"{loop_var!r} that bypasses _iter_openai_stream_chunks"
        )
