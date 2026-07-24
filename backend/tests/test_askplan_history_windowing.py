"""
Regression tests for the Ask/Plan history-windowing fix.

Root cause (proven via surgical_debug_d8f0ed39 (5).jsonl): the Ask/Plan
smart_stream branch in routers/chat.py sent the FULL raw, uncleaned
conversation_history straight to run_chat_stream() -- no HISTORY_WINDOW
slice, no _clean_history_content(), no per-message cap. The Edit-mode path
(services/pipeline.py, clean_history build ~line 14963) already applies all
three. In production this let 29 raw messages, including uncleaned
__SURGICAL_RESULT__ JSON blobs, reach 1,353,079 tokens, which Anthropic
hard-rejected (400: "prompt is too long ... > 1000000 maximum"), surfaced to
the user as the generic "file may be too large" error while the user was in
Ask mode.

A second, related bug: the compaction-trigger token estimate at
routers/chat.py (`_history_tokens`) also summed raw uncleaned content,
which could produce a false `needs_compaction` even for small sessions.

These tests exercise the REAL functions (_estimate_history_tokens,
_build_mode_history) extracted verbatim from routers/chat.py via the same
in-repo AST-extraction pattern used by tests/test_lf2_fix.py.
"""
import ast
import os
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_CHAT = os.path.normpath(os.path.join(_HERE, "..", "routers", "chat.py"))
_PIPELINE = os.path.normpath(os.path.join(_HERE, "..", "services", "pipeline.py"))
_FUNCS_TO_EXTRACT = ["_estimate_history_tokens", "_build_mode_history"]


def _extract_helpers():
    pipeline_src = open(_PIPELINE, encoding="utf-8").read()
    chat_src = open(_CHAT, encoding="utf-8").read()

    ns = {"_dlog": lambda *a, **kw: None}

    # Pull the real _clean_history_content + HISTORY_WINDOW straight from
    # pipeline.py -- the fix under test reuses these verbatim, don't
    # reimplement them here or the test would drift from reality.
    tree = ast.parse(pipeline_src)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_clean_history_content":
            segment = ast.get_source_segment(pipeline_src, node)
            exec(segment, ns)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "HISTORY_WINDOW":
                    segment = ast.get_source_segment(pipeline_src, node)
                    exec(segment, ns)
    assert "_clean_history_content" in ns, "Failed to extract _clean_history_content from pipeline.py"
    assert "HISTORY_WINDOW" in ns, "Failed to extract HISTORY_WINDOW from pipeline.py"

    # `json` is used inside _clean_history_content.
    exec("import json", ns)

    # Now extract the two functions under test from chat.py.
    chat_tree = ast.parse(chat_src)
    for node in ast.iter_child_nodes(chat_tree):
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCS_TO_EXTRACT:
            segment = ast.get_source_segment(chat_src, node)
            exec(segment, ns)
    for fn_name in _FUNCS_TO_EXTRACT:
        assert fn_name in ns, f"Failed to extract {fn_name} from routers/chat.py"

    mod = types.SimpleNamespace(**{k: v for k, v in ns.items() if k in _FUNCS_TO_EXTRACT})
    return mod, ns["HISTORY_WINDOW"]


H, HISTORY_WINDOW = _extract_helpers()


def _surgical_result_blob(size_hint=2000):
    import json as _json
    return "__SURGICAL_RESULT__:" + _json.dumps({
        "plan": {"summary": "x" * size_hint},
        "changes_by_file": {"f.py": {"changes": [{"symbol": {"name": "s"}}] * 50}},
    })


# ── _build_mode_history ───────────────────────────────────────────────────
def test_windows_to_history_window_limit():
    """29 raw messages (production shape) must be capped to HISTORY_WINDOW."""
    blob = _surgical_result_blob()
    history = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": (f"user turn {i} " * 50) if i % 2 == 0 else blob}
        for i in range(29)
    ]
    result = H._build_mode_history(history)
    assert len(result) <= HISTORY_WINDOW, f"expected <= {HISTORY_WINDOW}, got {len(result)}"


def test_strips_raw_surgical_result_json():
    """Uncleaned __SURGICAL_RESULT__ blobs must never reach the model verbatim."""
    blob = _surgical_result_blob()
    history = [{"role": "assistant", "content": blob}]
    result = H._build_mode_history(history)
    assert len(result) == 1
    assert "__SURGICAL_RESULT__" not in result[0]["content"]


def test_per_message_cap_enforced():
    history = [{"role": "user", "content": "x" * 10000}]
    result = H._build_mode_history(history)
    assert len(result[0]["content"]) <= 4000


def test_small_normal_history_passes_through_unchanged():
    small = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "what's next?"},
    ]
    result = H._build_mode_history(small)
    assert result == small


def test_empty_and_wrong_role_messages_dropped():
    edge = [
        {"role": "user", "content": "   "},
        {"role": "system", "content": "should be dropped"},
        {"role": "assistant", "content": "kept"},
    ]
    result = H._build_mode_history(edge)
    assert result == [{"role": "assistant", "content": "kept"}]


def test_production_scale_char_reduction():
    """Directly mirrors the real failure: verifies drastic size reduction."""
    blob = _surgical_result_blob(size_hint=20000)
    history = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": (f"turn {i}") if i % 2 == 0 else blob}
        for i in range(29)
    ]
    raw_chars = sum(len(str(h["content"])) for h in history)
    result = H._build_mode_history(history)
    windowed_chars = sum(len(m["content"]) for m in result)
    assert windowed_chars < raw_chars, "windowed+cleaned history must be smaller than raw"
    # 20 msgs * 4000 char cap = 80,000 chars max (~20k tokens), well under
    # Anthropic's context limits -- structurally impossible to reproduce the
    # 1.35M-token failure after this fix.
    assert windowed_chars <= HISTORY_WINDOW * 4000


# ── _estimate_history_tokens ───────────────────────────────────────────────
def test_estimate_uses_cleaned_content_not_raw():
    """The exact production shape: 8 messages (matches d8f0ed39 msg_count)."""
    blob = _surgical_result_blob()
    history = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": (f"user turn {i}") if i % 2 == 0 else blob}
        for i in range(8)
    ]
    raw_estimate = sum(len(str(h.get("content") or "")) for h in history) // 4
    fixed_estimate = H._estimate_history_tokens(history)
    assert fixed_estimate < raw_estimate, "cleaned estimate must be smaller than raw"


def test_estimate_caps_per_message():
    history = [{"role": "user", "content": "x" * 100000}]
    assert H._estimate_history_tokens(history) == 1000  # 4000 chars // 4


def test_estimate_empty_history_is_zero():
    assert H._estimate_history_tokens([]) == 0


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
