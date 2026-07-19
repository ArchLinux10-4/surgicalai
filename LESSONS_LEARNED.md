# Lessons Learned — Engineering Process Failures & Fixes

This file records concrete process failures (not just code bugs) so they are not
repeated. Each entry: what happened, the root cause, the proof, and the rule
adopted to prevent recurrence. Entries are append-only — do not delete past
entries even if the underlying code has since changed.

---

## 2026-07-19 — "Guaranteed" test prompt fired code that could never run in production

### What happened
A drift-guard safety feature (advisory-only rewrite-drift detection) was built,
unit-tested (7/7 passing), and pushed as commit `954a284`. A specific test
prompt was declared "guaranteed" to trigger it. It did not fire. Root cause was
found only after a live test failed and the trace was inspected.

### Root cause
The guard was added inside `run_smart_pipeline_stream()` in
`backend/services/pipeline.py`. That function is **never called in
production**. The router (`backend/routers/chat.py:950`) hardcodes:

```python
_use_natural = True  # All models use natural pipeline (R25)
```

and the pipeline selector (`backend/routers/chat.py:1113`) is:

```python
_pipeline = run_natural_pipeline_stream if _use_natural else run_smart_pipeline_stream
```

Since `_use_natural` is unconditionally `True`, with no env var, settings
toggle, or other assignment anywhere in the codebase, `run_smart_pipeline_stream`
is structurally unreachable. Every request actually runs through
`run_natural_pipeline_stream`, a separate, sibling function. The drift guard
lived in the dead one.

### Why the mistake happened
Verification stopped at "the code is correct and wired into a function that
contains related logic" (direct-rewrite helpers, docstrings, tests). It never
traced the actual call chain from the live entry point (the chat router)
forward to confirm that function executes for any real request. Correctness
and reachability were conflated as if checking one implied the other.

### Why it's dangerous beyond the immediate bug
Dead code that looks fully-formed, tested, and documented is worse than
obviously-broken code: it gives false positive signals to exactly the
verification habits meant to catch problems ("find the function," "check it
has tests," "read the code"). A future edit against this style of dead code
would look correct, pass its own tests, and still do nothing in production —
consuming time and creating false confidence, discovered only when a live run
fails again.

### Rule adopted
**Correctness and reachability are two separate checks. Both are required
before claiming a code path "will work," "is guaranteed," or "is live."**

Before asserting a change affects production behavior:
1. Identify the concrete production entry point (the router/endpoint that
   actually receives the request).
2. Trace forward, one hop at a time, through every conditional/selector, to
   the exact line being changed or relied upon.
3. State the traced chain explicitly (e.g., "confirmed reachable via
   `routers/chat.py:1113` → `run_natural_pipeline_stream` → line N") before
   making any guarantee — not just "the function exists and has tests."
4. If a selector/flag governs which path runs (e.g. `_use_natural`), grep for
   every place that flag is set, not just its current value, to confirm there
   is no other branch that could reach the dead path today or in the future.

Do not treat "found the right-looking function" as equivalent to "confirmed
the function runs." When in doubt, grep the call chain before writing the word
"guaranteed."

### Resolution
Commit `954a284` was fully reverted via `e16249c` after confirming byte-for-byte
identity with the pre-push state (`ef25196`). The drift-guard concept itself
was not judged wrong — it is parked pending being rebuilt against the actual
live pipeline (`run_natural_pipeline_stream`), on explicit authorization.
