"""Structured tool-activity events for every agent loop.

Pure helpers: no imports from pipeline.py. Loops call a ToolTracker and yield
the dicts it returns through their own SSE wrapper. A ToolTrail on the router
side folds those events into the compact list persisted with the message.
"""
import json
import re
import time

EVENT_TYPE = "tool_activity"
MAX_ROWS = 40
MAX_TARGET = 160
MAX_SUMMARY = 160
MAX_PREVIEW = 400

_KINDS = {
    "file", "search", "repo", "github", "history", "preview", "web",
    "git", "aws", "mcp", "edit", "qa", "step",
}

_REDACTIONS = (
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"xai-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
)

_FILE_HDR = re.compile(r"FILE:\s*(\S+)\s*\((\d+)\s*lines\)")
_ANY_LINES = re.compile(r"(\d+)\s+lines")

_FAIL_PREFIXES = ("FILE NOT FOUND", "Could not", "The user rejected")
_FAIL_CONTAINS = ("was not valid", "not available in this mode", "Unknown tool")

_GH_LABELS = {
    "list_repos": ("Listing repositories", "Listed repositories"),
    "list_prs": ("Listing pull requests", "Listed pull requests"),
    "get_pr": ("Reading a pull request", "Read a pull request"),
    "get_pr_diff": ("Reading a pull request diff", "Read a pull request diff"),
    "get_pr_comments": ("Reading pull request comments", "Read pull request comments"),
    "list_issues": ("Listing issues", "Listed issues"),
    "get_issue": ("Reading an issue", "Read an issue"),
    "get_issue_comments": ("Reading issue comments", "Read issue comments"),
    "diff_branches": ("Comparing branches", "Compared branches"),
    "list_branches": ("Listing branches", "Listed branches"),
    "list_commits": ("Listing commits", "Listed commits"),
    "list_files": ("Listing files on GitHub", "Listed files on GitHub"),
    "read_file": ("Reading a file from GitHub", "Read a file from GitHub"),
    "search_code": ("Searching GitHub code", "Searched GitHub code"),
    "check_deploy": ("Checking deploy status", "Checked deploy status"),
    "push_files": ("Pushing files", "Pushed files"),
    "push_session_file": ("Pushing a file", "Pushed a file"),
}

_REPO_LABELS = {
    "git_status": ("Checking git status", "Checked git status"),
    "git_diff": ("Reading the diff", "Read the diff"),
    "git_log": ("Reading git history", "Read git history"),
    "list_repo_files": ("Listing repository files", "Listed repository files"),
    "read_repo_file": ("Reading a repository file", "Read a repository file"),
    "search_repo": ("Searching the repository", "Searched the repository"),
}

_prefix_n = 0


def _auto_prefix() -> str:
    """A short unique prefix so two streams sharing one trail never reuse ids."""
    global _prefix_n
    _prefix_n += 1
    n = _prefix_n
    chars = []
    while n:
        n, rem = divmod(n - 1, 26)
        chars.append(chr(97 + rem))
    return "".join(reversed(chars))


def canonical_kind(raw) -> str:
    k = str(raw or "").strip().lower()
    if k in ("filereq", "file", "file_request"):
        return "file"
    if k in ("websearch", "web_search", "web"):
        return "web"
    if k in _KINDS:
        return k
    return "step"


def redact(text) -> str:
    s = "" if text is None else str(text)
    for rx in _REDACTIONS:
        s = rx.sub("[redacted]", s)
    return s


def _clip(text, limit: int) -> str:
    s = redact(text).strip()
    if len(s) <= limit:
        return s
    if limit <= 1:
        return s[:limit]
    return s[: limit - 1] + "…"


def _as_data(data):
    """Dict, list, or a JSON string of either. Anything else stays a string."""
    if isinstance(data, (dict, list)):
        return data
    if isinstance(data, str):
        s = data.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = json.loads(s)
            except Exception:
                return data
            if isinstance(parsed, (dict, list)):
                return parsed
        return data
    return "" if data is None else str(data)


def _names(value, limit=3) -> str:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ""
    items = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    if not items:
        return ""
    head = items[:limit]
    extra = len(items) - len(head)
    target = ", ".join(head)
    if extra:
        target = f"{target} +{extra}"
    return _clip(target, MAX_TARGET)


def _invalid(data) -> bool:
    return isinstance(data, dict) and "_invalid" in data


def describe_request(kind, data):
    """Plain-English label and a short target for one tool call.

    Returns None when the payload is marked invalid (the model will retry,
    so there is nothing to show) or when description itself fails.
    """
    try:
        return _describe(kind, data)
    except Exception:
        return None


