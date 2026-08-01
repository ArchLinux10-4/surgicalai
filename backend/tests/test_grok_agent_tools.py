"""Unit tests for the Grok native tool-calling adapter (grok_agent_tools).

Run from backend/:  pytest tests/test_grok_agent_tools.py
"""
import json
import pytest

from services import grok_agent_tools as gat


# ── Fakes that mimic the OpenAI/xAI streamed SDK delta objects ──────────────
class FakeFunction:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class FakeToolCallDelta:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = FakeFunction(name, arguments)


def _logs():
    """Return (dlog, events) where events collects every logged event name."""
    events = []

    def dlog(event, **kw):
        events.append(event)
    return dlog, events


# ─────────────────────────────────────────────────────────────────────────────
# build_grok_agent_tools — schema/mode gates
# ─────────────────────────────────────────────────────────────────────────────
def test_edit_mode_has_write_tools():
    dlog, events = _logs()
    tools = gat.build_grok_agent_tools("edit", dlog=dlog)
    names = {t["function"]["name"] for t in tools}
    assert gat.TOOL_WRITE_SURGICAL_EDIT in names
    assert gat.TOOL_WRITE_NEW_FILE in names
    assert gat.TOOL_WRITE_EDIT_PLAN in names
    assert gat.TOOL_REQUEST_FILE in names
    assert gat.TOOL_REQUEST_SEARCH in names
    assert gat.TOOL_REPORT_BLOCKED in names
    # gated off by default
    assert gat.TOOL_REQUEST_GITHUB not in names
    assert gat.TOOL_REQUEST_HISTORY not in names
    assert "grok_build_tools_entry" in events and "grok_build_tools_done" in events


def test_agent_mode_same_as_edit_plus_gated():
    tools = gat.build_grok_agent_tools("agent", github_enabled=True, history_enabled=True)
    names = {t["function"]["name"] for t in tools}
    assert gat.TOOL_REQUEST_GITHUB in names
    assert gat.TOOL_REQUEST_HISTORY in names
    assert gat.TOOL_WRITE_SURGICAL_EDIT in names


def test_ask_plan_modes_have_no_write_tools():
    for mode in ("ask", "plan"):
        tools = gat.build_grok_agent_tools(mode)
        names = {t["function"]["name"] for t in tools}
        assert gat.TOOL_WRITE_SURGICAL_EDIT not in names
        assert gat.TOOL_WRITE_NEW_FILE not in names
        assert gat.TOOL_WRITE_EDIT_PLAN not in names
        # read tools still present
        assert gat.TOOL_REQUEST_FILE in names
        assert gat.TOOL_REQUEST_SEARCH in names


