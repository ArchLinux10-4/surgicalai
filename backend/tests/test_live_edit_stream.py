"""Live edit stream: partial JSON in, stable diff events out."""
import json
import random

from services.grok_agent_tools import StreamedToolCallAccumulator
from services.live_edit_stream import LiveEditStreamer


class _Sym:
    def __init__(self, name, code, start_line=1, end_line=1):
        self.name = name
        self.full_path = name
        self.code = code
        self.start_line = start_line
        self.end_line = end_line


class _Map:
    def __init__(self, symbols):
        self.symbols = symbols


def _streamer(**kwargs):
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("min_chars", 0)
    return LiveEditStreamer(
        kwargs.pop("symbol_maps", {}),
        kwargs.pop("file_contents", {}),
        **kwargs,
    )


def _assemble(events, field):
    return "".join(
        ev.get("text") or ""
        for ev in events
        if ev.get("op") == "delta" and ev.get("field") == field
    )


def _events(raw, kind="edit", key=1, **kwargs):
    streamer = _streamer(**kwargs)
    buf = ""
    events = []
    for ch in raw:
        buf += ch
        events.extend(streamer.feed(key, kind, buf))
    events.extend(streamer.close(key, raw))
    return events


def _no_lone_surrogate(text):
    for ch in text:
        code = ord(ch)
        assert not (0xD800 <= code <= 0xDFFF), repr(text)


def test_symbol_edit_reassembles_byte_by_byte():
    code = 'export function Widget() {\n  return <div className="x">Hi</div>\n}\n'
    new = 'export function Widget() {\n  return <div className="x">Hello</div>\n}\n'
    raw = json.dumps({
        "filename": "Widget.tsx",
        "symbol": "Widget",
        "description": "swap the greeting",
        "new_code": new,
    })
    maps = {"Widget.tsx": (_Map([_Sym("Widget", code, 1, 3)]), None)}
    events = _events(raw, symbol_maps=maps)
    assert _assemble(events, "new_code") == new
    opened = next(ev for ev in events if ev["op"] == "open")
    closed = next(ev for ev in events if ev["op"] == "close")
    assert opened["mode"] == "symbol"
    assert opened["original"] == code
    assert closed["mode"] == "symbol"
    assert closed["original"] == code
    assert opened["id"].startswith("le_")


def test_random_chunks_match_full_text():
    new = "alpha\nbeta\tgamma\\delta\n"
    raw = json.dumps({"filename": "a.py", "symbol": "missing", "new_code": new})
    rng = random.Random(0)
    streamer = _streamer()
    events = []
    i = 0
    while i < len(raw):
        i = min(len(raw), i + rng.randint(1, 17))
        events.extend(streamer.feed(1, "edit", raw[:i]))
    events.extend(streamer.close(1, raw))
    assert _assemble(events, "new_code") == new


def test_escapes_split_at_every_position_stay_a_prefix():
    text = "a\nb\t\"c\\d/é\U0001f600"
    raw = json.dumps({"filename": "a.py", "symbol": "Nope", "new_code": text})
    assert json.loads(raw)["new_code"] == text
    acc = ""
    streamer = _streamer()
    buf = ""
    for ch in raw:
        buf += ch
        events = streamer.feed(1, "edit", buf)
        for ev in events:
            if ev.get("op") == "delta":
                _no_lone_surrogate(ev.get("text") or "")
        acc += _assemble(events, "new_code")
        assert text.startswith(acc)
    events = streamer.close(1, raw)
    for ev in events:
        if ev.get("op") == "delta":
            _no_lone_surrogate(ev.get("text") or "")
    acc += _assemble(events, "new_code")
    assert acc == text

    for cut in range(0, len(raw), 3):
        fresh = _streamer()
        events = fresh.feed(1, "edit", raw[:cut])
        events += fresh.feed(1, "edit", raw)
        events += fresh.close(1, raw)
        assert _assemble(events, "new_code") == text
        for ev in events:
            if ev.get("op") == "delta":
                _no_lone_surrogate(ev.get("text") or "")


def test_key_order_and_close_corrects_mode():
    raw = json.dumps({
        "new_code": "after\n",
        "filename": "a.py",
        "symbol": "Widget",
        "old_code": "before\n",
    })
    maps = {"a.py": (_Map([_Sym("Widget", "before\n")]), None)}
    events = _events(raw, symbol_maps=maps)
    assert _assemble(events, "new_code") == "after\n"
    assert _assemble(events, "old_code") == "before\n"
    closed = next(ev for ev in events if ev["op"] == "close")
    assert closed["mode"] == "snippet"
    assert closed["original"] is None


def test_lines_win_over_old_code_on_close():
    raw = json.dumps({
        "filename": "a.py",
        "old_code": "x",
        "new_code": "y",
        "edit_start_line": 2,
        "edit_end_line": 2,
    })
    events = _events(raw, file_contents={"a.py": "a\nb\nc\n"})
    closed = next(ev for ev in events if ev["op"] == "close")
    assert closed["mode"] == "lines"
    assert closed["original"] == "b"
    assert closed["start_line"] == 2
    assert closed["end_line"] == 2


def test_new_file_streams_content():
    content = "export const a = 1\n"
    raw = json.dumps({
        "filename": "n.ts",
        "language": "ts",
        "summary": "a constant",
        "content": content,
    })
    events = _events(raw, kind="new_file")
    assert _assemble(events, "content") == content
    opened = next(ev for ev in events if ev["op"] == "open")
    assert opened["kind"] == "new_file"
    assert opened["mode"] == "new_file"
    assert opened["original"] is None


