"""
Post-plan merge validator for Surgical AI task planner.

Sits between plan_tasks() and create_tasks() in chat.py.
Scans planned tasks for overlapping file targets and merges
them into fewer, larger tasks. Pure Python, no LLM calls.

If anything goes wrong, returns the original plan unchanged.
"""

import re
from typing import List, Dict

# `logging` here previously had no basicConfig/handler anywhere in backend/,
# so logger.info/warning calls went nowhere — silent by construction. Swapped
# to _dlog, the one logging path proven to actually surface output.
from services.pipeline import _dlog

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


def _extract_files(text: str) -> set:
    """Pull file paths from task detail text."""
    return {m.group(1).strip() for m in _FILE_RE.finditer(text)}


# ── Merge logic ─────────────────────────────────────────────────────────

def _merge_tasks(group: List[Dict]) -> Dict:
    """Merge a group of tasks that share file targets into one task."""
    if len(group) == 1:
        return group[0]

    # Combined title: first task title + " (merged N tasks)"
    title = group[0].get("title", "Merged task")
    title = f"{title} (+{len(group) - 1} merged)"

    # Combined detail: concatenate all details with clear separators
    details = []
    for i, t in enumerate(group, 1):
        detail = t.get("detail", "").strip()
        if detail:
            details.append(f"--- Part {i}: {t.get('title', 'untitled')} ---\n{detail}")
    merged_detail = "\n\n".join(details)

    # Kind: if any task is "code", result is "code"
    kind = "code" if any(t.get("kind", "code") == "code" for t in group) else "answer"

    return {"title": title, "detail": merged_detail, "kind": kind}


def _build_file_groups(tasks: List[Dict]) -> List[List[int]]:
    """
    Group task indices by shared files using union-find.
    If task A and task B both reference foo.vue, they belong
    in the same group. Transitive: if B also shares bar.py
    with task C, all three merge.
    """
    n = len(tasks)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Map each file to the first task that references it
    file_to_task: Dict[str, int] = {}
    task_files: List[set] = []

    for i, t in enumerate(tasks):
        files = _extract_files(t.get("detail", ""))
        task_files.append(files)
        for f in files:
            # Normalize: compare by basename to catch "src/Foo.vue" vs "Foo.vue"
            basename = f.rsplit("/", 1)[-1]
            if basename in file_to_task:
                union(i, file_to_task[basename])
            else:
                file_to_task[basename] = i

    # Collect groups
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    # Return groups in original order (by first task index)
    return [idxs for _, idxs in sorted(groups.items())]


# ── Public API ──────────────────────────────────────────────────────────

def validate_and_merge(planned_tasks: List[Dict]) -> List[Dict]:
    """
    Post-plan validator. Merges tasks that target the same file(s).
    Returns a (possibly smaller) list of tasks.

    Guaranteed safe: returns the original list on any error.
    """
    if not planned_tasks or len(planned_tasks) <= 1:
        return planned_tasks

    try:
        groups = _build_file_groups(planned_tasks)

        # If no merging happened, return as-is
        if len(groups) == len(planned_tasks):
            _dlog("plan_validator_no_overlap", task_count=len(planned_tasks))
            return planned_tasks

        merged = [_merge_tasks([planned_tasks[i] for i in group]) for group in groups]
        _dlog("plan_validator_merged", before=len(planned_tasks), after=len(merged))
        return merged

    except Exception as exc:
        _dlog("plan_validator_error", error=str(exc), detail="returning original plan unchanged")
        return planned_tasks
