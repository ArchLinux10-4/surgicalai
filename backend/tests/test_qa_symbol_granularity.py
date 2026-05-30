"""
Regression guard for the natural-pipeline QA false-positive.

Root cause (fixed): run_natural_pipeline_stream's first QA round passed the
WHOLE intermediate file as `original_code` while `new_code` was a single
symbol. QA's completeness check (#1) then reported every other symbol as
"dropped", blocking correct surgical edits at 1-2/10.

Contract under test (scoped to the natural pipeline): every run_qa_agent(...)
call inside run_natural_pipeline_stream must pass a SYMBOL-LEVEL value as
original_code (it must reference `symbol`), so it stays apples-to-apples with
new_code (always a single symbol on this path). We also assert the dead
whole-file intermediate machinery is gone so it cannot be re-wired into the bug.

Other QA call sites are intentionally out of scope: the smart pipeline's
direct-rewrite path legitimately compares full-file vs full-file.
"""
import ast
import pathlib

PIPELINE = pathlib.Path(__file__).with_name("pipeline.py")


def _natural_fn(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and \
                node.name == "run_natural_pipeline_stream":
            return node
    raise AssertionError("run_natural_pipeline_stream not found")


def _arg_source(call, name, src):
    for kw in call.keywords:
        if kw.arg == name:
            return ast.get_source_segment(src, kw.value)
    return None


def _natural_qa_calls():
    src = PIPELINE.read_text()
    tree = ast.parse(src)
    fn = _natural_fn(tree)
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            fname = getattr(f, "id", None) or getattr(f, "attr", None)
            if fname == "run_qa_agent":
                out.append((node, _arg_source(node, "original_code", src)))
    return out


def test_natural_pipeline_qa_calls_are_symbol_level():
    calls = _natural_qa_calls()
    # First round + retry round = at least two QA call sites.
    assert len(calls) >= 2, f"expected >=2 run_qa_agent calls, found {len(calls)}"
    for node, orig in calls:
        assert orig is not None, f"run_qa_agent at line {node.lineno} missing original_code"
        assert "symbol" in orig and ".code" in orig, (
            f"run_qa_agent at line {node.lineno} passes non-symbol original_code: {orig!r} "
            "— would compare whole file vs single symbol and falsely flag drops"
        )
        for forbidden in ("qa_original_content", "_intermediate", "file_content"):
            assert forbidden not in orig, (
                f"run_qa_agent at line {node.lineno} uses whole-file original_code ({forbidden!r})"
            )


def test_dead_intermediate_machinery_removed():
    src = PIPELINE.read_text()
    for token in ("qa_original_content", "_intermediate_contents", "_sc_proxy"):
        assert token not in src, f"dead machinery {token!r} still present — remove it"


if __name__ == "__main__":
    test_natural_pipeline_qa_calls_are_symbol_level()
    test_dead_intermediate_machinery_removed()
    print("ALL TESTS PASSED")
