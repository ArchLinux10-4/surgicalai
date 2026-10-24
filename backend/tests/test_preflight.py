"""Preflight gate fixtures.

Several cases are reduced from sessions named in the pipeline comments.
They are not dumps of those sessions. They reproduce the failure mode the
comment describes, so a future change can see the same false positive.
"""
import pytest

from services.ast_parser import ASTParser
from services.preflight import (
    apply_edits_to_text,
    block_key,
    decide_preflight,
    dedupe_latest,
    forget_superseded,
    parse_edit_block,
    restore_stash,
    run_preflight,
    stamp_grok_preflight,
    stash_dropped,
)


def _maps(filename, source):
    smap = ASTParser().parse(source, filename)
    return {filename: (smap, None)}


def _edit(filename, symbol, new_code, **extra):
    import json
    body = {"filename": filename, "symbol": symbol, "description": "test", "new_code": new_code}
    body.update(extra)
    return json.dumps(body)


def test_clean_edit_does_not_trigger():
    filename = "Widget.tsx"
    source = "export function Widget() {\n  return <div>Hi</div>\n}\n"
    raw = _edit(filename, "Widget", "export function Widget() {\n  return <div>Hello</div>\n}\n")
    report = run_preflight([raw], [], _maps(filename, source), {filename: source}, {})
    assert report["problems"] == []


def test_dd543a3a_duplicate_definition_blocks():
    filename = "Widget.tsx"
    source = "export function Widget() {\n  return <div>Hi</div>\n}\n"
    new_code = (
        "export function Widget() {\n  return <div>Hi</div>\n}\n"
        "export function Widget() {\n  return <div>Again</div>\n}\n"
    )
    report = run_preflight(
        [_edit(filename, "Widget", new_code)], [], _maps(filename, source), {filename: source}, {},
    )
    assert report["problems"]
    assert any(item["check"] == "duplicate_definition" for item in report["problems"])
    assert "NOT applied" in report["text"]
    assert "original:" in report["text"]
    assert "proposed:" in report["text"]


def test_52802d58_hook_imported_at_file_top_is_not_missing():
    filename = "Sidebar.tsx"
    source = (
        "import { useAppStore } from './store'\n"
        "export function Sidebar() {\n  return <div>old</div>\n}\n"
    )
    new_code = (
        "export function Sidebar() {\n"
        "  const files = useAppStore()\n"
        "  return <div>{files}</div>\n"
        "}\n"
    )
    report = run_preflight(
        [_edit(filename, "Sidebar", new_code)], [], _maps(filename, source), {filename: source}, {},
    )
    assert not any(item["check"] == "missing_import" for item in report["problems"])


def test_missing_hook_import_is_reported():
    filename = "Sidebar.tsx"
    source = "export function Sidebar() {\n  return <div>old</div>\n}\n"
    new_code = (
        "export function Sidebar() {\n"
        "  const files = useAppStore()\n"
        "  return <div>{files}</div>\n"
        "}\n"
    )
    report = run_preflight(
        [_edit(filename, "Sidebar", new_code)], [], _maps(filename, source), {filename: source}, {},
    )
    assert any(item["check"] == "missing_import" for item in report["problems"])


def test_d007eaf1_preexisting_error_does_not_block_a_distant_edit():
    filename = "Page.tsx"
    source = (
        "const A = 1\n"
        "const A = 2\n"
        "export function Page() {\n  return <div>old</div>\n}\n"
    )
    raw = _edit(
        filename, "Page",
        "export function Page() {\n  return <div>new</div>\n}\n",
    )
    report = run_preflight([raw], [], _maps(filename, source), {filename: source}, {})
    assert not any(item["check"] == "redeclaration" for item in report["problems"])


def test_off_by_one_shifted_preexisting_error_is_not_new():
    """Inserting lines moves a pre-existing error. The message stays the same."""
    filename = "Page.tsx"
    source = (
        "const A = 1\n"
        "const A = 2\n"
        "export function Page() {\n  return <div>old</div>\n}\n"
    )
    new_code = (
        "export function Page() {\n"
        "  const extra = 1\n"
        "  const more = 2\n"
        "  return <div>new</div>\n"
        "}\n"
    )
    report = run_preflight(
        [_edit(filename, "Page", new_code)], [], _maps(filename, source), {filename: source}, {},
    )
    assert not any(item["check"] == "redeclaration" for item in report["problems"])


