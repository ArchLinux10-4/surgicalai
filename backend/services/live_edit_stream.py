"""Stream partial edit JSON as live diff events while a model is still writing.

Claude and GPT emit ``<surgical_edit>`` / ``<new_file>`` bodies. Grok emits the
same JSON as native tool-call arguments. Both are incomplete for most of the
turn. This module reads that partial JSON without raising, and returns
``edit_live`` events the chat can render. It never applies an edit.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

_MAX_ORIGINAL_LINES = 300
_CODE_FIELDS = ("old_code", "new_code", "content")
_HEX = set("0123456789abcdefABCDEF")
_SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
}
_SKIP = object()


def _cap_text(text: Optional[str]):
    if text is None:
        return None, False
    lines = text.splitlines()
    if len(lines) <= _MAX_ORIGINAL_LINES:
        return text, False
    return "\n".join(lines[:_MAX_ORIGINAL_LINES]), True


def _span_lines(text: str, start: int, end: int) -> str:
    lines = (text or "").splitlines()
    if start < 1 or end < start or start > len(lines):
        return ""
    end = min(end, len(lines))
    return "\n".join(lines[start - 1:end])


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    return i


def _decode_string(s: str, i: int):
    """Decode a JSON string starting at the opening quote.

    Returns ``(decoded, index_after, complete)``. An unfinished escape or a
    high surrogate still waiting for its pair is left out of ``decoded`` so a
    caller can append more text later without rewriting what it already sent.
    """
    out = []
    j = i + 1
    n = len(s)
    while j < n:
        ch = s[j]
        if ch == '"':
            return "".join(out), j + 1, True
        if ch != "\\":
            out.append(ch)
            j += 1
            continue
        if j + 1 >= n:
            return "".join(out), j, False
        esc = s[j + 1]
        mapped = _SIMPLE_ESCAPES.get(esc)
        if mapped is not None:
            out.append(mapped)
            j += 2
            continue
        if esc != "u":
            out.append(esc)
            j += 2
            continue
        if j + 6 > n:
            rest = s[j + 2:]
            if all(c in _HEX for c in rest):
                return "".join(out), j, False
            out.append("\\")
            j += 1
            continue
        hexpart = s[j + 2:j + 6]
        if not all(c in _HEX for c in hexpart):
            out.append("\\")
            j += 1
            continue
        code = int(hexpart, 16)
        if 0xD800 <= code <= 0xDBFF:
            k = j + 6
            if k >= n or s[k] == "\\":
                if k >= n or k + 1 >= n:
                    return "".join(out), j, False
                if s[k + 1] == "u":
                    if k + 6 > n:
                        return "".join(out), j, False
                    lowhex = s[k + 2:k + 6]
                    if all(c in _HEX for c in lowhex):
                        low = int(lowhex, 16)
                        if 0xDC00 <= low <= 0xDFFF:
                            full = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                            out.append(chr(full))
                            j = k + 6
                            continue
            out.append(chr(code))
            j = j + 6
            continue
        out.append(chr(code))
        j += 6
    return "".join(out), j, False


def _skip_container(s: str, i: int) -> Optional[int]:
    pairs = {"{": "}", "[": "]"}
    if i >= len(s) or s[i] not in pairs:
        return None
    stack = [pairs[s[i]]]
    j = i + 1
    n = len(s)
    while j < n:
        ch = s[j]
        if ch == '"':
            _decoded, j, complete = _decode_string(s, j)
            if not complete:
                return None
            continue
        if ch in "{[":
            stack.append(pairs[ch])
        elif ch in "}]":
            if not stack or ch != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return None


def _parse_number(s: str, i: int):
    j = i
    n = len(s)
    if j < n and s[j] == "-":
        j += 1
    start = j
    while j < n and s[j].isdigit():
        j += 1
    if j == start:
        return _SKIP, None, False
    if j < n and s[j] in ".eE":
        k = j + 1
        while k < n and s[k] in "0123456789+-eE.":
            k += 1
        return _SKIP, k, True
    if j >= n:
        return _SKIP, None, False
    k = _skip_ws(s, j)
    if k < n and s[k] in ",}":
        try:
            return int(s[i:j]), j, True
        except ValueError:
            return _SKIP, j, True
    if k >= n:
        return _SKIP, None, False
    return _SKIP, j, True


def _parse_literal(s: str, i: int):
    for word in ("true", "false", "null"):
        if s.startswith(word, i):
            return _SKIP, i + len(word), True
        if word.startswith(s[i:]) and i + len(word) > len(s):
            return _SKIP, None, False
    return _SKIP, None, False


def _parse_value(s: str, i: int):
    if i >= len(s):
        return _SKIP, None, False
    ch = s[i]
    if ch == '"':
        return _decode_string(s, i)
    if ch in "{[":
        j = _skip_container(s, i)
        if j is None:
            return _SKIP, None, False
        return _SKIP, j, True
    if ch == "-" or ch.isdigit():
        return _parse_number(s, i)
    if ch in "tfn":
        return _parse_literal(s, i)
    return _SKIP, None, False


def _scan_top_level(raw: str) -> dict:
    """Top-level JSON fields of a possibly truncated object.

    Values are ``(decoded, complete)``. Incomplete strings omit a trailing
    unfinished escape. Nested objects and arrays are skipped. Non-objects
    return an empty dict.
    """
    if not isinstance(raw, str) or not raw:
        return {}
    s = raw.lstrip("\ufeff").lstrip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl == -1:
            return {}
        s = s[nl + 1:].lstrip()
    if not s.startswith("{"):
        return {}
    i = 1
    n = len(s)
    out = {}
    while i < n:
        i = _skip_ws(s, i)
        if i >= n or s[i] == "}":
            break
        if s[i] == ",":
            i += 1
            continue
        if s[i] != '"':
            break
        key, i, key_done = _decode_string(s, i)
        if not key_done:
            break
        i = _skip_ws(s, i)
        if i >= n or s[i] != ":":
            break
        i = _skip_ws(s, i + 1)
        if i >= n:
            break
        val, i, complete = _parse_value(s, i)
        if i is None:
            if val is not _SKIP:
                out[key] = (val, False)
            break
        if val is not _SKIP:
            out[key] = (val, bool(complete))
        if not complete:
            break
    return out


def _complete_str(fields: dict, name: str) -> Optional[str]:
    item = fields.get(name)
    if not item or not item[1] or not isinstance(item[0], str):
        return None
    return item[0]


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class _Block:
    def __init__(self, kind: str):
        self.kind = kind if kind in ("edit", "new_file") else "edit"
        self.id = None
        self.opened = False
        self.closed = False
        self.discarded = False
        self.mode = "unresolved"
        self.original = None
        self.original_truncated = False
        self.filename = ""
        self.symbol = ""
        self.start_line = None
        self.end_line = None
        self.sent = {}
        self.bytes_sent = 0
        self.capped = False
        self.symbol_announced = False
        self.last_emit_ts = None
        self.last_raw_len = 0


class LiveEditStreamer:
    """One agent request. ``feed`` takes the full partial body each time."""

    def __init__(
        self,
        symbol_maps,
        file_contents,
        *,
        turn: int = 0,
        enabled: bool = True,
        min_interval: float = 0.08,
        min_chars: int = 512,
        max_bytes: int = 200_000,
        dlog: Optional[Callable] = None,
    ):
        self.symbol_maps = symbol_maps if symbol_maps is not None else {}
        self.file_contents = file_contents if file_contents is not None else {}
        self.enabled = bool(enabled)
        self.min_interval = float(min_interval)
        self.min_chars = int(min_chars)
        self.max_bytes = int(max_bytes)
        self._dlog = dlog
        self._turn = int(turn or 0)
        self._seq = 0
        self._turn_keys = []
        self._states = {}
        self._logged = False

    def set_turn(self, turn) -> None:
        try:
            turn = int(turn or 0)
            if turn != self._turn:
                self._turn = turn
                self._seq = 0
                self._turn_keys = []
        except Exception as exc:
            self._log_once(exc)

    def feed(self, key, kind, raw_so_far, now=None) -> list:
        try:
            if not self.enabled:
                return []
            if not isinstance(raw_so_far, str):
                return []
            if now is None:
                now = time.monotonic()
            st = self._block(key, kind)
            if st.closed or st.discarded:
                return []
            if (
                st.last_emit_ts is not None
                and (now - st.last_emit_ts) < self.min_interval
                and (len(raw_so_far) - st.last_raw_len) < self.min_chars
            ):
                return []
            st.last_emit_ts = now
            st.last_raw_len = len(raw_so_far)
            if st.capped and st.opened:
                return []
            fields = _scan_top_level(raw_so_far)
            if not st.opened:
                if not self._can_open(fields):
                    return []
                return self._open_and_deltas(st, key, fields)
            return self._deltas(st, fields)
        except Exception as exc:
            self._log_once(exc)
            return []

    def close(self, key, raw_final, kind=None) -> list:
        try:
            if not self.enabled:
                return []
            if not isinstance(raw_final, str):
                raw_final = ""
            st = self._block(key, kind)
            if st.discarded or st.closed:
                return []
            loaded = _load_block(raw_final)
            fields = _scan_top_level(raw_final)
            events = []
            if not st.opened:
                if loaded is None and not self._can_open(fields):
                    st.closed = True
                    return []
                if loaded is not None:
                    info = self._resolve_loaded(st.kind, loaded)
                else:
                    info = self._resolve_fields(st.kind, fields)
                events.extend(self._emit_open(st, key, info))
            events.extend(self._deltas(st, fields))
            if loaded is not None:
                info = self._resolve_loaded(st.kind, loaded)
                st.mode = info["mode"]
                st.original = info["original"]
                st.original_truncated = info["original_truncated"]
                st.filename = info["filename"] or st.filename
                st.symbol = info["symbol"] or st.symbol
                st.start_line = info["start_line"]
                st.end_line = info["end_line"]
            st.closed = True
            if not st.opened:
                return events
            events.append({
                "type": "edit_live",
                "op": "close",
                "id": st.id,
                "mode": st.mode,
                "original": st.original,
                "original_truncated": st.original_truncated,
                "symbol": st.symbol or "",
                "filename": st.filename or "",
                "start_line": st.start_line,
                "end_line": st.end_line,
            })
            return events
        except Exception as exc:
            self._log_once(exc)
            return []

    def discard(self, key, reason) -> list:
        try:
            if not self.enabled:
                return []
            st = self._states.get((self._turn, key))
            if st is None or not st.opened or st.discarded:
                return []
            st.discarded = True
            return [{
                "type": "edit_live",
                "op": "discard",
                "id": st.id,
                "reason": str(reason or "Dropped")[:240],
            }]
        except Exception as exc:
            self._log_once(exc)
            return []

    def discard_all(self, reason) -> list:
        try:
            if not self.enabled:
                return []
            events = []
            for key in list(self._turn_keys):
                events.extend(self.discard(key, reason))
            return events
        except Exception as exc:
            self._log_once(exc)
            return []

    def _block(self, key, kind) -> _Block:
        slot = (self._turn, key)
        st = self._states.get(slot)
        if st is None:
            st = _Block(kind or "edit")
            self._states[slot] = st
        elif kind in ("edit", "new_file") and not st.opened:
            st.kind = kind
        return st

    def _can_open(self, fields: dict) -> bool:
        if _complete_str(fields, "filename") is None:
            return False
        return any(name in fields for name in _CODE_FIELDS)

    def _open_and_deltas(self, st: _Block, key, fields: dict) -> list:
        info = self._resolve_fields(st.kind, fields)
        events = self._emit_open(st, key, info)
        events.extend(self._deltas(st, fields))
        return events

    def _emit_open(self, st: _Block, key, info: dict) -> list:
        if st.id is None:
            st.id = f"le_{self._turn}_{self._seq}"
            self._seq += 1
        st.opened = True
        st.mode = info["mode"]
        st.original = info["original"]
        st.original_truncated = info["original_truncated"]
        st.filename = info["filename"]
        st.symbol = info["symbol"]
        st.start_line = info["start_line"]
        st.end_line = info["end_line"]
        if st.symbol:
            st.symbol_announced = True
        if key not in self._turn_keys:
            self._turn_keys.append(key)
        return [{
            "type": "edit_live",
            "op": "open",
            "id": st.id,
            "kind": st.kind,
            "filename": st.filename,
            "symbol": st.symbol,
            "mode": st.mode,
            "start_line": st.start_line,
            "end_line": st.end_line,
            "original": st.original,
            "original_truncated": st.original_truncated,
        }]

    def _deltas(self, st: _Block, fields: dict) -> list:
        events = []
        if st.capped:
            return events
        for name in _CODE_FIELDS:
            item = fields.get(name)
            if not item or not isinstance(item[0], str):
                continue
            val = item[0]
            prev = st.sent.get(name, "")
            if not val.startswith(prev):
                continue
            if len(val) == len(prev):
                continue
            chunk = val[len(prev):]
            room = self.max_bytes - st.bytes_sent
            capped = False
            if room <= 0:
                st.capped = True
                break
            encoded = chunk.encode("utf-8")
            if len(encoded) > room:
                chunk = _fit_bytes(chunk, room)
                capped = True
            if not chunk:
                st.capped = True
                break
            st.sent[name] = prev + chunk
            st.bytes_sent += len(chunk.encode("utf-8"))
            ev = {
                "type": "edit_live",
                "op": "delta",
                "id": st.id,
                "field": name,
                "text": chunk,
            }
            if capped:
                ev["capped"] = True
                st.capped = True
            if not st.symbol_announced:
                sym = _complete_str(fields, "symbol")
                if sym:
                    st.symbol = sym
                    st.symbol_announced = True
                    ev["symbol"] = sym
            events.append(ev)
            if st.capped:
                break
        if not events and not st.symbol_announced and st.opened and st.id:
            sym = _complete_str(fields, "symbol")
            if sym:
                st.symbol = sym
                st.symbol_announced = True
                events.append({
                    "type": "edit_live",
                    "op": "delta",
                    "id": st.id,
                    "field": "new_code",
                    "text": "",
                    "symbol": sym,
                })
        return events

    def _resolve_fields(self, kind: str, fields: dict) -> dict:
        complete = {}
        for name, (val, done) in fields.items():
            if done:
                complete[name] = val
        started = set(fields)
        return self._resolve(kind, complete, started)

    def _resolve_loaded(self, kind: str, loaded: dict) -> dict:
        return self._resolve(kind, loaded, set(loaded))

    def _resolve(self, kind: str, complete: dict, started: set) -> dict:
        filename = str(complete.get("filename") or "").strip()
        symbol = str(complete.get("symbol") or "").strip()
        if kind == "new_file":
            return _info("new_file", None, False, None, None, filename, symbol)
        start = _as_int(complete.get("edit_start_line"))
        end = _as_int(complete.get("edit_end_line"))
        if start is not None and end is not None:
            text = self.file_contents.get(filename, "") if self.file_contents is not None else ""
            if text is None:
                text = ""
            original, trunc = _cap_text(_span_lines(text, start, end))
            return _info("lines", original, trunc, start, end, filename, symbol)
        if "old_code" in started or "old_code" in complete:
            return _info("snippet", None, False, None, None, filename, symbol)
        if symbol:
            sym = _lookup_symbol(self.symbol_maps, filename, symbol)
            code = getattr(sym, "code", None) if sym is not None else None
            if isinstance(code, str):
                original, trunc = _cap_text(code)
                return _info(
                    "symbol",
                    original,
                    trunc,
                    getattr(sym, "start_line", None),
                    getattr(sym, "end_line", None),
                    filename,
                    symbol,
                )
        return _info("unresolved", None, False, None, None, filename, symbol)

    def _log_once(self, exc: Exception) -> None:
        if self._logged:
            return
        self._logged = True
        if not self._dlog:
            return
        try:
            self._dlog("live_edit_error", error=str(exc)[:200])
        except Exception:
            pass


def _info(mode, original, truncated, start, end, filename, symbol) -> dict:
    return {
        "mode": mode,
        "original": original,
        "original_truncated": truncated,
        "start_line": start,
        "end_line": end,
        "filename": filename,
        "symbol": symbol,
    }


def _fit_bytes(text: str, room: int) -> str:
    if room <= 0:
        return ""
    if len(text.encode("utf-8")) <= room:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].encode("utf-8")) <= room:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _lookup_symbol(symbol_maps, filename: str, symbol_name: str):
    try:
        from services.preflight import _lookup_symbol as _lookup
        return _lookup(symbol_maps, filename, symbol_name)
    except Exception:
        return None


def _load_block(raw: str):
    try:
        from services.preflight import parse_edit_block
        data = parse_edit_block(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    fields = _scan_top_level(raw)
    loaded = {}
    for key, (val, complete) in fields.items():
        if complete:
            loaded[key] = val
    return loaded or None
