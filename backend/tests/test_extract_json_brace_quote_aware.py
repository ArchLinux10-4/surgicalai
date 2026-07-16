"""
Regression test for _extract_json_from_text brace-counter bug.

Root cause (proven from session 97b7046f-bcd4-4629-a5b0-a13422a5ae5f,
/tmp/qa_log.jsonl): the brace-matching loop counted every "{" and "}"
character as structural JSON nesting, including ones that appear INSIDE
quoted JSON string values (QA prose describing broken code often contains
literal braces, e.g. "...stray '</span></div>))}' fragment..." or
"...'{ label: \"Virtual scrolling\", ... }'...").

That caused the object to be considered "closed" dozens to hundreds of
characters before the real end, producing an unparseable fragment. The
caller then fell back to _salvage_fields_from_truncated_json and mislabeled
a COMPLETE, valid model response as "[QA response truncated by max_tokens]"
even though stop_reason was actually end_turn and json.loads() on the full
raw text succeeds. Confirmed via simulation: the old counter cut the real
1855-char response off at char 897; the fix (quote/escape-aware counter)
finds the true end at char 1855 and the full object parses.

This test asserts the fix: braces inside quoted strings must not affect
depth tracking, while genuinely truncated (unterminated) responses must
still correctly fail to parse (no regression on the true-positive case).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import _extract_json_from_text  # noqa: E402


# Real raw QA response captured verbatim from session 97b7046f (event 121,
# qa_agent_raw_response). Full stop_reason for this call was "end_turn" —
# this is a complete response, not a truncated one.
REAL_CAPTURED_RESPONSE = (
    '{\n  "verdict": "blocked",\n  "qa_score": 1,\n  "summary": "The Surgeon fixed the previous header '
    "style-drop issue but introduced severe new syntax errors: duplicated/mismatched JSX blocks in the Help Modal's "
    "TAB 3 and TAB 4 sections (stray closing tags, an unterminated object literal mixing two different array "
    "structures, and duplicated 'Export Options' blocks), making the file unparsable.\",\n  \"import_issues\": [],\n  "
    '"downstream_risks": ["Component will fail to compile/parse due to malformed JSX and a broken object literal in '
    "the TAB 4 Export Options array (mixing 'Export Excel' object with a stray '{ label: \\\"Virtual scrolling\\\", "
    "... }' entry, missing closing brace), breaking the entire Dashboard component and any file that imports it.\"],\n"
    '  "type_errors": [],\n  "logic_errors": [\n    "In TAB 3 (Charts & Compare), after the Job Titles/Paid '
    "Locations/Paid Jobs .map() block closes with '))}', there is a stray leftover '</span></div>))}' fragment "
    "immediately after \u2014 invalid/duplicated JSX that will cause a parse error.\",\n    \"In TAB 4 (Table & Export), "
    "the 'Export Options' block is duplicated verbatim (entire array + .map() rendered twice back to back), and "
    "within the first duplicate the object array is malformed: an 'Export Excel' object with a trailing comma is "
    "directly followed by a mismatched entry '{ label: \\\"Virtual scrolling\\\", desc: ... }' with no closing brace "
    "on the prior object \u2014 this is a syntax error, not valid JS.\"\n  ],\n  \"plan_deviation\": \"The plan asked only "
    "to attach scoped responsive classes to the header/nav for tablet layout; the Surgeon correctly added "
    "className='dashboard-top-header' / 'dashboard-top-navigation' plus a media query, but in doing so (or via a "
    "bad merge) also corrupted unrelated help-modal JSX far outside the scope of the change, duplicating content and "
    'breaking syntax.",\n  "risk_verdicts": []\n}'
)


def test_real_captured_response_parses_fully():
    out = _extract_json_from_text(REAL_CAPTURED_RESPONSE)
    parsed = json.loads(out)  # must not raise
    assert parsed["verdict"] == "blocked"
    assert parsed["qa_score"] == 1
    # All 4 logic_errors/downstream_risks items must survive intact — the old
    # bug silently dropped everything after the premature brace closure.
    assert len(parsed["logic_errors"]) == 2
    assert len(parsed["downstream_risks"]) == 1
    assert parsed["plan_deviation"].startswith("The plan asked only")


def test_simple_json_still_works():
    out = _extract_json_from_text('{"a": 1, "b": "hello"}')
    assert json.loads(out) == {"a": 1, "b": "hello"}


def test_nested_objects_still_work():
    out = _extract_json_from_text('{"a": {"b": {"c": 1}}}')
    assert json.loads(out) == {"a": {"b": {"c": 1}}}


def test_markdown_fenced_json_still_works():
    out = _extract_json_from_text('```json\n{"a": 1}\n```')
    assert json.loads(out) == {"a": 1}


def test_preamble_and_trailing_text_still_stripped():
    out = _extract_json_from_text('Here is the result:\n{"a": 1}\nThanks!')
    assert json.loads(out) == {"a": 1}


def test_brace_inside_string_value_not_counted():
    out = _extract_json_from_text('{"summary": "the code has a stray } here", "score": 5}')
    parsed = json.loads(out)
    assert parsed["summary"] == "the code has a stray } here"
    assert parsed["score"] == 5


def test_escaped_quote_then_brace_inside_string():
    out = _extract_json_from_text('{"msg": "he said \\"hi\\" then } appeared", "ok": true}')
    parsed = json.loads(out)
    assert parsed["ok"] is True


def test_no_braces_returns_text_unchanged_and_fails_naturally():
    out = _extract_json_from_text("just some text")
    assert out == "just some text"
    try:
        json.loads(out)
        assert False, "expected JSONDecodeError"
    except json.JSONDecodeError:
        pass


def test_genuinely_truncated_response_still_fails_to_parse():
    # True positive: a response cut off mid-string by max_tokens must NOT be
    # coerced into parsing successfully — this guards against overcorrecting.
    truncated = '{"verdict": "blocked", "summary": "cut off mid string'
    out = _extract_json_from_text(truncated)
    try:
        json.loads(out)
        assert False, "expected JSONDecodeError for genuinely truncated input"
    except json.JSONDecodeError:
        pass


def test_python_style_single_quoted_dict_ast_fallback_still_works():
    out = _extract_json_from_text("{'a': 1, 'b': 'x'}")
    assert json.loads(out) == {"a": 1, "b": "x"}


def test_backslash_before_brace_in_string():
    out = _extract_json_from_text('{"path": "C:\\\\Users\\\\test} weird", "n": 1}')
    parsed = json.loads(out)
    assert parsed["n"] == 1
