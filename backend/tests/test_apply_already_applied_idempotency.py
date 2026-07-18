"""
Tests for the "already applied" idempotency check added to the Apply /
Apply All flow (services/surgical_editor.py, routers/surgical.py).

Bug this covers (proven with real logs + code, see conversation history):
A change's `id` is a fresh `str(uuid.uuid4())` generated every time a
SmartResult is emitted. If the same underlying edit is re-emitted (e.g. a
correction round, or a stale message still shown after page reload) and the
user already applied it via one diff card, a *different* diff card / the
"Apply All" button still holds the OLD change.id, which was never marked
applied in the DB. Applying it again fails with "Couldn't find the exact
code to modify" because the snippet is already gone from the file — and
before this fix, that failure was reported as a genuine, retry-needing
failure forever, so "Apply All" kept showing changes as available even
though the file already had them.

Fix: apply_changes_to_file() now checks, on failure, whether the change's
new_code is already present in the current file content. If so it flags
`already_applied: True` on the failed_changes entry instead of treating it
as a real failure. The same check is mirrored in
routers.surgical._rescue_after_total_failure for the all-changes-failed path.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.schemas import SurgicalChange, SymbolInfo, SurgicalApplyRequest
from services.surgical_editor import apply_changes_to_file


def _symbol(name="myFunc", start_line=1, end_line=3, code="old code here"):
    return SymbolInfo(
        name=name, symbol_type="function", start_line=start_line,
        end_line=end_line, code=code,
    )


def _change(change_id, original_code, new_code, symbol_name="myFunc"):
    # symbol.code is deliberately NOT the same text as operations[0]["find"]
    # (original_code) — this de-activates apply_change()'s sentinel-detection
    # ("symbol-replacement / line-number path") and forces the plain
    # operations/search-replace text-match path instead, which is what
    # actually runs in production when a change's anchor text can no longer
    # be found (the scenario this fix targets).
    return SurgicalChange(
        id=change_id,
        symbol=_symbol(name=symbol_name, code=f"__unrelated_symbol_code_for_{symbol_name}__"),
        original_code=original_code,
        new_code=new_code,
        diff="",
        confidence=90,
        description="test change",
        operations=[{"find": original_code, "replace": new_code}],
    )


def test_partial_failure_flags_already_applied_change():
    """One change is genuinely applicable; the other's new_code is already
    in the file (simulating: applied earlier via a different diff card).
    The already-applied one must be flagged, not treated as a hard failure.

    NOTE: apply_change() already has an EARLIER, separate idempotent-reapply
    safety net (surgical_editor.py, "session 52802d58 apply-409 fix") that
    silently no-ops re-applies when new_code.strip() is >= 50 chars and is
    already present verbatim. This test intentionally uses a SHORT new_code
    (< 50 chars) so that safety net does NOT fire, and the change instead
    falls through to the operations/search-replace path, fails to find its
    (already-replaced) original_code, and reaches the new already_applied
    classification added in this fix — proving the two layers are additive,
    not duplicated.
    """
    content = (
        "function real() {\n"
        "    return 1;\n"
        "}\n"
        "const short = 2;\n"  # new_code for stale_change already present, < 50 chars
    )
    genuinely_applicable = _change(
        "id-genuine",
        original_code="function real() {\n    return 1;\n}",
        new_code="function real() {\n    return 2;\n}",
        symbol_name="real",
    )
    stale_already_applied = _change(
        "id-stale",
        original_code="const short = 1;",
        new_code="const short = 2;",
        symbol_name="short",
    )

    result = apply_changes_to_file(
        file_path="virtual.js",
        changes=[genuinely_applicable, stale_already_applied],
        change_ids=None,
        file_content=content,
    )

    assert result.applied_count == 1, "only the genuinely applicable change should apply"
    assert result.failed_count == 1
    failed = result.failed_changes[0]
    assert failed["change_id"] == "id-stale"
    assert failed["already_applied"] is True, (
        "stale change's new_code is already in the file — must be flagged "
        "already_applied, not a generic failure"
    )
    assert "function real() {\n    return 2;\n}" in result.modified_content


def test_genuine_failure_is_not_flagged_already_applied():
    """A change whose new_code is NOT anywhere in the file is a real failure
    (e.g. the file diverged for an unrelated reason) — must NOT be flagged
    already_applied, so the frontend still surfaces it as needing attention.
    """
    content = "function untouched() {\n    return 1;\n}\n"
    real_failure = _change(
        "id-real-fail",
        original_code="function nonexistent() {\n    return 0;\n}",
        new_code="function nonexistent() {\n    return 999;\n}",
        symbol_name="nonexistent",
    )
    genuinely_applicable = _change(
        "id-genuine",
        original_code="function untouched() {\n    return 1;\n}",
        new_code="function untouched() {\n    return 2;\n}",
        symbol_name="untouched",
    )

    result = apply_changes_to_file(
        file_path="virtual.js",
        changes=[genuinely_applicable, real_failure],
        change_ids=None,
        file_content=content,
    )

    assert result.applied_count == 1
    assert result.failed_count == 1
    failed = result.failed_changes[0]
    assert failed["change_id"] == "id-real-fail"
    assert failed["already_applied"] is False


def test_total_failure_all_already_applied_returns_structured_response():
    """routers.surgical._rescue_after_total_failure: when every change in
    the batch is already applied, this must return a normal structured
    response (applied_count=0, failed_changes flagged already_applied=True)
    instead of raising an HTTPException — so the frontend can auto-clear
    them instead of showing a hard, retry-prompting error forever.
    """
    from routers.surgical import _rescue_after_total_failure
    from unittest.mock import MagicMock

    content = "function alreadyDone() {\n    return 'already new';\n}\n"
    stale_change = _change(
        "id-stale-total",
        original_code="function alreadyDone() {\n    return 'old';\n}",
        new_code="function alreadyDone() {\n    return 'already new';\n}",
        symbol_name="alreadyDone",
    )
    req = SurgicalApplyRequest(
        file_path="virtual.js",
        changes=[stale_change],
        file_content=content,
        session_id="test-session",
    )
    fake_request = MagicMock()
    fake_request.state.user_id = "test-user"

    result = _rescue_after_total_failure(
        req, fake_request, ValueError("Couldn't find the exact code to modify.")
    )

    assert result.applied_count == 0
    assert result.failed_count == 1
    assert result.failed_changes[0]["already_applied"] is True
