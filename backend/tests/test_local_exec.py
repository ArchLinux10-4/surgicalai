"""run_local classification and approval. The helper is faked."""
from __future__ import annotations

import asyncio
import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services import local_exec  # noqa: E402


def _level(body, root):
    parsed = local_exec.parse_local_body(body)
    assert "error" not in parsed, parsed
    found = local_exec.classify(parsed["action"], parsed, root)
    return found.get("error") or found["level"]


def test_level_table(tmp_path):
    root = str(tmp_path)
    assert _level("action: exec\nls", root) == "read"
    assert _level("action: exec\ncat README.md", root) == "read"
    assert _level("action: read\npath: README.md", root) == "read"
    assert _level("action: exec\nnpm test", root) == "change"
    assert _level("action: exec\nnpm install left-pad", root) == "network"
    assert _level("action: exec\nrm README.md", root) == "delete"
    assert _level("action: exec\ncat .env", root) == "protected"
    assert _level("action: write\npath: /etc/passwd\n---\nnope", root) == "protected"
    assert _level("action: exec\ncurl https://example.com", root) == "network"


def test_rejects_chaining_redirects_and_other_tools(tmp_path):
    root = str(tmp_path)
    assert "single command" in local_exec.parse_local_body("action: exec\nls && pwd")["error"]
    assert "single command" in local_exec.parse_local_body("action: exec\nls > out.txt")["error"]
    assert local_exec.REDIRECT_NOTE in _level("action: exec\ngit status", root)
    assert local_exec.REDIRECT_NOTE in _level("action: exec\naws s3 ls", root)
    assert "sudo" in _level("action: exec\nsudo ls", root)
    assert _level("action: exec\nmake build", root) == "change"


def test_parse_forms():
    write = local_exec.parse_local_body("action: write\npath: scratch-note.txt\n---\nhello")
    assert write["content"] == "hello"
    assert "error" in local_exec.parse_local_body("action: write\npath: a.txt\nhello")
    assert "error" in local_exec.parse_local_body("action: nope\nls")
    read = local_exec.parse_local_body("action: read\npath: a.txt\nlines: 1-20")
    assert read["offset"] == 1
    assert read["limit"] == 20


def test_allowlist_skips_change_but_not_delete_or_shell(tmp_path):
    root = str(tmp_path)
    rules = [["npm", "test"]]
    allowed = local_exec.begin_local_call("action: exec\nnpm test", "edit", 0, root, rules)
    assert allowed["run"] is True
    assert allowed["needs_approval"] is False
    delete = local_exec.begin_local_call("action: exec\nrm README.md", "edit", 0, root, [["rm"]])
    assert delete["needs_approval"] is True
    shell = local_exec.begin_local_call('action: exec\nbash -lc "npm test"', "edit", 0, root, [["bash", "-lc"]])
    assert shell["needs_approval"] is True
    assert shell["shell"] is True


def test_ask_plan_is_read_only(tmp_path):
    root = str(tmp_path)
    read = local_exec.begin_local_call("action: exec\nls", "ask", 0, root, [])
    assert read["run"] is True
    write = local_exec.begin_local_call("action: exec\nnpm test", "plan", 0, root, [])
    assert write["run"] is False
    assert write["note"] == local_exec.ASK_PLAN_MUTATION_NOTE
    assert write["needs_approval"] is False
    capped = local_exec.begin_local_call("action: exec\nls", "ask", 3, root, [])
    assert capped["note"] == local_exec.ASK_PLAN_STOP_NOTE


def test_write_summary_is_a_diff(tmp_path, monkeypatch):
    root = str(tmp_path)
    (tmp_path / "scratch-note.txt").write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(local_exec, "local_agent_enabled", lambda: True)
    monkeypatch.setattr(local_exec, "load_allowlist", lambda user_id: [])
    monkeypatch.setattr(local_exec, "_call_helper", lambda *a, **k: {
        "content": "old\n", "sha": "abc", "binary": False,
    })
    inbox = asyncio.Queue()

    async def go():
        task = asyncio.create_task(_drain(local_exec.resolve_local_call(
            "action: write\npath: scratch-note.txt\n---\nnew\n",
            "edit", 0, inbox, user_id="user-a", session_id="s1", root=root, timeout=2,
        )))
        await asyncio.sleep(0.05)
        await inbox.put({"type": "local_cli_response", "action": "reject"})
        return await task

    events = asyncio.run(go())
    card = next(ev for ev in events if ev["type"] == "local_approval_needed")
    assert "-old" in card["summary"]
    assert "+new" in card["summary"]


async def _drain(gen):
    events = []
    async for event in gen:
        events.append(event)
    return events


def _enable(monkeypatch, helper):
    monkeypatch.setattr(local_exec, "local_agent_enabled", lambda: True)
    monkeypatch.setattr(local_exec, "local_available", lambda user_id="": True)
    monkeypatch.setattr(local_exec, "load_allowlist", lambda user_id: [])
    monkeypatch.setattr(local_exec, "_call_helper", helper)