def test_all_schemas_are_valid_openai_function_shape():
    tools = gat.build_grok_agent_tools("agent", github_enabled=True, history_enabled=True)
    for t in tools:
        assert t["type"] == "function"
        fn = t["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert isinstance(params.get("required", []), list)
    # tool names must not collide with xAI reserved built-ins
    names = {t["function"]["name"] for t in tools}
    assert not (names & {"web_search", "x_search", "code_execution", "code_interpreter"})


# ─────────────────────────────────────────────────────────────────────────────
# StreamedToolCallAccumulator — fragmented, indexed deltas
# ─────────────────────────────────────────────────────────────────────────────
def test_accumulator_merges_fragments_by_index():
    dlog, events = _logs()
    acc = gat.StreamedToolCallAccumulator(dlog=dlog)
    # first fragment: id + name + args prefix
    acc.add_delta([FakeToolCallDelta(0, id="call_abc", name="write_surgical_edit",
                                     arguments='{"filename":"a.py",')])
    # later fragments: args only, no id/name
    acc.add_delta([FakeToolCallDelta(0, arguments='"symbol":"foo",')])
    acc.add_delta([FakeToolCallDelta(0, arguments='"description":"d","new_code":"x=1"}')])
    calls = acc.finalize()
    assert len(calls) == 1
    assert calls[0]["id"] == "call_abc"
    assert calls[0]["name"] == "write_surgical_edit"
    parsed = json.loads(calls[0]["arguments"])
    assert parsed["filename"] == "a.py"
    assert parsed["new_code"] == "x=1"
    assert "grok_tc_finalize" in events


def test_accumulator_multiple_parallel_calls():
    acc = gat.StreamedToolCallAccumulator()
    acc.add_delta([FakeToolCallDelta(0, id="c0", name="write_surgical_edit", arguments='{"a":')])
    acc.add_delta([FakeToolCallDelta(1, id="c1", name="write_new_file", arguments='{"b":')])
    acc.add_delta([FakeToolCallDelta(0, arguments='1}')])
    acc.add_delta([FakeToolCallDelta(1, arguments='2}')])
    calls = acc.finalize()
    assert [c["id"] for c in calls] == ["c0", "c1"]
    assert json.loads(calls[0]["arguments"]) == {"a": 1}
    assert json.loads(calls[1]["arguments"]) == {"b": 2}


def test_accumulator_synthesizes_missing_id():
    acc = gat.StreamedToolCallAccumulator()
    acc.add_delta([FakeToolCallDelta(0, name="request_file", arguments='{"filenames":["x"]}')])
    calls = acc.finalize()
    assert calls[0]["id"]  # non-empty synthesized id


def test_accumulator_handles_dict_deltas():
    acc = gat.StreamedToolCallAccumulator()
    acc.add_delta([{"index": 0, "id": "d0", "function": {"name": "report_blocked",
                                                         "arguments": '{"reason":"x"}'}}])
    calls = acc.finalize()
    assert calls[0]["name"] == "report_blocked"


# ─────────────────────────────────────────────────────────────────────────────
# translate_tool_calls — each tool type -> producer shapes
# ─────────────────────────────────────────────────────────────────────────────
def _call(name, args, cid="c1"):
    return {"id": cid, "name": name,
            "arguments": args if isinstance(args, str) else json.dumps(args)}


def test_translate_surgical_edit_appends_json_string():
    dlog, events = _logs()
    args = {"filename": "app.py", "symbol": "run", "description": "fix",
            "new_code": "def run():\n    return 1", "old_code": "def run(): pass"}
    res = gat.translate_tool_calls([_call("write_surgical_edit", args)], dlog=dlog)
    assert len(res.edit_json_strings) == 1
    # exactly the shape edit_blocks_raw expects: a JSON string decoding to the args
    decoded = json.loads(res.edit_json_strings[0])
    assert decoded["filename"] == "app.py"
    assert decoded["new_code"].startswith("def run()")
    assert res.produced_any()
    assert "grok_translate_surgical_edit" in events


def test_translate_surgical_edit_missing_new_code_is_error():
    res = gat.translate_tool_calls([_call("write_surgical_edit",
                                          {"filename": "a.py", "symbol": "s", "description": "d"})])
    assert not res.edit_json_strings
    assert res.errors and "new_code" in res.errors[0][1]


def test_translate_new_file():
    args = {"filename": "new.ts", "language": "typescript", "summary": "s",
            "content": "export const x = 1;"}
    res = gat.translate_tool_calls([_call("write_new_file", args)])
    assert len(res.new_file_json_strings) == 1
    assert json.loads(res.new_file_json_strings[0])["content"].startswith("export")


def test_translate_new_file_missing_content_is_error():
    res = gat.translate_tool_calls([_call("write_new_file",
                                          {"filename": "n.ts", "language": "ts", "summary": "s"})])
    assert not res.new_file_json_strings
    assert res.errors


def test_translate_edit_plan_to_list():
    args = {"steps": [
        {"filename": "a.py", "symbol": "f", "description": "d1"},
        {"filename": "b.py", "symbol": "g", "description": "d2"},
        {"symbol": "no_filename"},  # dropped
    ]}
    res = gat.translate_tool_calls([_call("write_edit_plan", args)])
    assert isinstance(res.edit_plan, list)
    assert len(res.edit_plan) == 2
    assert res.edit_plan[0]["filename"] == "a.py"


def test_translate_request_file_context_tuple():
    res = gat.translate_tool_calls([_call("request_file", {"filenames": ["a.py", "b.py"]})])
    assert res.context_request is not None
    kind, body = res.context_request
    assert kind == "filereq"
    assert json.loads(body) == ["a.py", "b.py"]
    assert res.context_call_id == "c1"


def test_translate_request_search_context_tuple():
    res = gat.translate_tool_calls([_call("request_search",
                                          {"terms": ["foo", "bar"], "reason": "why"})])
    kind, body = res.context_request
    assert kind == "search"
    payload = json.loads(body)
    assert payload["terms"] == ["foo", "bar"]
    assert payload["reason"] == "why"


def test_translate_request_github_context_tuple():
    res = gat.translate_tool_calls([_call("request_github",
                                          {"tool": "read_file", "args": {"path": "x"}})])
    kind, body = res.context_request
    assert kind == "github"
    payload = json.loads(body)
    assert payload["tool"] == "read_file"
    assert payload["args"] == {"path": "x"}


def test_translate_request_history_context_tuple():
    res = gat.translate_tool_calls([_call("request_history",
                                          {"filename": "a.py", "query": "old"})])
    kind, body = res.context_request
    assert kind == "history"
    payload = json.loads(body)
    assert payload["filename"] == "a.py"
    assert payload["query"] == "old"


def test_translate_report_blocked_terminal():
    res = gat.translate_tool_calls([_call("report_blocked", {"reason": "need file X"})])
    assert res.blocked_reason == "need file X"
    assert res.produced_any()


def test_translate_only_first_context_request_dispatched():
    calls = [
        _call("request_search", {"terms": ["a"]}, cid="c1"),
        _call("request_file", {"filenames": ["b.py"]}, cid="c2"),
    ]
    res = gat.translate_tool_calls(calls)
    assert res.context_request[0] == "search"      # first wins
    assert res.context_call_id == "c1"
    assert any(cid == "c2" for cid, _ in res.errors)  # second recorded as no-op


def test_translate_multiple_writes_all_collected():
    calls = [
        _call("write_surgical_edit",
              {"filename": "a.py", "symbol": "s1", "description": "d",
               "new_code": "x=1"}, cid="c1"),
        _call("write_surgical_edit",
              {"filename": "b.py", "symbol": "s2", "description": "d",
               "new_code": "y=2"}, cid="c2"),
    ]
    res = gat.translate_tool_calls(calls)
    assert len(res.edit_json_strings) == 2


def test_translate_malformed_json_is_error_not_crash():
    res = gat.translate_tool_calls([_call("write_surgical_edit", '{"filename": "a.py", broken')])
    assert not res.edit_json_strings
    assert res.errors


def test_translate_html_entity_encoded_arguments():
    # xAI has been observed HTML-entity-encoding argument strings.
    raw = json.dumps({"filename": "a.py", "symbol": "run", "description": "use && and quotes",
                      "new_code": 'if a && b: print("x")'})
    encoded = raw.replace("&", "&amp;").replace('"', "&quot;")
    res = gat.translate_tool_calls([_call("write_surgical_edit", encoded)])
    # grok_provider.parse_tool_call_arguments should HTML-decode and parse it.
    assert len(res.edit_json_strings) == 1, f"errors={res.errors}"
    decoded = json.loads(res.edit_json_strings[0])
    assert "&&" in decoded["new_code"]


def test_translate_unknown_tool_is_error():
    res = gat.translate_tool_calls([_call("totally_made_up", {"x": 1})])
    assert res.errors
    assert not res.produced_any()


# ─────────────────────────────────────────────────────────────────────────────
# Native tool-result conversation messages
# ─────────────────────────────────────────────────────────────────────────────
def test_assistant_tool_calls_message_never_null_content():
    calls = [{"id": "c1", "name": "request_file", "arguments": '{"filenames":["a"]}'}]
    msg = gat.build_assistant_tool_calls_message(None, calls)
    assert msg["role"] == "assistant"
    assert msg["content"] == ""  # xAI 400s on null content + tool_calls
    assert msg["tool_calls"][0]["id"] == "c1"
    assert msg["tool_calls"][0]["type"] == "function"
    assert msg["tool_calls"][0]["function"]["name"] == "request_file"


def test_tool_result_messages_one_per_call_with_matching_ids():
    calls = [
        {"id": "c1", "name": "write_surgical_edit", "arguments": "{}"},
        {"id": "c2", "name": "request_file", "arguments": "{}"},
    ]
    results = {"c1": "Surgical edit recorded."}
    msgs = gat.build_tool_result_messages(calls, results, context_call_id="c2",
                                          context_result="FILE CONTENTS HERE")
    assert [m["tool_call_id"] for m in msgs] == ["c1", "c2"]
    assert all(m["role"] == "tool" for m in msgs)
    assert msgs[0]["content"] == "Surgical edit recorded."
    assert msgs[1]["content"] == "FILE CONTENTS HERE"


def test_normalize_dispatch_pair_rewrites_echo_into_native():
    dlog, events = _logs()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "searching..."},   # echo
        {"role": "user", "content": "Here are the search results: ..."},  # observation
    ]
    native_turn = {
        "calls": [{"id": "s1", "name": "request_search", "arguments": '{"terms":["x"]}'}],
        "results_by_id": {},
        "context_call_id": "s1",
        "assistant_text": "searching...",
    }
    rebuilt = gat.normalize_dispatch_pair(messages, native_turn, dlog=dlog)
    # last two (echo+observation) replaced by assistant(tool_calls)+tool result
    assert rebuilt[-2]["role"] == "assistant"
    assert rebuilt[-2]["tool_calls"][0]["id"] == "s1"
    assert rebuilt[-1]["role"] == "tool"
    assert rebuilt[-1]["tool_call_id"] == "s1"
    assert "search results" in rebuilt[-1]["content"]
    assert "grok_normalize_applied" in events


