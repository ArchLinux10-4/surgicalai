"""
Unit tests for _compute_rewrite_drift — the Cursor-inspired post-hoc drift guard
on the full-file direct-rewrite path.

The guard is ADVISORY ONLY: it must never raise and never mutate content. It just
measures how much of a file a rewrite changed and flags egregious churn so the
(advisory) QA agent scrutinizes it.
"""
import importlib.util
import os

# Load the helper from the LIVE pipeline (backend/services/pipeline.py) without
# importing the whole heavy module graph. NOTE: the live file is services/pipeline.py,
# NOT the stale production-dead backend/pipeline.py duplicate.
_PIPELINE = os.path.join(os.path.dirname(__file__), "..", "services", "pipeline.py")


def _load_helper():
    import ast
    src = open(_PIPELINE).read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_compute_rewrite_drift":
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {}
            exec(compile(mod, _PIPELINE, "exec"), ns)
            return ns["_compute_rewrite_drift"]
    raise AssertionError("_compute_rewrite_drift not found in services/pipeline.py")


_compute_rewrite_drift = _load_helper()


def _make_file(n):
    return "\n".join(f"line {i}" for i in range(n))


def test_identical_files_zero_drift():
    f = _make_file(100)
    r = _compute_rewrite_drift(f, f, is_redesign=False)
    assert r["changed_ratio"] == 0.0
    assert r["flagged"] is False


def test_small_targeted_change_not_flagged():
    orig = _make_file(200)
    new_lines = orig.splitlines()
    # Change 5 lines out of 200 => 2.5% churn.
    for i in range(20, 25):
        new_lines[i] = f"CHANGED {i}"
    new = "\n".join(new_lines)
    r = _compute_rewrite_drift(orig, new, is_redesign=False)
    assert r["changed_ratio"] < 0.10
    assert r["flagged"] is False


def test_massive_churn_flagged_on_targeted():
    orig = _make_file(100)
    # Rewrite 80% of the file on a NON-redesign targeted change => should flag.
    new_lines = orig.splitlines()
    for i in range(0, 80):
        new_lines[i] = f"TOTALLY DIFFERENT {i}"
    new = "\n".join(new_lines)
    r = _compute_rewrite_drift(orig, new, is_redesign=False)
    assert r["changed_ratio"] > 0.60
    assert r["flagged"] is True
    assert "exceeding" in r["reason"] or "Possible" in r["reason"]


def test_massive_churn_allowed_on_redesign():
    orig = _make_file(100)
    # Same 80% churn, but this time it's a legitimate redesign => NOT flagged
    # (ceiling is 95% for redesigns).
    new_lines = orig.splitlines()
    for i in range(0, 80):
        new_lines[i] = f"REDESIGNED {i}"
    new = "\n".join(new_lines)
    r = _compute_rewrite_drift(orig, new, is_redesign=True)
    assert r["flagged"] is False


def test_full_replacement_flagged_on_targeted():
    orig = _make_file(50)
    new = _make_file(50).replace("line", "totallynew")
    r = _compute_rewrite_drift(orig, new, is_redesign=False)
    assert r["changed_ratio"] > 0.9
    assert r["flagged"] is True


def test_empty_original_does_not_crash():
    r = _compute_rewrite_drift("", _make_file(10), is_redesign=False)
    assert isinstance(r["changed_ratio"], float)
    assert r["total_lines"] >= 1


def test_return_shape_stable():
    r = _compute_rewrite_drift(_make_file(10), _make_file(10), is_redesign=False)
    for k in ("changed_lines", "unchanged_lines", "total_lines",
              "changed_ratio", "expected_max_ratio", "flagged", "reason"):
        assert k in r