def test_approve_runs_the_stored_command(tmp_path, monkeypatch):
    root = str(tmp_path)
    ran = []

    def helper(endpoint, payload, timeout=120):
        ran.append((endpoint, payload.get("argv")))
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}

    _enable(monkeypatch, helper)
    inbox = asyncio.Queue()

    async def go():
        task = asyncio.create_task(_drain(local_exec.resolve_local_call(
            "action: exec\nnpm test", "edit", 0, inbox,
            user_id="user-a", session_id="s1", root=root, timeout=2,
        )))
        await asyncio.sleep(0.05)
        await inbox.put({
            "type": "local_cli_response", "action": "approve", "command": "rm -rf /",
        })
        return await task

    events = asyncio.run(go())
    assert ran == [("/exec", ["npm", "test"])]
    assert events[-1]["type"] == "local_done"
    assert events[-1]["run"] is True
    assert any(ev.get("type") == "local_approval_needed" for ev in events)


def test_reject_timeout_and_no_inbox(tmp_path, monkeypatch):
    root = str(tmp_path)
    _enable(monkeypatch, lambda *a, **k: {"exit_code": 0, "stdout": "", "stderr": ""})
    inbox = asyncio.Queue()

    async def reject():
        task = asyncio.create_task(_drain(local_exec.resolve_local_call(
            "action: exec\nnpm test", "agent", 0, inbox,
            user_id="user-a", session_id="s1", root=root, timeout=2,
        )))
        await asyncio.sleep(0.05)
        await inbox.put({"type": "local_cli_response", "action": "reject"})
        return await task

    rejected = asyncio.run(reject())
    assert rejected[-1]["run"] is False
    assert rejected[-1]["note"] == local_exec.REJECT_NOTE

    async def expire():
        return await _drain(local_exec.resolve_local_call(
            "action: exec\nnpm test", "edit", 0, asyncio.Queue(),
            user_id="user-a", session_id="s1", root=root, timeout=0.2,
        ))

    timed = asyncio.run(expire())
    assert timed[-1]["note"] == local_exec.TIMEOUT_NOTE

    async def missing():
        return await _drain(local_exec.resolve_local_call(
            "action: exec\nnpm test", "edit", 0, None,
            user_id="user-a", session_id="s1", root=root, timeout=0.2,
        ))

    assert asyncio.run(missing())[-1]["note"] == local_exec.NO_INBOX_NOTE


def test_edit_that_raises_the_level_reprompts(tmp_path, monkeypatch):
    root = str(tmp_path)
    ran = []
    _enable(monkeypatch, lambda *a, **k: ran.append(1) or {"exit_code": 0, "stdout": "", "stderr": ""})
    inbox = asyncio.Queue()

    async def go():
        task = asyncio.create_task(_drain(local_exec.resolve_local_call(
            "action: exec\nnpm test", "edit", 0, inbox,
            user_id="user-a", session_id="s1", root=root, timeout=2,
        )))
        await asyncio.sleep(0.05)
        await inbox.put({"type": "local_cli_response", "action": "edit", "command": "rm README.md"})
        await asyncio.sleep(0.05)
        await inbox.put({"type": "local_cli_response", "action": "reject"})
        return await task

    events = asyncio.run(go())
    cards = [ev for ev in events if ev["type"] == "local_approval_needed"]
    assert len(cards) == 2
    assert cards[1]["level"] == "delete"
    assert ran == []
    assert events[-1]["note"] == local_exec.REJECT_NOTE


def test_four_card_cap(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(local_exec, "level_is_worse", lambda old, new: True)
    _enable(monkeypatch, lambda *a, **k: {"exit_code": 0, "stdout": "", "stderr": ""})
    inbox = asyncio.Queue()

    async def feed():
        for _ in range(4):
            await asyncio.sleep(0.05)
            await inbox.put({"type": "local_cli_response", "action": "edit", "command": "rm README.md"})

    async def go():
        task = asyncio.create_task(_drain(local_exec.resolve_local_call(
            "action: exec\nnpm test", "edit", 0, inbox,
            user_id="user-a", session_id="s1", root=root, timeout=2,
        )))
        await feed()
        return await task

    events = asyncio.run(go())
    assert sum(1 for ev in events if ev["type"] == "local_approval_needed") == 4
    assert events[-1]["note"] == local_exec.REJECT_NOTE
    assert events[-1]["run"] is False


def test_hosted_mode_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr("database.USE_POSTGRES", True)
    monkeypatch.setattr(local_exec, "local_agent_enabled", lambda: True)

    async def go():
        return await _drain(local_exec.resolve_local_call(
            "action: exec\nls", "edit", 0, None, root=str(tmp_path),
        ))

    events = asyncio.run(go())
    assert events[-1]["note"] == "The local tool is off."
    assert events[-1]["run"] is False
