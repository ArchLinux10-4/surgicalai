"""
Regression test for the hard file-request circuit breaker
(`check_filereq_hard_limit`).

Context: `resolve_file_request_batch` (see
test_filereq_infinite_reask_bug.py) fixes the proven e3f0e267 infinite
re-ask bug by requiring the CALLER to record every outcome (found or
not-found) in `requested_files`. That fix is correct today, but it is a
single point of failure: if any future code change ever adds a filename
to `fnames_req` without going through that bookkeeping correctly (a new
branch, a renamed variable, an early `continue` before
`requested_files.add`), the same infinite-reask failure mode can recur
silently.

`check_filereq_hard_limit` is a second, INDEPENDENT guard: it counts raw
appearances of a filename in `<file_request>` tags directly, with its own
dict, and trips once a filename has been requested more than
`max_hard_retries` times -- regardless of what `requested_files` or
`same_name_retries` believe about that file's resolution state. Two
independent guards must both fail for the infinite-reask bug to recur.

These tests exercise the pure counting/tripping logic in isolation.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import check_filereq_hard_limit  # noqa: E402


def test_does_not_trip_within_the_limit():
    """Requesting the same file up to (and including) max_hard_retries
    times must never trip the breaker."""
    counts = {}
    for _ in range(4):  # max_hard_retries=4 -> 4 requests is still allowed
        tripped = check_filereq_hard_limit(["Foo.jsx"], counts, max_hard_retries=4)
        assert tripped == []
    assert counts["Foo.jsx"] == 4


def test_trips_on_the_request_after_the_limit():
    """The 5th request for the same filename (with max_hard_retries=4)
    must trip the breaker exactly once, at the moment the count exceeds
    the limit."""
    counts = {}
    for _ in range(4):
        assert check_filereq_hard_limit(["Foo.jsx"], counts, max_hard_retries=4) == []
    tripped = check_filereq_hard_limit(["Foo.jsx"], counts, max_hard_retries=4)
    assert tripped == ["Foo.jsx"]
    assert counts["Foo.jsx"] == 5


def test_stays_tripped_on_every_subsequent_request():
    """Once past the limit, every further request for that filename must
    keep tripping the breaker (it must not silently reset or only fire
    once) -- this is the actual infinite-loop stopper."""
    counts = {}
    for _ in range(5):
        check_filereq_hard_limit(["Foo.jsx"], counts, max_hard_retries=4)
    for _ in range(3):
        tripped = check_filereq_hard_limit(["Foo.jsx"], counts, max_hard_retries=4)
        assert tripped == ["Foo.jsx"]


def test_independent_per_filename_counters():
    """A different filename must have its own independent counter -- one
    file being over-requested must not trip the breaker for an unrelated
    file, and vice versa."""
    counts = {}
    for _ in range(5):
        check_filereq_hard_limit(["Foo.jsx"], counts, max_hard_retries=4)
    # Foo.jsx is now tripped; Bar.jsx has never been requested.
    tripped = check_filereq_hard_limit(["Bar.jsx"], counts, max_hard_retries=4)
    assert tripped == [], "an unrelated, first-time filename must not trip"
    assert counts == {"Foo.jsx": 5, "Bar.jsx": 1}


def test_multiple_filenames_in_one_batch_are_deduped_and_independent():
    """A single <file_request> batch can list several filenames at once.
    Each must be counted and evaluated independently, with no duplicate
    entries in the tripped list even if the same name appears twice in one
    batch (defensive; the model should not do this, but the counter must
    not double-trip)."""
    counts = {"Already.jsx": 4}  # one away from tripping
    tripped = check_filereq_hard_limit(
        ["Already.jsx", "Already.jsx", "Brand.jsx"], counts, max_hard_retries=4)
    assert tripped == ["Already.jsx"], (
        "Already.jsx crosses the limit and must appear exactly once, "
        "Brand.jsx is on its first request and must not trip")
    assert counts["Already.jsx"] == 6
    assert counts["Brand.jsx"] == 1


def test_mutates_the_caller_owned_dict_in_place():
    """The caller's dict must persist counts across calls (across turns in
    the real pipeline loop) -- verifies the function does not silently
    return a fresh dict instead of mutating the one it was given."""
    counts = {}
    check_filereq_hard_limit(["X.jsx"], counts, max_hard_retries=4)
    assert counts == {"X.jsx": 1}
    check_filereq_hard_limit(["X.jsx"], counts, max_hard_retries=4)
    assert counts == {"X.jsx": 2}


def test_empty_batch_is_a_noop():
    """An empty fnames_req list (defensive) must not error or trip
    anything."""
    counts = {}
    tripped = check_filereq_hard_limit([], counts, max_hard_retries=4)
    assert tripped == []
    assert counts == {}