def _describe(kind, data):
    parsed = _as_data(data)
    if _invalid(parsed):
        return None
    ck = canonical_kind(kind)
    label = done = ""
    target = ""
    tool = ""

    if ck == "search":
        label, done = "Searching the code", "Searched the code"
        terms = parsed.get("terms") if isinstance(parsed, dict) else None
        if terms is None and isinstance(parsed, list):
            terms = parsed
        if isinstance(parsed, str):
            terms = [parsed]
        target = _names(terms or [])
    elif ck == "file":
        label, done = "Reading files", "Read files"
        names = parsed
        if isinstance(parsed, dict):
            names = parsed.get("filenames") or parsed.get("files") or []
        target = _names(names)
    elif ck == "github":
        tool = parsed.get("tool", "") if isinstance(parsed, dict) else ""
        label, done = _GH_LABELS.get(str(tool), ("Calling GitHub", "Called GitHub"))
        args = parsed.get("args") if isinstance(parsed, dict) and isinstance(parsed.get("args"), dict) else {}
        owner = str(args.get("owner") or "")
        repo = str(args.get("repo") or "")
        repo_s = f"{owner}/{repo}".strip("/") if (owner or repo) else ""
        path = str(args.get("path") or "")
        number = args.get("number") or args.get("pull_number") or args.get("issue_number") or ""
        bits = [b for b in (repo_s, path, f"#{number}" if number else "") if b]
        target = _clip(" ".join(bits), MAX_TARGET)
    elif ck == "history":
        label, done = "Checking file history", "Checked file history"
        name = parsed.get("filename", "") if isinstance(parsed, dict) else parsed
        target = _clip(name, MAX_TARGET)
    elif ck == "repo":
        tool = parsed.get("tool", "") if isinstance(parsed, dict) else ""
        label, done = _REPO_LABELS.get(str(tool), ("Reading the repository", "Read the repository"))
        if isinstance(parsed, dict):
            target = _names(parsed.get("terms")) or _clip(parsed.get("path") or "", MAX_TARGET)
    elif ck == "git":
        label, done = "Running git", "Ran git"
        cmd = parsed if isinstance(parsed, str) else ""
        if isinstance(parsed, dict):
            cmd = str(parsed.get("command") or "")
        target = _clip(cmd, MAX_TARGET)
    elif ck == "aws":
        label, done = "Running an AWS command", "Ran an AWS command"
        cmd = parsed if isinstance(parsed, str) else ""
        if isinstance(parsed, dict):
            cmd = str(parsed.get("command") or "")
        lines = cmd.splitlines()
        if lines and lines[0].lower().startswith("intent:"):
            lines = lines[1:]
        target = _clip(" ".join(part.strip() for part in lines if part.strip()), MAX_TARGET)
    elif ck == "preview":
        label, done = "Checking the preview", "Checked the preview"
        files = parsed.get("files") if isinstance(parsed, dict) else parsed
        target = _names(files)
    elif ck == "web":
        label, done = "Searching the web", "Searched the web"
        query = parsed.get("query", "") if isinstance(parsed, dict) else parsed
        target = _clip(query, MAX_TARGET)
    elif ck == "mcp":
        server = parsed.get("server", "") if isinstance(parsed, dict) else ""
        tool = parsed.get("tool", "") if isinstance(parsed, dict) else ""
        label = f"Calling {server}" if server else "Calling a connected tool"
        done = f"Called {server}" if server else "Called a connected tool"
        target = _clip(tool, MAX_TARGET)
    else:
        label, done = "Working", "Done"

    return {
        "kind": ck,
        "label": label,
        "done_label": done,
        "target": target,
        "tool": str(tool or ""),
    }


def _is_failure(note: str) -> bool:
    text = (note or "").lstrip()
    if text.startswith(_FAIL_PREFIXES):
        return True
    return any(marker in text for marker in _FAIL_CONTAINS)


def _first_line(note: str) -> str:
    for line in (note or "").splitlines():
        stripped = line.strip()
        if stripped:
            return _clip(stripped, MAX_SUMMARY)
    return ""


