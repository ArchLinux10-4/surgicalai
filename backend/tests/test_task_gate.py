"""
Regression test for the agentic-tasks planning gate.

Two production incidents shaped this gate:

1. The gate tripped on conversational phrases ("step by step", "one by one",
   "do all of these", "plan the steps") that appear in ordinary coding prompts.

2. The gate tripped on bare verb cues ("create tasks") even when they appeared
   inside a DESCRIPTION of a feature, e.g. a request to build a landing-page
   section about "a feature that allows users to create tasks from a prompt".

In both cases the prompt was hijacked away from the proven single-pass pipeline
into the multi-task planner, and the user saw "no code produced".

This test pins the gate to strictly opt-in behaviour: only explicit
task-creation INSTRUCTIONS may trip it; ordinary coding prompts — including
those that merely *mention* creating tasks — never do.
"""
from services.task_planner import wants_task_breakdown


# The exact prompt from the live landing-page test. It is about BUILDING a UI
# section that describes the tasks feature; it must flow through the normal
# pipeline, not the planner.
LANDING_PAGE_PROMPT = (
    "I would like to add a new section to the landingpage. The section is about "
    "the feature we have that allows user to create tasks from a prompt. Please "
    "first review the design of the landingpage to make sure you stay inside our "
    "defined theme. Then study/review the Tasks feature and creak a animated "
    "section that show cases this feature and explains it simply. The current "
    "landing page will give you all the theme info and current UX UI of the "
    "design so you can follow it as a design"
)


# Ordinary coding prompts that must NEVER trigger the agentic planner.
ORDINARY_PROMPTS = [
    LANDING_PAGE_PROMPT,
    # descriptive mentions of "create tasks" (incident #2)
    "add a feature that allows users to create tasks from a prompt",
    "build a button to create tasks",
    "explain how the create tasks endpoint works",
    "Document the Tasks feature in the README",
    "wire up the modal so users can create tasks faster",
    # conversational cues (incident #1)
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

# Genuine task-creation INSTRUCTIONS that MUST trigger the planner.
EXPLICIT_TASK_PROMPTS = [
    "Create tasks from this spec",
    "Create a task for each endpoint",
    "Please create tasks to migrate the DB",
    "Break this down into tasks",
    "Break it into tasks and run them",
    "Split this up into separate tasks",
    "Turn the following into tasks and run them",
    "Make a task list for the migration",
    "Generate a task list",
    "Generate tasks for the refactor",
    "Plan the work for the v2 rollout",
    "Create the following tasks: a, b, c",
]


def test_ordinary_prompts_do_not_trigger_planner():
    hijacked = [p for p in ORDINARY_PROMPTS if wants_task_breakdown(p)]
    assert not hijacked, f"ordinary prompts wrongly hijacked into task planner: {hijacked}"


def test_landing_page_prompt_not_hijacked():
    """The exact live-test prompt must route to the single-pass pipeline."""
    assert wants_task_breakdown(LANDING_PAGE_PROMPT) is False


def test_descriptive_create_tasks_not_hijacked():
    """Mentioning 'create tasks' while describing a feature must not trip."""
    assert wants_task_breakdown(
        "Refactor the dashboard to let users create tasks from a prompt"
    ) is False


def test_explicit_task_prompts_trigger_planner():
    missed = [p for p in EXPLICIT_TASK_PROMPTS if not wants_task_breakdown(p)]
    assert not missed, f"explicit task-creation prompts missed by gate: {missed}"


def test_empty_and_none_safe():
    assert wants_task_breakdown("") is False
    assert wants_task_breakdown(None) is False


def test_no_conversational_cues_remain():
    """Guard against the over-broad patterns ever being re-added.

    Checks the actual matching patterns (not comments/docstrings, which may
    legitimately reference the banned phrases for documentation).
    """
    import services.task_planner as tp
    pattern_src = (" ".join(tp._STRONG_CUE_PATTERNS) + " " + tp._VERB_CUE_RE.pattern).lower()
    for banned in ("step by step", "one by one", "do all of"):
        assert banned not in pattern_src, f"banned conversational cue re-introduced: {banned!r}"
