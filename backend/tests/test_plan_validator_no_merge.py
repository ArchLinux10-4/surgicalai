"""
Regression test for the removed same-file merge.

Real-key testing (see plan_validator.py module docstring) proved that
merging planner tasks purely because they named the same file was why
Agent Mode almost never produced more than 1 task. This test locks in
the fix: plan_validator now only extracts file references; it must not
expose any merge function, and task_runner's wave builder must be the
sole thing responsible for same-file execution safety.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import plan_validator  # noqa: E402
from services.task_runner import _build_wave  # noqa: E402


def test_validate_and_merge_removed():
    """The harmful merge API must not exist anymore."""
    assert not hasattr(plan_validator, "validate_and_merge")
    assert not hasattr(plan_validator, "_merge_tasks")
    assert not hasattr(plan_validator, "_build_file_groups")


def test_extract_files_still_works():
    """The file-extraction utility task_runner depends on must survive."""
    files = plan_validator._extract_files(
        "Update src/app.py to add validation, then touch styles.css"
    )
    assert "src/app.py" in files
    assert "styles.css" in files


def test_same_file_tasks_stay_separate_but_serialize_in_waves():
    """Three tasks naming the same file: must remain 3 separate tasks,
    and the wave builder must never put more than one of them in the
    same concurrent wave (same-file safety belongs here now, not in a
    pre-merge step)."""
    tasks = [
        {"id": "1", "seq": 0, "detail": "Add input validation to app.py"},
        {"id": "2", "seq": 1, "detail": "Add loading spinner in app.py"},
        {"id": "3", "seq": 2, "detail": "Add logout dialog to app.py"},
    ]

    # No merge step exists anymore -> planner output passes through as-is.
    assert len(tasks) == 3

    # Wave builder must still guarantee same-file tasks never run together.
    first_wave = _build_wave(tasks, cap=4)
    assert len(first_wave) == 1  # only the first same-file task joins


def test_disjoint_file_tasks_can_share_a_wave():
    """Tasks touching different files should be allowed to run in
    parallel — proving the multi-agent runner isn't starved for
    genuinely independent work."""
    tasks = [
        {"id": "1", "seq": 0, "detail": "Update src/app.py"},
        {"id": "2", "seq": 1, "detail": "Update src/styles.css"},
        {"id": "3", "seq": 2, "detail": "Update README.md"},
    ]
    wave = _build_wave(tasks, cap=4)
    assert len(wave) == 3
