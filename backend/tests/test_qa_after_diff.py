"""QA notes land on a diff that was already sent.

The pipeline event order (smart_result, then qa_note, then qa_done, then done)
is covered by the merge helper: a pending result gains qa_result per note, and
a missing note stays unfinished.
"""
from services.qa_notes import merge_qa_notes


def _result():
    return {
        "qa_status": "pending",
        "risks": [],
        "changes_by_file": {
            "a.py": {
                "filename": "a.py",
                "changes": [
                    {"id": "c1", "diff": "+x", "qa_result": None},
                    {"id": "c2", "diff": "+y", "qa_result": None},
                ],
            }
        },
        "new_files": [{"filename": "b.py", "content": "print(1)\n", "qa_result": None}],
    }


def test_all_notes_clear_pending():
    result = merge_qa_notes(_result(), [
        {"change_id": "c1", "qa_result": {"verdict": "safe", "summary": "ok"}},
        {"change_id": "c2", "qa_result": {"verdict": "warning", "summary": "check"}},
        {"change_id": "file:b.py", "qa_result": {"verdict": "safe", "summary": "new"}},
    ], risks=["touches auth"])
    assert "qa_status" not in result
    assert result["changes_by_file"]["a.py"]["changes"][0]["qa_result"]["verdict"] == "safe"
    assert result["changes_by_file"]["a.py"]["changes"][1]["qa_result"]["summary"] == "check"
    assert result["new_files"][0]["qa_result"]["summary"] == "new"
    assert result["risks"] == ["touches auth"]


def test_missing_note_stays_unfinished():
    result = merge_qa_notes(_result(), [
        {"change_id": "c1", "qa_result": {"verdict": "safe", "summary": "ok"}},
    ])
    assert result["qa_status"] == "unfinished"
    assert result["changes_by_file"]["a.py"]["changes"][1]["qa_result"] is None
    assert result["new_files"][0]["qa_result"] is None


def test_event_order_names():
    """The saved order is pending diff, notes, done. This pins the names the UI reads."""
    order = ["smart_result", "qa_note", "qa_done", "done"]
    assert order.index("smart_result") < order.index("qa_note") < order.index("qa_done") < order.index("done")
