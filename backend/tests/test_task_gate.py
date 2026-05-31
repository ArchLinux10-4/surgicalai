"""
Regression test for the agentic-tasks planning gate.

Root cause (production incident): the gate tripped on conversational phrases
("step by step", "one by one", "do all of these", "plan the steps") that appear
in ordinary coding prompts. Those prompts were hijacked away from the proven
single-pass pipeline into the multi-task planner, which re-decomposed the
request and failed to produce the single coherent edit the user expected — so
the user saw "no code produced".

This test pins the gate to strictly opt-in behaviour: only explicit
task-creation requests may trip it; ordinary coding prompts never do.
"""
import re

# Mirror of the deployed pattern list (kept in sync with task_planner.py).
from services.task_planner import wants_task_breakdown


# Ordinary coding prompts that must NEVER trigger the agentic planner.
ORDINARY_PROMPTS = [
    "Walk me through fixing this login bug step by step",
    "Can you explain step by step how this auth flow works?",
    "Fix these validation errors one by one",
    "Please do all of these: add logging, fix the typo, and bump the version",
    "Plan the steps to refactor this component",
    "Refactor the upload handler to use async/await",
    "Add input validation to the signup form",
    "Walk through the code step by step and fix the null check",
    "Go through each function and add type hints",
    "Handle all of the edge cases in the parser",
]

# Genuine task-creation requests that MUST trigger the planner.
EXPLICIT_TASK_PROMPTS = [
    "Create tasks from this spec",
    "Create a task for each endpoint",
    "Break this down into tasks",
    "Break it into tasks and run them",
    "Turn the following into tasks and run them",
    "Make a task list for the migration",
    "Generate tasks for the refactor",
    "Plan the work for the v2 rollout",
    "Split this as separate tasks",
]


def test_ordinary_prompts_do_not_trigger_planner():
    hijacked = [p for p in ORDINARY_PROMPTS if wants_task_breakdown(p)]
    assert not hijacked, f"ordinary prompts wrongly hijacked into task planner: {hijacked}"


def test_explicit_task_prompts_trigger_planner():
    missed = [p for p in EXPLICIT_TASK_PROMPTS if not wants_task_breakdown(p)]
    assert not missed, f"explicit task-creation prompts missed by gate: {missed}"


def test_empty_and_none_safe():
    assert wants_task_breakdown("") is False
    assert wants_task_breakdown(None) is False


def test_no_conversational_cues_remain():
    """Guard against the over-broad patterns ever being re-added."""
    import inspect
    import services.task_planner as tp
    src = inspect.getsource(tp)
    for banned in ("step by step", "one by one", "do all of"):
        assert banned not in src, f"banned conversational cue re-introduced: {banned!r}"
