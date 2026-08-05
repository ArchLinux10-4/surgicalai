"""
Regression tests for the plan-execution-phase disconnect checkpoint.

Root cause (proven from surgical_debug_97224670.jsonl, session
97224670, Grok 4.5, Plan/Agent mode):

  - 3 consecutive user attempts each had the client's SSE stream
    disconnect (76-266s of silence while Grok re-read GitHub files)
    WHILE the plan-execution loop (`execute_task_windowed` /
    `_execute_single_edit`) was mid-way through applying a multi-file
    edit plan. `plan_execute_success` and `plan_chain_update` logs prove
    real edits had already been applied to `file_content_lookup_stream`
    before the drop.
  - The loop's `except asyncio.CancelledError` handler only logged
    `plan_execute_cancelled` and marked the *remaining* items skipped,
    then re-raised immediately — with no checkpoint of the items
    *already completed* in this phase. chat.py's safety net had nothing
    to recover, so the user saw a short reply and no diff card,
    indistinguishable from "the model refused to write code."
  - The architect turn loop (session 414dfaef) and the resolution /
    QA-retry loops (session e4e9d098, QA-retry parity) already
    checkpoint proactively before each risky call. Plan-execution was
    the one phase missing this. The fix reuses the existing, already
    -tested `_build_architect_checkpoint_resolved` parser/shape
    (test_architect_disconnect_checkpoint.py covers its parsing
    robustness in full) and wires a checkpoint yield into the
    plan-execution loop, right before each item's risky edit call —
    mirroring the exact pattern already proven in the architect and
    QA-retry loops.
"""
import inspect

from services.pipeline import run_natural_pipeline_stream


def test_checkpoint_wired_before_risky_edit_call_in_plan_execution_loop():
    """Static-source check (same style as the architect-checkpoint suite's
    wiring test): the plan-execution checkpoint must be built and yielded
    strictly AFTER `p_smap` is resolved for this plan item and strictly
    BEFORE the risky `_edit_task` (the long-running, disconnect-prone LLM
    call) is created — otherwise an item could start its risky call
    without whatever was completed by prior items ever being checkpointed.
    """
    src = inspect.getsource(run_natural_pipeline_stream)

    smap_idx = src.index(
        "p_smap = symbol_maps_by_name.get(p_filename, (None, None))[0]"
    )
    edit_task_idx = src.index("_edit_task = asyncio.ensure_future(_edit_coro)")
    assert smap_idx < edit_task_idx, \
        "p_smap must be resolved before the risky edit task is created"

    between = src[smap_idx:edit_task_idx]
    assert "_build_architect_checkpoint_resolved(" in between, \
        "checkpoint builder must be called between p_smap and the risky edit task"
    assert '"type": "checkpoint"' in between, \
        "must yield an SSE checkpoint chunk in that same window"
    assert '"phase": "plan_execute"' in between, \
        "checkpoint payload must be tagged so it's distinguishable from the " \
        "architect-phase and resolution-phase checkpoints"

    # Must be gated — never yield a checkpoint of nothing when no edits
    # have been produced yet (would spam every plan's very first item).
    guard_idx = between.rfind("if edit_blocks_raw or new_file_blocks_raw:")
    assert guard_idx != -1
    ckpt_call_idx = between.index("_build_architect_checkpoint_resolved(")
    assert guard_idx < ckpt_call_idx


def test_checkpoint_yield_wrapped_in_try_except():
    """The checkpoint emission must never be able to kill the plan-execution
    loop itself with an unrelated exception — mirrors the architect- and
    resolution-phase checkpoints' own try/except pattern."""
    src = inspect.getsource(run_natural_pipeline_stream)
    idx = src.index('"phase": "plan_execute"')
    surrounding = src[max(0, idx - 600):idx + 600]
    assert "except Exception as _plan_ckpt_err:" in surrounding
    assert '"plan_execute_checkpoint_error"' in surrounding