def test_3a6150e9_half_done_edit_is_not_a_preflight_failure():
    """Plan completeness stays in QA. A valid edit that ignores the request passes."""
    filename = "Page.tsx"
    source = "export function Page() {\n  return <div>old</div>\n}\n"
    new_code = "export function Page() {\n  return <div>still old</div>\n}\n"
    report = run_preflight(
        [_edit(filename, "Page", new_code)], [], _maps(filename, source), {filename: source}, {},
    )
    assert report["problems"] == []


def test_6930f196_sibling_edits_both_land_in_the_composed_file():
    filename = "Types.ts"
    source = (
        "export function alpha() {\n  return 1\n}\n"
        "export function beta() {\n  return 2\n}\n"
    )
    first = _edit(filename, "alpha", "export function alpha() {\n  return 10\n}\n")
    second = _edit(filename, "beta", "export function beta() {\n  return 20\n}\n")
    text, applied, skipped = apply_edits_to_text(
        source,
        [parse_edit_block(first), parse_edit_block(second)],
        _maps(filename, source),
        filename,
    )
    assert len(applied) == 2
    assert skipped == []
    assert "return 10" in text
    assert "return 20" in text


def test_line_edits_apply_bottom_to_top_without_shifting():
    filename = "Page.tsx"
    source = "\n".join([
        "export function Page() {",
        "  const a = 1",
        "  const b = 2",
        "  return <div>{a}{b}</div>",
        "}",
        "",
    ])
    top = {
        "filename": filename, "symbol": "Page",
        "edit_start_line": 2, "edit_end_line": 2, "new_code": "  const a = 10",
    }
    bottom = {
        "filename": filename, "symbol": "Page",
        "edit_start_line": 3, "edit_end_line": 3, "new_code": "  const b = 20",
    }
    text, applied, skipped = apply_edits_to_text(
        source, [top, bottom], _maps(filename, source), filename,
    )
    assert skipped == []
    assert len(applied) == 2
    assert "const a = 10" in text
    assert "const b = 20" in text


def test_unspliceable_block_is_skipped_not_failed():
    filename = "Page.tsx"
    source = "export function Page() {\n  return <div>old</div>\n}\n"
    raw = _edit(filename, "MissingSymbol", "export function Other() {\n  return 1\n}\n")
    report = run_preflight([raw], [], _maps(filename, source), {filename: source}, {})
    assert report["problems"] == []
    assert report["skipped"]


def test_new_file_syntax_error_is_reported():
    import json
    raw = json.dumps({
        "filename": "Broken.tsx",
        "content": "export function Broken() {\n  return (\n}\n",
    })
    report = run_preflight([], [raw], {}, {}, {})
    assert any(item["check"] == "syntax" for item in report["problems"])
    assert report["failing_keys"]


def test_unresolved_relative_import_is_reported():
    filename = "Page.tsx"
    source = "export function Page() {\n  return <div>old</div>\n}\n"
    new_code = (
        "import { Missing } from './Missing'\n"
        "export function Page() {\n  return <Missing />\n}\n"
    )
    report = run_preflight(
        [_edit(filename, "Page", new_code)], [], _maps(filename, source), {filename: source}, {},
    )
    assert any(item["check"] == "unresolved_import" for item in report["problems"])


def test_stash_restore_when_the_symbol_is_omitted_and_supersede_when_resent():
    import json
    bad = _edit("A.tsx", "A", "export function A(){ return (\n }\n")
    key = block_key(parse_edit_block(bad))
    stash = stash_dropped({}, [bad], [])
    assert key in stash
    restored_edits, restored_files = restore_stash([], [], stash)
    assert restored_edits == [bad]
    assert restored_files == []

    replacement = _edit("A.tsx", "A", "export function A() {\n  return <i/>\n}\n")
    kept_stash = forget_superseded(stash, [replacement])
    assert kept_stash == {}
    edits, _files = restore_stash([replacement], [], stash)
    # restore itself also suppresses a symbol the model re-sent
    assert bad not in edits
    assert replacement in edits


def test_dedupe_keeps_the_latest_block_for_a_key():
    first = _edit("A.tsx", "A", "export function A() {\n  return <a/>\n}\n")
    second = _edit("A.tsx", "A", "export function A() {\n  return <b/>\n}\n")
    kept = dedupe_latest([first, second])
    assert kept == [second]


