"""
Regression tests for the QA-reference-window false-positive bug.

Root cause (proven byte-for-byte via surgical_debug_ca544fc4.jsonl [GPT] and
surgical_debug_c0df52ac (1).jsonl [Claude] — same shared code path, both
models): ``_extract_qa_reference_lines`` matched any syntactically-valid
camelCase/PascalCase token in the QA summary PROSE as a "code identifier
reference", including CSS/style prop names quoted in the summary (QA wrote
"a new inner Box (overflowX:hidden, maxWidth:100vw)"). Generic style props
like ``maxWidth`` appear scattered dozens of times across a real file, so
they manufactured giant multi-hundred-line "QA-reference" windows with
changed_line_count == 0. Those windows are UNCONDITIONALLY exempt from the
relevance filter (_source == "qa_reference"), so they were always sent to
the correction model — wasting tokens/time and risking silent edits to
unrelated code the QA summary never actually flagged.

STRUCTURAL FIX (version-proof, zero magic numbers):
Stop scraping code symbols out of English prose. The authoritative source
of "what is a real code symbol in this file" is the tree-sitter AST symbol
table (``symbol_maps_by_name``), which the pipeline already builds for every
file. A prose token seeds a correction window ONLY when it resolves to a
real DEFINED symbol in that table. CSS props (overflowX, maxWidth, margin),
imported components (Box, Dialog, Drawer) and generic English are never in
the symbol table, so they are rejected — in every variant of this bug —
without any occurrence-count guessing.

Backstop: when the parser is unavailable (parse error / empty map / older
caller that passes no allow-list, i.e. known_symbols is None), fall back to
the occurrence-frequency heuristic so we never crash or regress on
unparseable files.

Ground truth (real tree-sitter parse of the proving file PublicHome.jsx):
    overflowX  -> NOT a symbol (5 occurrences)
    maxWidth   -> NOT a symbol (48 occurrences)
    margin     -> NOT a symbol (44 occurrences)
    JobMarketTicker -> IS a symbol (2 occurrences)
    PublicHome      -> IS a symbol (3 occurrences)
Note: overflowX (5x) is BELOW any occurrence threshold, proving the
frequency heuristic alone is insufficient and the AST allow-list is required.
"""
from services.pipeline import _extract_qa_reference_lines

try:
    from services.ast_parser import ASTParser
except Exception:  # pragma: no cover - parser optional in some envs
    ASTParser = None

import pytest

# Real QA summary text, byte-for-byte from surgical_debug_ca544fc4.jsonl
# (qa_agent_parsed event). This is the exact prose that seeded the garbage
# windows in production.
REAL_QA_SUMMARY = (
    "Root Box no longer clips overflow-x; a new inner Box "
    "(overflowX:hidden, maxWidth:100vw) now wraps everything from "
    "JobMarketTicker through the legal Dialog, while the sticky header "
    "and mobile Drawer remain outside it, matching the plan exactly."
)


def _qa(summary):
    return {
        "summary": summary,
        "plan_deviation": "",
        "import_issues": [], "type_errors": [], "logic_errors": [],
        "downstream_risks": [], "issues": [], "risk_verdicts": [],
    }