def _preview_lines(note: str, count: int = 4) -> str:
    lines = []
    for line in (note or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lines.append(_clip(stripped, 160))
        if len(lines) >= count:
            break
    return _clip("\n".join(lines), MAX_PREVIEW)


def summarize_result(kind, note):
    """(ok, summary, preview). File bodies never leave this function."""
    try:
        return _summarize(kind, note)
    except Exception:
        return True, "", ""


def _summarize(kind, note):
    text = "" if note is None else str(note)
    ok = not _is_failure(text)
    ck = canonical_kind(kind)
    if ck == "file":
        headers = _FILE_HDR.findall(text)
        if headers:
            total = sum(int(n) for _, n in headers)
            noun = "file" if len(headers) == 1 else "files"
            summary = f"Loaded {len(headers)} {noun} ({total} lines)"
            if not ok:
                summary = _first_line(text) or summary
            preview = "\n".join(f"{name} ({n} lines)" for name, n in headers[:4])
            return ok, _clip(summary, MAX_SUMMARY), _clip(preview, MAX_PREVIEW)
        counts = _ANY_LINES.findall(text)
        if counts and ok:
            total = sum(int(n) for n in counts[:20])
            noun = "file" if len(counts) == 1 else "files"
            return True, _clip(f"Loaded {len(counts)} {noun} ({total} lines)", MAX_SUMMARY), ""
        return ok, (_first_line(text) or ("Read files" if ok else "Could not read files")), ""
    summary = _first_line(text)
    preview = _preview_lines(text) if ok else ""
    return ok, summary, preview


def note_at(messages, idx):
    """The tool result note appended at ``idx + 1``.

    Dispatch appends ``[assistant echo, user note]`` starting at the saved
    index. A steering message may be appended after that pair; reading a
    fixed index (not the last message) still returns the note.
    """
    try:
        if idx is None:
            return ""
        i = idx + 1
        if i < 0 or i >= len(messages):
            return ""
        message = messages[i]
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    except Exception:
        return ""
    return ""


def _cap(rows: list) -> list:
    if len(rows) <= MAX_ROWS:
        return rows
    hidden = len(rows) - 5 - 34
    placeholder = {
        "id": "more",
        "kind": "step",
        "label": f"{hidden} more steps",
        "done_label": f"{hidden} more steps",
        "target": "",
        "status": "done",
        "ok": True,
        "ms": None,
        "summary": "",
        "preview": "",
    }
    return rows[:5] + [placeholder] + rows[-34:]


class ToolTracker:
    """One per stream. Emits start / update / end dicts. Never raises."""

    def __init__(self, agent: str = "", id_prefix: str | None = None, dlog=None):
        self.agent = agent or ""
        self._prefix = _auto_prefix() if id_prefix is None else id_prefix
        self._dlog = dlog
        self._n = 0
        self._open: dict = {}
        self._order: list = []
        self._exit: dict = {}
        self._pending_ends: list = []
        self._kind_ids: dict = {}
        self._logged = False

    def _next_id(self) -> str:
        self._n += 1
        return f"{self._prefix}t{self._n}"

    def _guard(self, fn):
        try:
            return fn()
        except Exception as exc:
            if not self._logged:
                self._logged = True
                if self._dlog:
                    try:
                        self._dlog("tool_activity_error", error=str(exc)[:200])
                    except Exception:
                        pass
            return None

    def start(self, kind, data, *, turn=None):
        def go():
            desc = describe_request(kind, data)
            if not desc:
                return None
            rid = self._next_id()
            self._open[rid] = {"t0": time.time(), "kind": desc["kind"]}
            self._order.append(rid)
            event = {
                "type": EVENT_TYPE,
                "id": rid,
                "phase": "start",
                "kind": desc["kind"],
                "label": desc["label"],
                "done_label": desc["done_label"],
                "target": desc.get("target") or "",
                "tool": desc.get("tool") or "",
                "agent": self.agent,
            }
            if turn is not None:
                event["turn"] = turn
            return event
        return self._guard(go)

    def phase(self, kind, label, done_label, target=""):
        def go():
            rid = self._next_id()
            ck = canonical_kind(kind)
            self._open[rid] = {"t0": time.time(), "kind": ck}
            self._order.append(rid)
            return {
                "type": EVENT_TYPE,
                "id": rid,
                "phase": "start",
                "kind": ck,
                "label": label,
                "done_label": done_label,
                "target": _clip(target, MAX_TARGET),
                "tool": "",
                "agent": self.agent,
            }
        return self._guard(go)

    def _latest_open(self):
        for rid in reversed(self._order):
            if rid in self._open:
                return rid
        return None

    def observe(self, ev):
        def go():
            if not isinstance(ev, dict):
                return None
            rid = self._latest_open()
            if not rid:
                return None
            kind = ev.get("type")
            if kind in ("git_approval_needed", "aws_approval_needed", "mcp_approval_needed"):
                return {"type": EVENT_TYPE, "id": rid, "phase": "update", "status": "waiting"}
            if kind in ("git_cli", "aws_cli", "mcp_call"):
                if ev.get("exit_code") is not None:
                    try:
                        self._exit[rid] = int(ev.get("exit_code")) == 0
                    except Exception:
                        pass
                if isinstance(ev.get("ok"), bool):
                    self._exit[rid] = ev["ok"]
                return {"type": EVENT_TYPE, "id": rid, "phase": "update", "status": "running"}
            return None
        return self._guard(go)

    def retarget(self, target):
        """Update the open row's target (QA correction rounds)."""
        def go():
            rid = self._latest_open()
            if not rid:
                return None
            return {
                "type": EVENT_TYPE,
                "id": rid,
                "phase": "update",
                "target": _clip(target, MAX_TARGET),
            }
        return self._guard(go)

    def end(self, note: str = "", ok=None):
        def go():
            rid = self._latest_open()
            if not rid:
                return None
            return self._end_id(rid, note, ok)
        return self._guard(go)

    def _end_id(self, rid, note, ok):
        info = self._open.pop(rid, None)
        if not info:
            return None
        ms = int(max(0, (time.time() - info["t0"]) * 1000))
        success, summary, preview = summarize_result(info["kind"], note or "")
        remembered = self._exit.pop(rid, None)
        if ok is None and remembered is not None:
            ok = remembered
        if ok is False:
            success = False
        elif ok is True:
            success = True
        return {
            "type": EVENT_TYPE,
            "id": rid,
            "phase": "end",
            "ok": success,
            "ms": ms,
            "summary": summary,
            "preview": preview,
        }

    def close_all(self, status: str = "stopped"):
        def go():
            out = []
            for rid in list(self._order):
                if rid not in self._open:
                    continue
                info = self._open.pop(rid, None)
                if not info:
                    continue
                ms = int(max(0, (time.time() - info["t0"]) * 1000))
                out.append({
                    "type": EVENT_TYPE,
                    "id": rid,
                    "phase": "end",
                    "ok": None,
                    "status": status,
                    "ms": ms,
                    "summary": "Stopped",
                    "preview": "",
                })
            self._pending_ends = []
            return out
        return self._guard(go) or []

    def begin_tags(self, sr=None, fr=None, gr=None, hr=None, rr=None):
        """Start one row per tag match, in search/file/github/history/repo order."""
        events = []
        for kind, match in (("search", sr), ("file", fr), ("github", gr), ("history", hr), ("repo", rr)):
            if match is None:
                continue
            try:
                body = match.group(1)
            except Exception:
                body = ""
            event = self.start(kind, body)
            if event:
                self._kind_ids[canonical_kind(kind)] = event["id"]
                events.append(event)
        return events

    def finish(self, kind, note):
        """Record an end for the row begin_tags opened for this kind."""
        try:
            rid = self._kind_ids.get(canonical_kind(kind))
            if not rid or rid not in self._open:
                return
            event = self._end_id(rid, note, None)
            if event:
                self._pending_ends.append(event)
        except Exception as exc:
            if not self._logged:
                self._logged = True
                if self._dlog:
                    try:
                        self._dlog("tool_activity_error", error=str(exc)[:200])
                    except Exception:
                        pass

    def drain_ends(self):
        out = self._pending_ends
        self._pending_ends = []
        return out


class ToolTrail:
    """Fold a stream of tool_activity events into rows safe to persist."""

    def __init__(self):
        self._rows: dict = {}
        self._order: list = []

    def observe(self, ev):
        try:
            self._observe(ev)
        except Exception:
            return

    def _observe(self, ev):
        if not isinstance(ev, dict) or ev.get("type") != EVENT_TYPE:
            return
        rid = ev.get("id")
        if not isinstance(rid, str) or not rid:
            return
        phase = ev.get("phase")
        if phase == "start":
            if rid in self._rows:
                return
            row = {
                "id": rid,
                "kind": ev.get("kind") or "step",
                "label": ev.get("label") or "",
                "done_label": ev.get("done_label") or ev.get("label") or "",
                "target": ev.get("target") or "",
                "status": "running",
                "ok": None,
                "ms": None,
                "summary": "",
                "preview": "",
            }
            if ev.get("task_id"):
                row["task_id"] = ev["task_id"]
            self._rows[rid] = row
            self._order.append(rid)
            return
        row = self._rows.get(rid)
        if row is None:
            return
        if ev.get("task_id") and "task_id" not in row:
            row["task_id"] = ev["task_id"]
        if phase == "update":
            if ev.get("status") in ("waiting", "running"):
                row["status"] = ev["status"]
            if ev.get("target"):
                row["target"] = ev["target"]
            return
        if phase == "end":
            if ev.get("status") == "stopped" or ev.get("summary") == "Stopped":
                row["status"] = "stopped"
            elif ev.get("ok") is False:
                row["status"] = "failed"
            else:
                row["status"] = "done"
            row["ok"] = ev.get("ok")
            if isinstance(ev.get("ms"), (int, float)):
                row["ms"] = int(ev["ms"])
            if ev.get("summary"):
                row["summary"] = ev["summary"]
            if ev.get("preview"):
                row["preview"] = ev["preview"]

    def rows(self):
        out = []
        for rid in self._order:
            row = dict(self._rows[rid])
            if row.get("status") in ("running", "waiting"):
                row["status"] = "stopped"
                row["summary"] = row.get("summary") or "Stopped"
            if not row.get("task_id"):
                row.pop("task_id", None)
            out.append(row)
        return _cap(out)