def test_decide_shadow_does_not_drop_and_enforce_does():
    report = {
        "problems": [{"check": "syntax", "message": "bad"}],
        "failing_keys": [("A.tsx", "A", "full:A")],
        "text": "[PREFLIGHT] 1 problem(s). The edit(s) below were NOT applied.",
    }
    raw = _edit("A.tsx", "A", "x")
    shadow = decide_preflight(
        report, mode="shadow", rounds=0, over_budget=False, wants_edit=True,
        edit_blocks=[raw], new_files=[], stash={},
    )
    assert shadow["action"] == "shadow"
    assert shadow["edit_blocks"] == [raw]

    rejected = decide_preflight(
        report, mode="enforce", rounds=0, over_budget=False, wants_edit=True,
        edit_blocks=[raw], new_files=[], stash={},
    )
    assert rejected["action"] == "reject"
    assert rejected["edit_blocks"] == []
    assert rejected["dropped"] == [raw]

    capped = decide_preflight(
        report, mode="enforce", rounds=2, over_budget=False, wants_edit=True,
        edit_blocks=[raw], new_files=[], stash={},
    )
    assert capped["action"] == "pass"
    assert capped["edit_blocks"] == [raw]


def test_decide_skips_questions_and_budget():
    report = {"problems": [{"check": "syntax"}], "failing_keys": [("A.tsx", "A", "full:A")], "text": "x"}
    raw = _edit("A.tsx", "A", "x")
    for kwargs in (
        {"wants_edit": False, "over_budget": False, "mode": "enforce"},
        {"wants_edit": True, "over_budget": True, "mode": "enforce"},
        {"wants_edit": True, "over_budget": False, "mode": "off"},
    ):
        decision = decide_preflight(
            report, rounds=0, edit_blocks=[raw], new_files=[], stash={}, **kwargs,
        )
        assert decision["action"] == "pass"
        assert decision["edit_blocks"] == [raw]


def test_baseline_cache_is_reused():
    filename = "Page.tsx"
    source = "export function Page() {\n  return <div>old</div>\n}\n"
    cache = {}
    raw = _edit(filename, "Page", "export function Page() {\n  return <div>new</div>\n}\n")
    run_preflight([raw], [], _maps(filename, source), {filename: source}, cache)
    assert filename in cache
    cached = cache[filename]
    run_preflight([raw], [], _maps(filename, source), {filename: source}, cache)
    assert cache[filename] is cached


def test_grok_stamp_writes_the_message_onto_the_dropped_call_only():
    import json
    dropped = _edit("A.tsx", "A", "broken")
    kept_args = {"filename": "B.tsx", "symbol": "B", "new_code": "ok"}
    native = {
        "calls": [
            {"id": "c1", "name": "write_surgical_edit", "arguments": json.dumps(parse_edit_block(dropped))},
            {"id": "c2", "name": "write_surgical_edit", "arguments": json.dumps(kept_args)},
        ],
        "results_by_id": {"c1": "Surgical edit recorded.", "c2": "Surgical edit recorded."},
    }
    stamp_grok_preflight(native, [dropped], "[PREFLIGHT] 1 problem")
    assert native["results_by_id"]["c1"].startswith("[PREFLIGHT]")
    assert native["results_by_id"]["c2"] == "Surgical edit recorded."


def test_passing_sibling_is_kept_when_one_block_fails():
    filename = "Page.tsx"
    source = (
        "export function Page() {\n  return <div>old</div>\n}\n"
        "export function Other() {\n  return <span>old</span>\n}\n"
    )
    bad = _edit(
        filename, "Page",
        "export function Page() {\n  return (\n}\n",
    )
    good = _edit(
        filename, "Other",
        "export function Other() {\n  return <span>new</span>\n}\n",
    )
    report = run_preflight([bad, good], [], _maps(filename, source), {filename: source}, {})
    decision = decide_preflight(
        report, mode="enforce", rounds=0, over_budget=False, wants_edit=True,
        edit_blocks=[bad, good], new_files=[], stash={},
    )
    if report["problems"]:
        assert decision["action"] == "reject"
        assert good in decision["edit_blocks"]
        assert bad in decision["dropped"]
    else:
        pytest.fail(f"expected the broken edit to fail, got {report}")
