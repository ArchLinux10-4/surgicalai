"""
Regression suite for the QA-fallback "no report" bug found in session
0183c92e-30a2-4a92-9478-fecfe9769e03.

ROOT CAUSE (proven from the session log):
  1. The QA model burned its max_tokens budget on internal "thinking" before
     writing any JSON, so attempt 1 returned zero text blocks (stop_reason=
     max_tokens, output_tokens=4000). The empty-text guard correctly retried.
  2. Attempt 2 (max_tokens=6000) again hit max_tokens, this time cutting the
     JSON text off mid-string inside the "plan_deviation" field, after
     verdict/qa_score/summary/import_issues/downstream_risks/type_errors had
     already been written IN FULL.
  3. json.loads() rejected the whole (unterminated) object, so the code fell
     into `_qa_fallback_from_prose`, whose only summary strategy was
     `raw_text.strip().split("\\n")[0][:200]` -- the first line of a JSON
     blob is just "{", so the user got score 3/10 with summary "[QA prose
     fallback] {" and every real diagnostic field silently discarded.

FIX: `_salvage_fields_from_truncated_json` regex-extracts whichever JSON
fields were fully written before the cutoff (verdict, qa_score, summary,
plan_deviation, and array fields) using JSON's own string-escape grammar via
json.loads(f'"{match}"') -- NOT str.encode/decode('unicode_escape'), which
was tried first and silently mangled real UTF-8 multibyte characters (e.g.
turned an em-dash into "â\\x80\\x94" mojibake) because that codec assumes
latin-1 bytes, not a already-decoded unicode str.
"""
import sys, json
sys.path.insert(0, "/tmp/sgcheck/backend")

