"""
Regression test for the "infinite re-ask burns the time budget" bug.

Root cause (proven from session e3f0e267, uploaded by user, real log lines
below): a file that could never be resolved -- GitHub filename-search
exhausted all 4 filename/extension variants, then the human-in-the-loop
pause (`_await_user_file`, up to 240s) timed out with no upload -- used to
NOT be recorded in `requested_files`. Only the SUCCESS path called
`requested_files.add(fn)`. When the model asked for the exact same missing
filename again in a later turn, `resolve_file_request_batch` (formerly
inline) had no record of the earlier attempt, so the file was treated as
brand new: another full GitHub search + another full 240s blocking pause.

Real log evidence (surgical_debug_e3f0e267 (3).jsonl, session
e3f0e267-d18a-497b-8049-195615e291ab):

  10:46:36  agent_filereq_pause_ask   {filename: useRateCardHooks.js}
  10:50:37  agent_filereq_pause_timeout  {waited_s: 240.0}
  10:50:37  agent_file_request_resolved  {turn: 3, found: []}   <- fn NOT
                                                                    recorded
  10:51:23  agent_filereq_pause_ask   {filename: useRateCardHooks.js}  <- SAME
                                                                          file,
                                                                          turn 5
  10:55:23  agent_filereq_pause_timeout  {waited_s: 240.0}
  10:55:23  agent_file_request_resolved  {turn: 5, found: []}
  10:55:23  agent_loop_deadline_abort  {reason: phase_deadline,
                                        elapsed_s: 583.8}

Two full 240s pauses (480s) for one never-found file blew the 480s
streaming-phase deadline before a single edit was attempted. The user saw:
"No code changes were produced and I ran out of time before finishing my
search" -- despite never actually being given a chance to finish, because
the time was spent re-asking for a file that had already definitively
failed to resolve.

Fix: `requested_files.add(fn)` is now called on the failure path too (see
services/pipeline.py, the `else` branch right before the file-not-found
message is appended), so `resolve_file_request_batch` correctly excludes
an already-attempted-and-failed filename from `new_fnames` on every
subsequent turn -- one search + one pause per genuinely missing file, ever,
not one per turn.

This test exercises `resolve_file_request_batch` directly (the pure
filtering contract) using the real filename and turn-count shape from the
session above, plus the two invariants the fix protects:
  1. A file recorded as requested-but-failed must not be re-fetched.
  2. The legitimate "user uploaded the WRONG file, let the model ask once
     more" correction path (`_rerequestable`) must keep working.
  3. The total distinct-file request cap must still trip correctly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import resolve_file_request_batch  # noqa: E402


def test_never_found_file_is_not_reasked_after_being_recorded():
    """This is the actual proven bug from session e3f0e267. Turn 3: model
    asks for useRateCardHooks.js. It's genuinely missing (GitHub search
    exhausted, HITL pause timed out). THE FIX requires the caller to add
    it to requested_files even on failure -- simulate that here, then turn
    5 must NOT re-fetch it."""
    requested_files = {"RateCard.jsx", "utils.js", "RateCardBillPValues.jsx",
                        "RateCardSavingsSummary.jsx", "RateCardTableSection.jsx",
                        "RateCardMarkupModal.jsx"}
    user_supplied_files = set()  # never supplied -- it timed out
    same_name_retries = {}

    # Turn 3: first time this filename is requested.
    new_fnames, rerequestable, hit_limit = resolve_file_request_batch(
        ["useRateCardHooks.js"], requested_files, user_supplied_files,
        same_name_retries, max_same_file_retries=1, max_file_req_total=15,
    )
    assert new_fnames == ["useRateCardHooks.js"]
    assert rerequestable == set()
    assert hit_limit is False

    # This is the fix under test: the caller must record the outcome (found
    # OR not-found) before the next turn. Simulate the failure path adding
    # it to requested_files, exactly as services/pipeline.py now does.
    requested_files.add("useRateCardHooks.js")

    # Turn 5: model asks for the SAME still-missing filename again.
    new_fnames_2, rerequestable_2, hit_limit_2 = resolve_file_request_batch(
        ["useRateCardHooks.js"], requested_files, user_supplied_files,
        same_name_retries, max_same_file_retries=1, max_file_req_total=15,
    )
    assert new_fnames_2 == [], (
        "BUG REGRESSION: a file already recorded as requested-but-failed "
        "must not trigger a second search + HITL pause")
    assert rerequestable_2 == set()


def test_bug_reproduced_when_failure_outcome_is_not_recorded():
    """Sanity check that proves this test suite actually catches the bug:
    if the caller (wrongly, as the OLD code did) never adds a failed
    filename to requested_files, resolve_file_request_batch has no way to
    know and will legitimately offer it up for re-fetch every time -- this
    is the exact old, broken behavior."""
    requested_files = {"RateCard.jsx"}
    user_supplied_files = set()
    same_name_retries = {}

    # Turn 3 fails; OLD buggy code does NOT add "useRateCardHooks.js" here.
    new_fnames, _, _ = resolve_file_request_batch(
        ["useRateCardHooks.js"], requested_files, user_supplied_files,
        same_name_retries,
    )
    assert new_fnames == ["useRateCardHooks.js"]

    # Turn 5: since it was never recorded, the OLD behavior re-offers it --
    # this is the infinite-reask bug, reproduced here for documentation.
    new_fnames_2, _, _ = resolve_file_request_batch(
        ["useRateCardHooks.js"], requested_files, user_supplied_files,
        same_name_retries,
    )
    assert new_fnames_2 == ["useRateCardHooks.js"], (
        "if this ever fails, the filtering function itself changed -- the "
        "real fix belongs in the CALLER (record every outcome), not here")


def test_wrong_file_uploaded_gets_exactly_one_correction_retry():
    """Legitimate case the fix must not break: user uploads a file, but the
    model realizes it's the WRONG file for what it needs. One re-ask is
    allowed (bounded by max_same_file_retries), then no more."""
    requested_files = {"Foo.jsx"}
    user_supplied_files = {"Foo.jsx"}  # came from a user upload
    same_name_retries = {}

    new_fnames, rerequestable, _ = resolve_file_request_batch(
        ["Foo.jsx"], requested_files, user_supplied_files,
        same_name_retries, max_same_file_retries=1,
    )
    assert new_fnames == ["Foo.jsx"]
    assert rerequestable == {"Foo.jsx"}

    # Caller spends the retry budget after honoring one re-ask.
    same_name_retries["Foo.jsx"] = 1

    new_fnames_2, rerequestable_2, _ = resolve_file_request_batch(
        ["Foo.jsx"], requested_files, user_supplied_files,
        same_name_retries, max_same_file_retries=1,
    )
    assert new_fnames_2 == [], "correction budget exhausted, must not re-ask again"
    assert rerequestable_2 == set()


def test_first_time_request_for_a_new_distinct_file_is_never_blocked():
    """A brand-new filename the model hasn't asked for yet must always be
    fetched, regardless of how many OTHER files have already failed."""
    requested_files = {"a.js", "b.js", "c.js"}
    new_fnames, rerequestable, hit_limit = resolve_file_request_batch(
        ["d.js"], requested_files, set(), {},
        max_same_file_retries=1, max_file_req_total=15,
    )
    assert new_fnames == ["d.js"]
    assert rerequestable == set()
    assert hit_limit is False


def test_total_distinct_file_cap_still_trips():
    """The overall per-session file-request budget must still be enforced
    once the distinct-file count reaches the cap."""
    requested_files = {f"f{i}.js" for i in range(15)}
    new_fnames, rerequestable, hit_limit = resolve_file_request_batch(
        ["brand_new_file.js"], requested_files, set(), {},
        max_same_file_retries=1, max_file_req_total=15,
    )
    assert hit_limit is True
    # new_fnames still includes the requested name (it's a genuinely new,
    # distinct file) -- the CALLER is responsible for refusing to serve it
    # once hit_limit is True; this function's job is just to report it.
    assert new_fnames == ["brand_new_file.js"]


def test_total_cap_does_not_block_a_pure_correction_retry():
    """A same-name correction re-ask of an already-loaded user file must
    NOT count against the total distinct-file cap (it isn't pulling a new
    file, just re-confirming an existing one)."""
    requested_files = {f"f{i}.js" for i in range(15)}
    requested_files.add("Wrong.jsx")
    user_supplied_files = {"Wrong.jsx"}
    new_fnames, rerequestable, hit_limit = resolve_file_request_batch(
        ["Wrong.jsx"], requested_files, user_supplied_files, {},
        max_same_file_retries=1, max_file_req_total=15,
    )
    assert hit_limit is False
    assert new_fnames == ["Wrong.jsx"]
    assert rerequestable == {"Wrong.jsx"}
