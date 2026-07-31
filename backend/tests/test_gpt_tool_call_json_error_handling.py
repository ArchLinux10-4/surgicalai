"""
Regression test for malformed/truncated tool-call-argument JSON handling in
the two GPT native-tool-calling loops inside services/pipeline.py:

  1. run_smart_pipeline_stream's OAI_TOOL_USE search loop      (~line 11774)
  2. analyze_and_plan_stream's Agent-Mode GPT correction loop  (~line 6836)

Background (researched via OpenAI community forum threads, GitHub issues, and
an academic empirical study of Stack Overflow / GitHub AI-agent issues, all
fetched live — see gpt-agent-top-issues-research-and-audit.md):

    "Malformed / truncated tool-call arguments JSON" is one of the most
    commonly reported production failure modes for GPT-style function/tool
    calling agents (OpenAI community threads #693997, #427479, #1293374;
    industry write-up waxell.ai calls it "the #1 production problem"). The
    documented failure isn't just the parse exception itself — it's silently
    treating the bad JSON as an empty dict and letting the tool "succeed" on
    garbage input, so the model never learns anything went wrong and repeats
    or compounds the mistake.

    Both loops above already caught `json.JSONDecodeError`, but on catch they
    did exactly that: `_otc_input = {}` / `_cotc_input = {}` and fell through
    to the normal per-tool dispatch. For `search_codebase` with empty
    `terms`, that produced the misleading "All requested terms already
    searched." — implying success when nothing was searched. For
    `submit_fix`/`request_symbol_code`, it degraded to a generic (and
    misleading) "Symbol '' not found" instead of telling the model its JSON
    was invalid.

Fix verified here (this test executes the ACTUAL committed source lines —
read fresh from services/pipeline.py at test time via `exec()`, not
hand-retyped logic) makes both loops:
  - short-circuit tool dispatch entirely on a JSON parse failure,
  - emit an explicit "[ERROR] ... were not valid JSON ... this call did not
    run" tool result so the model can self-correct next turn,
  - log a `_dlog`/`dlog` event carrying the tool name and parse error,
  - continue behaving exactly as before for syntactically valid JSON
    (including the pre-existing "already searched" message, which must NOT
    fire on a parse failure).
"""
import json
import pathlib
import re
import sys
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
PIPELINE_SRC = (REPO_ROOT / "services" / "pipeline.py").read_text()
sys.path.insert(0, str(REPO_ROOT))


def _extract_lines(start: int, end: int) -> str:
    """1-indexed, inclusive — exactly like read_file's start_line/end_line."""
    lines = PIPELINE_SRC.splitlines()
    return "\n".join(lines[start - 1:end])


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id_, name, arguments):
        self.id = id_
        self.function = _FakeFunction(name, arguments)


# ─────────────────────────────────────────────────────────────────────────
# Site 1: run_smart_pipeline_stream OAI_TOOL_USE search loop (~11774-11807)
# ─────────────────────────────────────────────────────────────────────────

def _run_site1(tool_call, dlog_events):
    """Extract the real for-loop body from pipeline.py and execute it against
    a single fake tool call. Returns the computed `_otc_result`."""
    anchor = "                for _otc in _oai_tool_calls:\n                    _otc_name = _otc.function.name\n                    _otc_parse_failed = False"
    start_idx = PIPELINE_SRC.index(anchor)
    start_line = PIPELINE_SRC.count("\n", 0, start_idx) + 1
    end_anchor = '_otc_result = "All requested terms already searched. Call submit_plan with what you have."'
    end_idx = PIPELINE_SRC.index(end_anchor)
    end_line = PIPELINE_SRC.count("\n", 0, end_idx) + 1

    src = _extract_lines(start_line, end_line)
    # Sanity: make sure we actually captured the parse-failure branch and the
    # comparison "already searched" branch, and NOT some unrelated block.
    assert "_otc_parse_failed = True" in src
    assert "json.JSONDecodeError" in src
    assert "already searched" in src

    src = textwrap.dedent(src.replace("                for _otc in", "for _otc in", 1))
    fn_src = "def _harness(_oai_tool_calls, _oai_searched_terms, _dlog, session_id, _oai_round):\n" \
        + textwrap.indent(src, "    ") + "\n    return _otc_result\n"

    ns = {"json": json}
    exec(compile(fn_src, "<pipeline.py extract: site1>", "exec"), ns)
    return ns["_harness"](
        [tool_call], set(),
        lambda event, **kw: dlog_events.append((event, kw)),
        "sess-1", 1,
    )


