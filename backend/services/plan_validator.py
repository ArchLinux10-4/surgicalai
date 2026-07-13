"""
File-reference extraction for Surgical AI task planner / task runner.

Historical note (evidence-based, see PR that removed the merge step):
This module used to also merge planner tasks that shared a target file
into a single larger task, on the theory that same-file edits needed to
be serialized. That merge ran on EVERY task list, before create_tasks(),
and used pure filename-basename matching (no code-region awareness).

Real-key testing (task_planner.py returning correct 3-4 task plans, then
plan_validator collapsing them to 1 because they named the same file)
proved this was the root cause of "Agent Mode never creates more than
one task": any session with one file open, or any request whose tasks
happen to name the same filename (extremely common), got flattened
before it ever reached the multi-agent runner.

The merge was also redundant for the safety property it claimed to serve.
services/task_runner.py's `_build_wave()` independently guarantees the
same thing at execution time: tasks whose file targets are not disjoint
never join the same wave, so same-file tasks always execute sequentially
across separate waves, never in parallel. That check does not require or
assume any pre-merge — it only needs _extract_files(), kept below.

So the merge (`validate_and_merge`, `_merge_tasks`, `_build_file_groups`)
was deleted rather than "fixed to be smarter" (e.g. real overlap/symbol
diffing) because the safety it existed for is already provided elsewhere,
and removing it is strictly simpler and more correct than adding complex
region-overlap analysis for a property the system already has.
"""

import re
from typing import Set

# ── File-path extraction ────────────────────────────────────────────────
# Matches common code file paths in task detail text.
# Examples: src/components/Foo.tsx, backend/routers/chat.py,
#           Market_Rate_Report-6.html, styles.css
_FILE_RE = re.compile(
    r'(?:^|[\s`"\'\(,])('                      # leading boundary
    r'(?:[\w./-]+/)?'                           # optional directory segments
    r'[\w][\w.-]*'                              # filename stem
    r'\.'                                       # dot
    r'(?:tsx?|jsx?|vue|py|html?|css|scss|'      # common extensions
    r'json|ya?ml|toml|md|sql|sh|bat|rs|go|'
    r'java|kt|rb|php|c|cpp|h|hpp|swift|'
    r'svelte|astro|env|cfg|ini|xml|csv|txt)'
    r')(?=$|[\s`"\'\),;:.])',                    # trailing boundary
    re.MULTILINE
)


def _extract_files(text: str) -> Set[str]:
    """Pull file paths from task detail text.

    Used by services/task_runner.py to build file-disjoint execution
    waves. Kept here (rather than moved) so task_runner's regex and the
    planner's own file-detection stay identical without duplication.
    """
    return {m.group(1).strip() for m in _FILE_RE.finditer(text)}
