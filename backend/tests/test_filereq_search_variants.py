"""
Regression test for the agent-mode file-request GitHub search fallback.

Root cause (proven from session 6930f196, uploaded by user, and verified
live against the real repo via github_search_code):

1. The model asked for "lib/fileClassify.ts". The real file is
   "frontend/src/lib/fileClassify.tsx" (note: .tsx, not .ts). GitHub's own
   search for `filename:lib/fileClassify.ts` DOES return this file (verified
   live: totalCount=1), but the old result filter compared the full
   requested string against just the found file's basename
   (`"fileClassify.tsx" == "lib/fileClassify.ts"` -> False), so a real hit
   was silently discarded.

2. The model asked for "types.ts". No file named exactly "types.ts" exists
   anywhere in the repo (verified live: `filename:types.ts` -> 0 hits). The
   real file is "frontend/src/types/index.ts" -- an index-barrel file. A
   second query, `filename:types/index.ts`, DOES find it (verified live:
   totalCount=1), but the old code never tried any variant beyond the exact
   name.

Both failures caused the agent to pause and ask the user for files that
actually already existed in the repo, burning turns and eventually running
out of AGENT_MAX_TURNS with zero edits produced.

This test exercises the pure, extracted variant-building + matching logic
(`build_filereq_search_variants` / `filereq_path_matches`) against the
REAL search-result paths captured live from the repo, without hitting the
network.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import (  # noqa: E402
    build_filereq_search_variants,
    filereq_path_matches,
)


def _first_match(fn, all_paths_by_query):
    """Simulate the real fallback loop: try each variant's query in order,
    filter `all_paths_by_query[query]` with the matcher, stop at first hit."""
    for query_base, ok_bases, allow_barrel in build_filereq_search_variants(fn):
        query = f"filename:{query_base}"
        stem = fn.split("/")[-1].rpartition(".")[0]
        candidates = all_paths_by_query.get(query, [])
        matched = [p for p in candidates if filereq_path_matches(p, ok_bases, allow_barrel, stem)]
        if matched:
            return query, matched
    return None, []


def test_real_case_fileclassify_ts_to_tsx():
    """Real case 1: requested 'lib/fileClassify.ts', real file is
    'frontend/src/lib/fileClassify.tsx'. GitHub's search on the ORIGINAL
    query already returns the hit (verified live) -- the fix must accept it
    instead of discarding it on the basename mismatch."""
    fn = "lib/fileClassify.ts"
    real_search_results = {
        # Verified live via github_search_code with query
        # "repo:ArchLinux10-4/surgicalai filename:lib/fileClassify.ts"
        "filename:lib/fileClassify.ts": ["frontend/src/lib/fileClassify.tsx"],
    }
    query, matched = _first_match(fn, real_search_results)
    assert query == "filename:lib/fileClassify.ts"
    assert matched == ["frontend/src/lib/fileClassify.tsx"]


def test_real_case_types_ts_index_barrel():
    """Real case 2: requested 'types.ts', real file is
    'frontend/src/types/index.ts'. The exact-name query returns 0 hits
    (verified live); the index-barrel variant query
    'filename:types/index.ts' returns the real hit (verified live)."""
    fn = "types.ts"
    real_search_results = {
        # Verified live: 0 hits
        "filename:types.ts": [],
        # Verified live via github_search_code with query
        # "repo:ArchLinux10-4/surgicalai filename:types/index.ts"
        "filename:types/index.ts": ["frontend/src/types/index.ts"],
    }
    query, matched = _first_match(fn, real_search_results)
    assert query == "filename:types/index.ts"
    assert matched == ["frontend/src/types/index.ts"]


def test_no_variants_no_match_returns_empty():
    """True-negative: if none of the variant queries return anything, the
    fallback must give up cleanly (empty), not raise or hang."""
    fn = "totally_nonexistent_file.ts"
    real_search_results = {}  # nothing returns hits
    query, matched = _first_match(fn, real_search_results)
    assert query is None
    assert matched == []


def test_wrong_subdir_falls_back_to_bare_basename():
    """If the model guesses the wrong subdirectory and GitHub's own search on
    the full guessed path also comes up empty, the bare-basename retry must
    still find the file by basename alone."""
    fn = "wrong/dir/client.ts"
    real_search_results = {
        "filename:wrong/dir/client.ts": [],
        "filename:client.ts": ["frontend/src/api/client.ts"],
    }
    query, matched = _first_match(fn, real_search_results)
    assert query == "filename:client.ts"
    assert matched == ["frontend/src/api/client.ts"]


def test_js_jsx_extension_swap():
    """.js requested but real file is .jsx."""
    fn = "utils/helper.js"
    real_search_results = {
        "filename:utils/helper.js": [],
        "filename:helper.js": [],
        "filename:helper.jsx": ["frontend/src/utils/helper.jsx"],
    }
    query, matched = _first_match(fn, real_search_results)
    assert query == "filename:helper.jsx"
    assert matched == ["frontend/src/utils/helper.jsx"]


def test_exact_match_still_works_no_regression():
    """Baseline: a plain, already-correct request must still resolve on the
    very first variant (no behavior change for the common case)."""
    fn = "pipeline.py"
    real_search_results = {
        "filename:pipeline.py": ["backend/services/pipeline.py"],
    }
    query, matched = _first_match(fn, real_search_results)
    assert query == "filename:pipeline.py"
    assert matched == ["backend/services/pipeline.py"]


def test_filereq_path_matches_rejects_unrelated_same_extension_file():
    """A same-extension file in an unrelated directory that happens to share
    no basename match must not be accepted (basic sanity on the matcher)."""
    assert not filereq_path_matches(
        "frontend/src/other/unrelated.tsx", {"fileClassify.tsx"}, False, "fileClassify")


def test_filereq_path_matches_barrel_requires_correct_parent_dir():
    """Index-barrel matching must require the parent directory name to equal
    the requested stem -- an index.ts in an unrelated folder must not match."""
    assert not filereq_path_matches(
        "frontend/src/unrelated/index.ts", set(), True, "types")
    assert filereq_path_matches(
        "frontend/src/types/index.ts", set(), True, "types")