def test_unresolved_symbol_has_no_original():
    raw = json.dumps({"filename": "a.py", "symbol": "Missing", "new_code": "z = 1\n"})
    events = _events(raw, symbol_maps={})
    opened = next(ev for ev in events if ev["op"] == "open")
    closed = next(ev for ev in events if ev["op"] == "close")
    assert opened["mode"] == "unresolved"
    assert opened["original"] is None
    assert closed["mode"] == "unresolved"
    assert _assemble(events, "new_code") == "z = 1\n"


def test_original_truncated_at_300_lines():
    code = "\n".join(f"L{i}" for i in range(1, 320))
    raw = json.dumps({"filename": "a.py", "symbol": "Big", "new_code": "x"})
    maps = {"a.py": (_Map([_Sym("Big", code, 1, 319)]), None)}
    events = _events(raw, symbol_maps=maps)
    opened = next(ev for ev in events if ev["op"] == "open")
    assert opened["original_truncated"] is True
    assert opened["original"].splitlines() == [f"L{i}" for i in range(1, 301)]


def test_garbage_emits_nothing_and_does_not_raise():
    streamer = _streamer()
    for raw in ("", "hello", "{", '{"filename":', "```json", "I mentioned <surgical_edit> in prose"):
        assert streamer.feed(1, "edit", raw) == []
        assert streamer.close(1, raw) == []
    fenced = "```json\n" + json.dumps({
        "filename": "a.py", "symbol": "Nope", "new_code": "ok\n",
    }) + "\n```"
    events = _events(fenced)
    assert _assemble(events, "new_code") == "ok\n"
    nested = json.dumps({"filename": "a.py", "meta": {"a": 1}, "new_code": "z"})
    assert _assemble(_events(nested), "new_code") == "z"


def test_throttle_coalesces_and_close_flushes():
    streamer = LiveEditStreamer({}, {}, min_interval=0.08, min_chars=512)
    part = '{"filename":"a.py","new_code":"'
    first = streamer.feed(1, "edit", part, now=10.0)
    assert any(ev["op"] == "open" for ev in first)
    held = streamer.feed(1, "edit", part + "ab", now=10.01)
    assert held == []
    flushed = streamer.close(1, part + 'ab"}')
    assert _assemble(first + flushed, "new_code") == "ab"
    grown = part + ("c" * 600)
    streamer2 = LiveEditStreamer({}, {}, min_interval=0.08, min_chars=512)
    streamer2.feed(2, "edit", part, now=1.0)
    assert streamer2.feed(2, "edit", grown, now=1.01)
    assert streamer2.feed(2, "edit", grown + "Z", now=1.02) == []
    assert streamer2.feed(2, "edit", grown + "Z", now=1.20)


def test_byte_cap_stops_deltas():
    raw = json.dumps({"filename": "a.py", "symbol": "Nope", "new_code": "a" * 100})
    streamer = _streamer(max_bytes=30)
    events = streamer.feed(1, "edit", raw) + streamer.close(1, raw)
    text = _assemble(events, "new_code")
    assert 0 < len(text.encode("utf-8")) <= 30
    assert any(ev.get("capped") for ev in events)


def test_disabled_streamer_is_silent():
    raw = json.dumps({"filename": "a.py", "new_code": "z"})
    streamer = _streamer(enabled=False)
    assert streamer.feed(1, "edit", raw) == []
    assert streamer.close(1, raw) == []
    assert streamer.discard_all("x") == []


def test_discard_all_is_once_and_only_this_turn():
    raw = json.dumps({"filename": "a.py", "symbol": "Nope", "new_code": "z"})
    streamer = _streamer()
    streamer.set_turn(1)
    streamer.feed(1, "edit", raw)
    streamer.close(1, raw)
    dropped = streamer.discard_all("Preflight found 1 problem(s), asking for a fix")
    assert len(dropped) == 1
    assert dropped[0]["op"] == "discard"
    assert dropped[0]["id"].startswith("le_1_")
    assert streamer.discard_all("again") == []
    streamer.set_turn(2)
    assert streamer.discard_all("next") == []
    streamer.feed(1, "edit", raw)
    only = streamer.discard_all("Held until a plan exists")
    assert len(only) == 1
    assert only[0]["id"].startswith("le_2_")


def test_grok_arguments_match_tag_path():
    raw = json.dumps({
        "filename": "a.py",
        "symbol": "Nope",
        "description": "n",
        "new_code": "value = 1\n",
    })
    tag = _events(raw, key=1)
    grok = _events(raw, key=("grok", 0))
    assert tag == grok


def test_lookup_failure_is_swallowed():
    class Boom(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("nope")

    logs = []

    def dlog(event, **kwargs):
        logs.append((event, kwargs))

    raw = json.dumps({
        "filename": "a.py",
        "edit_start_line": 1,
        "edit_end_line": 1,
        "new_code": "z",
    })
    streamer = LiveEditStreamer({}, Boom(), min_interval=0, min_chars=0, dlog=dlog)
    assert streamer.feed(1, "edit", raw) == []
    assert streamer.feed(1, "edit", raw) == []
    assert len(logs) == 1
    assert logs[0][0] == "live_edit_error"


def test_accumulator_peek_appends_arguments():
    acc = StreamedToolCallAccumulator()
    acc.add_delta([{
        "index": 0,
        "id": "c1",
        "function": {"name": "write_surgical_edit", "arguments": '{"filename":'},
    }])
    acc.add_delta([{"index": 0, "function": {"arguments": '"a.py"}'}}])
    acc.add_delta([{
        "index": 1,
        "id": "c2",
        "function": {"name": "request_file", "arguments": '["b.py"]'},
    }])
    assert acc.peek() == [
        (0, "write_surgical_edit", '{"filename":"a.py"}'),
        (1, "request_file", '["b.py"]'),
    ]
    names = [call["name"] for call in acc.finalize()]
    assert names == ["write_surgical_edit", "request_file"]
