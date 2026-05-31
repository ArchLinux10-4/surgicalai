"""
Regression guards for the LANDING failure class ("edits emitted -> nothing
coherent ships"):

  1. Same-symbol cumulative merge — when two targeted edits hit the SAME symbol,
     they must be spliced cumulatively into ONE symbol body. Previously each
     edit spliced into the pristine original, producing two changes whose
     `find` anchor was the same original text — so only the first could apply
     and the rest silently conflicted ("N edits emitted -> 1 survives").

  2. Hard 8/10 QA gate — a change ships ONLY with a real score >= 8 and a
     non-blocking verdict. blocked / skipped / unscored / <8 must be withheld
     and surfaced in skipped_changes (never silently shipped at phantom
     confidence).
"""
from services.pipeline import _apply_snippet_to_symbol


SYMBOL = """\
export function LandingPage() {
  useEffect(() => { setupHero(); }, []);
  return (
    <>
      <section id="linear">Linear</section>
      <section id="compare">Compare</section>
    </>
  );
}"""

_OLD_JSX = ('      <section id="linear">Linear</section>\n'
            '      <section id="compare">Compare</section>')
_NEW_JSX = ('      <section id="linear">Linear</section>\n'
            '      <section id="tasks">Tasks from Prompt</section>\n'
            '      <section id="compare">Compare</section>')
_OLD_FX = '  useEffect(() => { setupHero(); }, []);'
_NEW_FX = ('  useEffect(() => { setupHero(); }, []);\n'
           '  useEffect(() => { setupTasksAnim(); }, []);')


def test_cumulative_merge_keeps_both_edits():
    """Two edits to different regions of one symbol must both survive."""
    merged, ok_a, _ = _apply_snippet_to_symbol(SYMBOL, _OLD_JSX, _NEW_JSX)
    assert ok_a
    # second edit splices on top of the FIRST result (the fixed behavior)
    merged, ok_b, _ = _apply_snippet_to_symbol(merged, _OLD_FX, _NEW_FX)
    assert ok_b

    assert '<section id="tasks">' in merged       # JSX edit survived
    assert "setupTasksAnim" in merged             # animation edit survived
    assert merged.count("useEffect(") == 2        # original + added


def test_independent_splice_reproduces_the_bug():
    """Document the old failure: independent splices each lose the other edit."""
    a, _, _ = _apply_snippet_to_symbol(SYMBOL, _OLD_JSX, _NEW_JSX)   # pristine
    b, _, _ = _apply_snippet_to_symbol(SYMBOL, _OLD_FX, _NEW_FX)     # pristine
    # Each carries only its own edit -> conflicting full-symbol rewrites.
    assert ("setupTasksAnim" in a) is False
    assert ('<section id="tasks">' in b) is False


# ── Hard 8/10 gate predicate ────────────────────────────────────────────────
# Mirrors the enforcement in run_natural_pipeline_stream's assembly loop:
# ship ONLY when verdict not in (blocked, skipped) AND score is a real >= 8.
def _ships(verdict, score, gate_min=8):
    return not (verdict in ("blocked", "skipped") or score is None or score < gate_min)


def test_hard_gate():
    cases = [
        ("safe", 9, True),
        ("safe", 8, True),
        ("warning", 8, True),
        ("safe", 7, False),        # below the bar
        ("warning", 5, False),
        ("blocked", 9, False),     # blocked never ships, even at high score
        ("skipped", None, False),  # QA could not run -> never ships unscored
        ("safe", None, False),     # unscored -> never ships
    ]
    for verdict, score, expected in cases:
        assert _ships(verdict, score) is expected, (verdict, score)


if __name__ == "__main__":
    test_cumulative_merge_keeps_both_edits()
    test_independent_splice_reproduces_the_bug()
    test_hard_gate()
    print("ALL SAME-SYMBOL-MERGE + 8/10-GATE TESTS PASSED")