import importlib.util
spec = importlib.util.spec_from_file_location("pipeline", "/tmp/sgcheck/backend/services/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)

_fallback = pipeline._qa_fallback_from_prose
_salvage = pipeline._salvage_fields_from_truncated_json

passed = 0
failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        print(f"  PASS  {label}")
        passed += 1
    else:
        print(f"  FAIL  {label}")
        failed += 1


# ── Case 1: verbatim session 0183c92e raw text (score 3/10, real bug) ───────
RAW_0183C92E = (
    '{\n  "verdict": "blocked",\n  "qa_score": 3,\n  "summary": "Replaces '
    "store-backed sidebarPinned/setSidebarPinned with a localStorage-backed "
    "local state via useState+useCallback, matching the plan's intent, but "
    "introduces `useCallback` which is not imported anywhere in the file "
    "(only useEffect, useState, useRef were imported), which will cause a "
    "new TS2304 compile error \u2014 directly undermining the stated goal of "
    'fixing a build error.",\n  "import_issues": [\n    "`useCallback` is '
    "used (`const persistPinned = useCallback(...)`) but was never imported "
    "in ORIGINAL CODE's import line (`import React, { useEffect, useState, "
    "useRef } from 'react'`) and is not used anywhere else in the original "
    "file, so this is a newly introduced dependency that will fail to "
    'compile unless the file-level import was also updated (not shown/'
    'verifiable here)."\n  ],\n  "downstream_risks": [\n    "The new '
    "localStorage key 'sai-sidebar-pinned' differs from the existing "
    "SIDEBAR_PINNED_KEY constant ('surgicalai_sidebar_pinned') defined "
    "elsewhere in the file, silently discarding any previously persisted "
    'pin state and leaving that constant unused/orphaned."\n  ],\n  '
    '"type_errors": [\n    "Potential TS2304 \'Cannot find name '
    "useCallback' if the file's react import wasn't also updated outside "
    'the shown symbol."\n  ],\n  "logic_errors": [],\n  "plan_deviation": '
    '"Plan asked only to replace sidebarPinned/setSidebarPinned with '
    "localStorage-backed"  # cut off here -- no closing quote/braces, exactly as logged
)

print("== Case 1: verbatim ff4ff718-adjacent session 0183c92e (the reported bug) ==")
r1 = _fallback(RAW_0183C92E, session_id="test", user_id="test")
check("verdict recovered = blocked", r1["verdict"] == "blocked")
check("qa_score recovered = 3", r1["qa_score"] == 3)
check('summary is NOT "[QA prose fallback] {"', r1["summary"] != "[QA prose fallback] {")
check("summary contains the real diagnostic text", "useCallback" in r1["summary"] and "TS2304" in r1["summary"])
check("em-dash preserved correctly (no mojibake)", "\u2014" in r1["summary"] and "\u00e2" not in r1["summary"])
check("import_issues recovered (was silently dropped before)", len(r1["import_issues"]) == 1 and "useCallback" in r1["import_issues"][0])
check("downstream_risks recovered", len(r1["downstream_risks"]) == 1 and "SIDEBAR_PINNED_KEY" in r1["downstream_risks"][0])
check("type_errors recovered", len(r1["type_errors"]) == 1 and "TS2304" in r1["type_errors"][0])
check("plan_deviation (truncated field itself) left empty, not garbage", r1["plan_deviation"] == "")
check("summary tag identifies truncation, not mislabeled as prose", "truncated" in r1["summary"].lower())

# ── Case 2: second real truncated-JSON case from 52802d58 log (score 8/10) ──
RAW_52802D58 = (
    '{\n  "verdict": "safe",\n  "qa_score": 8,\n  "summary": "Sidebar now '
    "sources pinned/setPinned as sidebarPinned/setSidebarPinned from "
    "useAppStore (removing the local useState + localStorage persistPinned "
    "logic) and adds an always-visible chevron toggle button on the 44px "
    "rail that expands/collapses the panel independent of tab selection; "
    'logic traced correctly for both collapse and expand paths.",\n  '
    '"import_issues": [],\n  "downst'  # cut off mid-key-name
)
print("\n== Case 2: second real truncated-JSON case (52802d58, score 8/10) ==")
r2 = _fallback(RAW_52802D58, session_id="test", user_id="test")
check("verdict recovered = safe", r2["verdict"] == "safe")
check("qa_score recovered = 8", r2["qa_score"] == 8)
check("summary is the real sentence, not '{'", r2["summary"].strip() != "[QA prose fallback] {" and "chevron toggle" in r2["summary"])
check("import_issues empty array salvaged correctly (not crashed)", r2["import_issues"] == [])

# ── Case 3: genuine non-JSON prose response must be unaffected ──────────────
print("\n== Case 3: genuine prose response (no JSON at all) -- must be unchanged ==")
PROSE = "This change looks safe overall. Score: 8/10. Verdict: safe. No issues found."
r3 = _fallback(PROSE, session_id="test", user_id="test")
check("verdict = safe", r3["verdict"] == "safe")
check("qa_score = 8", r3["qa_score"] == 8)
check('old "[QA prose fallback]" tag preserved for true prose', r3["summary"].startswith("[QA prose fallback]"))
check("summary is the actual prose sentence", "looks safe overall" in r3["summary"])

# ── Case 4: no signal at all -> must still return None (no false positives) ─
print("\n== Case 4: no verdict/score signal anywhere -> None ==")
r4 = _fallback("The weather today is nice and sunny.", session_id="test", user_id="test")
check("returns None (no hallucinated verdict)", r4 is None)

# ── Case 5: valid, non-truncated JSON must never reach this fallback path, ──
# but if it somehow did, salvage must not corrupt a perfectly fine object.
print("\n== Case 5: well-formed JSON handed to salvage directly (defensive) ==")
WELLFORMED = json.dumps({
    "verdict": "warning", "qa_score": 5, "summary": "All good but minor risk.",
    "import_issues": ["x"], "downstream_risks": [], "type_errors": [], "logic_errors": [],
    "plan_deviation": "none",
})
s5 = _salvage(WELLFORMED)
check("salvage recovers all fields even from well-formed JSON", s5["verdict"] == "warning" and s5["qa_score"] == 5 and s5["summary"] == "All good but minor risk.")

print(f"\n{'='*50}\nTOTAL: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