def test_normalize_dispatch_pair_no_calls_is_noop():
    messages = [{"role": "user", "content": "x"}]
    out = gat.normalize_dispatch_pair(messages, {"calls": []})
    assert out == messages


# ─────────────────────────────────────────────────────────────────────────────
# System-prompt projection
# ─────────────────────────────────────────────────────────────────────────────
def test_system_suffix_edit_mentions_native_tools_not_tags():
    suffix = gat.build_grok_system_suffix("edit")
    assert "write_surgical_edit" in suffix
    assert "NATIVE TOOL" in suffix
    # explicitly tells the model to NOT emit XML tags as text
    assert "<surgical_edit>" in suffix and "do NOT" in suffix


def test_system_suffix_ask_has_no_write_tools():
    suffix = gat.build_grok_system_suffix("ask")
    assert "write_surgical_edit" not in suffix
    assert "request_file" in suffix


def test_agent_instruction_edit_mode_is_native():
    instr = gat.build_grok_agent_instruction("edit")
    assert "write_surgical_edit" in instr
    assert "report_blocked" in instr


# ─────────────────────────────────────────────────────────────────────────────
# Logging contract — every public fn logs entry/success
# ─────────────────────────────────────────────────────────────────────────────
def test_every_public_fn_logs():
    dlog, events = _logs()
    gat.build_grok_agent_tools("edit", dlog=dlog)
    gat.translate_tool_calls([_call("request_file", {"filenames": ["a"]})], dlog=dlog)
    gat.build_grok_system_suffix("edit", dlog=dlog)
    gat.build_grok_agent_instruction("edit", dlog=dlog)
    for expected in ("grok_build_tools_entry", "grok_translate_entry",
                     "grok_translate_done", "grok_system_suffix_built",
                     "grok_agent_instruction_built"):
        assert expected in events, f"missing log {expected}"