def _make_code(rare_symbol_occurrences=2, common_prop_occurrences=48):
    """Synthetic file with realistic occurrence ratios (mirrors proving file).

    ``JobMarketTicker`` (a real component) appears a handful of times;
    ``maxWidth`` (a generic style prop) appears dozens of times scattered
    file-wide.
    """
    lines = []
    for i in range(rare_symbol_occurrences):
        lines.append(f"<JobMarketTicker key={{{i}}} />")
    step = (500 // common_prop_occurrences) if common_prop_occurrences else 0
    maxwidth_count = 0
    for i in range(500):
        lines.append(f"// filler line {i}")
        if step and i % step == 0 and maxwidth_count < common_prop_occurrences:
            lines.append("  sx={{ maxWidth: '100%' }}")
            maxwidth_count += 1
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# STRUCTURAL PATH: AST symbol allow-list (known_symbols provided)
# ─────────────────────────────────────────────────────────────────────────

def test_ast_grounding_rejects_css_prop_and_imported_component_tokens():
    """With the real symbol allow-list, prose tokens that are not defined
    symbols (overflowX, maxWidth, Box, Dialog, Drawer) must NOT seed windows.
    Only the genuinely-defined symbol JobMarketTicker may."""
    code = _make_code(rare_symbol_occurrences=2, common_prop_occurrences=48)
    # Authoritative allow-list — only real DEFINED symbols from the file.
    known_symbols = {"JobMarketTicker", "PublicHome", "Section"}
    ranges = _extract_qa_reference_lines(
        _qa(REAL_QA_SUMMARY), code, context_lines=20, known_symbols=known_symbols
    )
    total_lines = len(code.splitlines())
    covered = sum(e - s + 1 for s, e in ranges)
    # maxWidth's 48 scattered occurrences would cover most of the file if it
    # leaked through. Grounded, only the tight JobMarketTicker cluster (near
    # the top) may appear — a small fraction of the file.
    assert covered < total_lines * 0.2, (
        f"AST-grounded qa_reference windows cover {covered}/{total_lines} "
        "lines — a non-symbol prose token leaked through the allow-list"
    )


def test_ast_grounding_keeps_real_defined_symbol():
    """A genuine defined-symbol reference in prose must still create a window."""
    code = _make_code(rare_symbol_occurrences=2, common_prop_occurrences=0)
    ranges = _extract_qa_reference_lines(
        _qa("The JobMarketTicker component needs a fix."),
        code, context_lines=20, known_symbols={"JobMarketTicker"},
    )
    assert ranges, "genuine defined-symbol reference was incorrectly dropped"


def test_ast_grounding_empty_allowlist_drops_all_ident_windows():
    """Parser ran but file defines no symbols -> authoritative empty allow-list.
    No identifier-based window may be produced (explicit line numbers still work)."""
    code = _make_code(rare_symbol_occurrences=2, common_prop_occurrences=48)
    ranges = _extract_qa_reference_lines(
        _qa(REAL_QA_SUMMARY), code, context_lines=20, known_symbols=set()
    )
    assert ranges == [], (
        "empty (authoritative) symbol allow-list must drop all identifier "
        f"windows, got {ranges}"
    )


def test_ast_grounding_still_honors_explicit_line_numbers():
    """Explicit 'line N' references are machine-precise and must survive even
    with an empty symbol allow-list (they don't depend on prose tokens)."""
    code = "\n".join(f"line {i}" for i in range(200))
    ranges = _extract_qa_reference_lines(
        _qa("There is a bug at line 100 that must be fixed."),
        code, context_lines=10, known_symbols=set(),
    )
    assert ranges, "explicit line-number reference was dropped"
    # window should be centered around line 100 (0-indexed 99)
    assert any(s <= 99 <= e for s, e in ranges)


# ─────────────────────────────────────────────────────────────────────────
# PARSER-LEVEL GUARANTEE: CSS props are structurally never symbols
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(ASTParser is None, reason="AST parser unavailable")
def test_treesitter_never_indexes_css_props_as_symbols():
    """Locks the structural guarantee at the parser: camelCase CSS/style keys
    inside JSX style objects are never emitted as symbols, while real
    component/function declarations always are."""
    src = (
        "const JobMarketTicker = () => {\n"
        "  return <Box sx={{ overflowX: 'hidden', maxWidth: '100vw', "
        "margin: 0 }}>hi</Box>;\n"
        "};\n"
        "export function PublicHome() {\n"
        "  return <JobMarketTicker />;\n"
        "}\n"
    )
    smap = ASTParser().parse(src, "PublicHome.jsx")
    names = {s.name for s in smap.symbols}
    assert "JobMarketTicker" in names
    assert "PublicHome" in names
    for css_prop in ("overflowX", "maxWidth", "margin"):
        assert css_prop not in names, (
            f"{css_prop} was indexed as a symbol — tree-sitter should never "
            "descend into JSX style-object keys"
        )


# ─────────────────────────────────────────────────────────────────────────
# BACKSTOP PATH: parser unavailable (known_symbols is None) -> frequency guard
# ─────────────────────────────────────────────────────────────────────────

def test_backstop_frequency_guard_when_no_allowlist():
    """When known_symbols is None (parser unavailable), the occurrence-count
    backstop must still suppress a common CSS-prop token."""
    code = _make_code(rare_symbol_occurrences=2, common_prop_occurrences=48)
    ranges = _extract_qa_reference_lines(_qa(REAL_QA_SUMMARY), code, context_lines=20)
    total_lines = len(code.splitlines())
    covered = sum(e - s + 1 for s, e in ranges)
    assert covered < total_lines * 0.5, (
        f"backstop failed: qa_reference windows cover {covered}/{total_lines}"
    )


def test_backstop_keeps_rare_real_symbol():
    code = _make_code(rare_symbol_occurrences=2, common_prop_occurrences=0)
    ranges = _extract_qa_reference_lines(
        _qa("The JobMarketTicker component needs a fix."), code, context_lines=20
    )
    assert ranges, "genuine rare symbol reference was incorrectly dropped (backstop)"


def test_no_qa_text_returns_no_ranges():
    code = _make_code()
    assert _extract_qa_reference_lines(_qa(""), code, context_lines=20) == []
