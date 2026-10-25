"""Attach QA notes to a diff that was already sent."""


def merge_qa_notes(result: dict, notes: list | None, risks: list | None = None) -> dict:
    """Fill qa_result on matching changes. Missing notes become unfinished."""
    if not isinstance(result, dict):
        return result
    notes = list(notes or [])
    by_id = {}
    by_file = {}
    for note in notes:
        if not isinstance(note, dict):
            continue
        change_id = str(note.get("change_id") or "")
        qa = note.get("qa_result")
        if not isinstance(qa, dict):
            qa = {
                "verdict": note.get("level") or "info",
                "summary": note.get("message") or "",
                "machine_verified": bool(note.get("machine_verified")),
            }
        if change_id.startswith("file:"):
            by_file[change_id[5:]] = qa
        elif change_id:
            by_id[change_id] = qa
    seen = 0
    applied = 0
    for file_data in (result.get("changes_by_file") or {}).values():
        if not isinstance(file_data, dict):
            continue
        for change in file_data.get("changes") or []:
            if not isinstance(change, dict) or not change.get("id"):
                continue
            seen += 1
            qa = by_id.get(change["id"])
            if qa is None:
                continue
            change["qa_result"] = qa
            applied += 1
    for new_file in result.get("new_files") or []:
        if not isinstance(new_file, dict):
            continue
        name = new_file.get("filename") or ""
        seen += 1
        qa = by_file.get(name)
        if qa is None:
            continue
        new_file["qa_result"] = qa
        applied += 1
    if risks is not None:
        result["risks"] = list(risks)
    if notes and seen and applied >= seen:
        result.pop("qa_status", None)
    elif result.get("qa_status") == "pending":
        result["qa_status"] = "unfinished"
    return result
