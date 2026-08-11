"""Thin architect/plan-execute checkpoints → apply-ready changes_by_file.

Evidence: sessions 414dfaef (architect disconnect) + 97224670 (plan-execute
disconnect) recovered only markdown; resolution/QA already enrich via
checkpoint_recovery. This suite covers the thin→rich adapter those phases use.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

from services.checkpoint_recovery import (  # noqa: E402
    CHECKPOINT_FORMAT_VERSION,
    entries_from_thin_resolved,
    enrich_thin_checkpoint_payload,
    diff_has_real_body,
)
from services.pipeline import _make_diff  # noqa: E402


def _sym(name="foo", code="def foo():\n    return 1\n", parent=None):
    return SimpleNamespace(
        name=name,
        symbol_type="function",
        start_line=1,
        end_line=2,
        parent=parent,
        indentation=0,
        code=code,
        signature=f"def {name}()",
        full_path=f"{parent}.{name}" if parent else name,
    )


def test_entries_from_thin_resolves_via_lookup():
    thin = [{
        "filename": "app.py",
        "symbol": "foo",
        "description": "tweak",
        "new_code": "def foo():\n    return 2\n",
    }]
    called = []

    def lookup(fn, sn):
        called.append((fn, sn))
        return _sym()

    entries = entries_from_thin_resolved(thin, lookup_symbol=lookup)
    assert called == [("app.py", "foo")]
    assert len(entries) == 1
    assert entries[0]["filename"] == "app.py"
    assert entries[0]["symbol"].name == "foo"
    assert entries[0]["new_code"].startswith("def foo")


def test_entries_skip_lookup_miss_without_inventing():
    thin = [{
        "filename": "app.py",
        "symbol": "missing",
        "description": "",
        "new_code": "def missing():\n    pass\n",
    }]
    entries = entries_from_thin_resolved(thin, lookup_symbol=lambda f, s: None)
    assert entries == []


def test_new_file_thin_item_gets_empty_original_symbol():
    thin = [{
        "filename": "NewComp.tsx",
        "symbol": "(new file)",
        "description": "add component",
        "new_code": "export function NewComp() {\n  return null\n}\n",
    }]
    entries = entries_from_thin_resolved(
        thin,
        lookup_symbol=lambda f, s: (_ for _ in ()).throw(
            AssertionError("should not lookup")
        ),
    )
    assert len(entries) == 1
    assert entries[0]["symbol"]["code"] == ""
    assert entries[0]["new_code"].startswith("export function")


def test_enrich_thin_attaches_changes_by_file_and_format_version():
    thin = [{
        "filename": "app.py",
        "symbol": "foo",
        "description": "tweak",
        "new_code": "def foo():\n    return 2\n",
    }]
    payload = {"phase": "architect", "resolved": thin}
    enrich_thin_checkpoint_payload(
        payload,
        thin,
        lookup_symbol=lambda f, s: _sym(),
        make_diff=_make_diff,
        resolve_file_id=lambda fn: "file-id-1",
    )
    assert payload["format_version"] == CHECKPOINT_FORMAT_VERSION
    assert "app.py" in payload["changes_by_file"]
    changes = payload["changes_by_file"]["app.py"]["changes"]
    assert len(changes) == 1
    ch = changes[0]
    assert ch["id"]
    assert ch["original_code"]
    assert ch["new_code"]
    assert diff_has_real_body(ch["diff"])
    assert payload["changes_by_file"]["app.py"]["file_id"] == "file-id-1"
    # thin resolved preserved
    assert payload["resolved"] == thin


def test_enrich_thin_leaves_payload_thin_when_nothing_resolves():
    thin = [{
        "filename": "app.py",
        "symbol": "ghost",
        "description": "",
        "new_code": "x",
    }]
    payload = {"phase": "plan_execute", "resolved": thin}
    enrich_thin_checkpoint_payload(
        payload,
        thin,
        lookup_symbol=lambda f, s: None,
        make_diff=_make_diff,
    )
    assert "changes_by_file" not in payload
    assert payload["resolved"] == thin


def test_safety_net_structured_path_uses_changes_by_file_envelope():
    """Mirror chat.py safety_net branch: changes_by_file → NATURAL_AND_RESULT."""
    thin = [{
        "filename": "app.py",
        "symbol": "foo",
        "description": "tweak",
        "new_code": "def foo():\n    return 2\n",
    }]
    ckpt = {"phase": "architect", "resolved": thin}
    enrich_thin_checkpoint_payload(
        ckpt, thin,
        lookup_symbol=lambda f, s: _sym(),
        make_diff=_make_diff,
        resolve_file_id=lambda fn: "fid",
    )
    assert ckpt.get("changes_by_file")
    # Same construction as chat.py ~1509–1523
    rec_n = sum(len(v.get("changes", [])) for v in ckpt["changes_by_file"].values())
    rec_text = (
        f"⚡ **Connection was interrupted** before I could finish, but I recovered "
        f"{rec_n} completed edit(s) below — review and apply them, or re-send "
        f"the prompt to finish the rest."
    )
    rec_result = {
        "intent": "edit",
        "summary": f"Recovered {rec_n} edit(s) from an interrupted run",
        "reasoning": "Recovered from a connection interruption before the run finished.",
        "risks": [],
        "skipped_changes": [],
        "changes_by_file": ckpt["changes_by_file"],
        "new_files": [],
        "natural_text": rec_text,
        "recovered": True,
    }
    saved = "__NATURAL_AND_RESULT__:" + json.dumps({"text": rec_text, "result": rec_result})
    assert saved.startswith("__NATURAL_AND_RESULT__:")
    body = json.loads(saved[len("__NATURAL_AND_RESULT__:"):])
    assert body["result"]["recovered"] is True
    assert body["result"]["changes_by_file"]["app.py"]["changes"][0]["diff"]