def test_site1_malformed_json_yields_explicit_error_not_misleading_success():
    events = []
    bad_call = _FakeToolCall("tc1", "search_codebase", '{"terms": ["foo"'.rstrip())  # truncated
    result = _run_site1(bad_call, events)

    assert result.startswith("[ERROR]"), f"expected explicit error, got: {result!r}"
    assert "search_codebase" in result
    assert "did not run" in result
    assert "already searched" not in result, (
        "malformed JSON must NOT fall through to the misleading "
        "'already searched' success-shaped message"
    )

    assert events, "parse failure must be logged via _dlog"
    event_name, kw = events[0]
    assert event_name == "oai_tool_use_args_json_error"
    assert kw["tool"] == "search_codebase"
    assert kw["session_id"] == "sess-1"


def test_site1_empty_terms_valid_json_still_gets_original_already_searched_message():
    """Contrast case: syntactically VALID JSON with genuinely empty terms
    must keep behaving exactly as before (unaffected by the fix)."""
    events = []
    ok_call = _FakeToolCall("tc2", "search_codebase", json.dumps({"terms": []}))
    result = _run_site1(ok_call, events)

    assert result == "All requested terms already searched. Call submit_plan with what you have."
    assert not events, "no parse-error dlog should fire for valid JSON"


# ─────────────────────────────────────────────────────────────────────────
# Site 2: analyze_and_plan_stream Agent-Mode GPT correction loop (~6836-6862)
# ─────────────────────────────────────────────────────────────────────────

def _run_site2(tool_calls, dlog_events):
    anchor = "                                    for _cotc in _corr_oai_tcalls:"
    start_idx = PIPELINE_SRC.index(anchor)
    start_line = PIPELINE_SRC.count("\n", 0, start_idx) + 1
    end_anchor = '"summary": "..."'  # not present; use structural marker instead
    # Grab from the for-loop start through just after the parse-error branch's
    # `continue`, plus the following `if _cotc_name == "done_fixing":` check
    # (real code) so we can prove valid JSON still reaches real dispatch.
    marker = "                                        elif _cotc_name == \"request_symbol_code\":"
    marker_idx = PIPELINE_SRC.index(marker)
    end_line = PIPELINE_SRC.count("\n", 0, marker_idx) + 1 - 1  # line just before marker

    src = _extract_lines(start_line, end_line)
    assert "_cotc_pe" in src
    assert "continue" in src
    assert 'agent_mode_gpt_correction_args_json_error' in src
    assert '"done_fixing"' in src

    src = textwrap.dedent(src.replace(
        "                                    for _cotc in", "for _cotc in", 1))
    fn_src = (
        "def _harness(_corr_oai_tcalls, _dlog, session_id, _corr_turn):\n"
        "    _corr_oai_tool_results = []\n"
        "    _corr_done = False\n"
        "    _corr_changes = []\n"
        + textwrap.indent(src, "    ")
        + "\n    return _corr_oai_tool_results, _corr_done\n"
    )
    ns = {"json": json}
    exec(compile(fn_src, "<pipeline.py extract: site2>", "exec"), ns)
    return ns["_harness"](
        tool_calls, lambda event, **kw: dlog_events.append((event, kw)), "sess-2", 0,
    )


def test_site2_malformed_json_appends_explicit_tool_error_and_skips_dispatch():
    events = []
    bad_call = _FakeToolCall("tc3", "submit_fix", '{"symbol_path": "Foo.bar", "new_code": "def bar():\\n    ret')  # truncated mid-string
    results, done = _run_site2([bad_call], events)

    assert done is False
    assert len(results) == 1
    tool_msg = results[0]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "tc3"
    payload = json.loads(tool_msg["content"])
    assert "not valid JSON" in payload["error"]
    assert "submit_fix" in payload["error"]
    assert "did not run" in payload["hint"]
    # Must NOT look like the old misleading "Symbol '' not found" degradation.
    assert "not found" not in payload["error"]

    assert events, "parse failure must be logged"
    event_name, kw = events[0]
    assert event_name == "agent_mode_gpt_correction_args_json_error"
    assert kw["tool"] == "submit_fix"
    assert kw["session_id"] == "sess-2"


def test_site2_valid_json_done_fixing_still_reaches_real_dispatch():
    """Contrast case: syntactically valid JSON for a known tool must still
    reach the real (untouched) dispatch logic, proving the fix only
    intercepts genuine parse failures."""
    events = []
    ok_call = _FakeToolCall("tc4", "done_fixing", json.dumps({"summary": "all good"}))
    results, done = _run_site2([ok_call], events)

    assert done is True, "valid done_fixing call must still set _corr_done=True"
    assert len(results) == 1
    assert json.loads(results[0]["content"]) == {"status": "ok"}
    assert all(e != "agent_mode_gpt_correction_args_json_error" for e, _ in events), (
        "no parse-error dlog should fire for valid JSON"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
