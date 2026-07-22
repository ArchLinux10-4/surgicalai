"""
Regression test for session ff34af1d-6193-4089-ab9e-2a9289c33698.

Production log evidence: a `PublicHome.jsx` symbol containing the perfectly
valid, unescaped JSX text `& many more` was repeatedly scored 9/10 "safe" by
the LLM QA agent, then force-downgraded to 3/10 "blocked" by the deterministic
structural syntax checker (tree-sitter-typescript TSX grammar false positive
on raw '&' in JSX text), causing an infinite correct-then-reject retry loop
that never converged (3 full retry rounds observed in the log, all producing
byte-identical "safe/9" LLM verdicts immediately overridden to "blocked/3").

Root cause proven via minimal tree-sitter repro (not guessed): the
tree-sitter-typescript TSX grammar raises a genuine ERROR parse node for raw
'&' / '&&' used as plain prose inside JSX text content (e.g. "Terms &
Conditions"), even though this is valid, unescaped JSX that every
browser/Babel/React runtime accepts without complaint.

IMPORTANT — verified against actual @babel/core + @babel/preset-react
(ground truth for what really compiles, not a guess): bare '>' and '<' in
JSX text are, unlike '&', genuinely REJECTED by Babel ("Unexpected token
`>`. Did you mean `&gt;` or `{'>'}`?"). An earlier draft of this fix
suppressed '<'/'>' too based on tree shape alone, which would have silently
hidden a real class of bug. That was caught by testing against Babel before
shipping and narrowed: only text starting with '&' is suppressed.

This test locks in the fix in services/syntax_validator.py and guards against
regressions to the *actual bug catching* the fix must not weaken: adjacent
JSX elements, unclosed tags, broken expression blocks, and bare '<'/'>' in
JSX text must still be flagged.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

try:
    import tree_sitter  # noqa: F401
    import tree_sitter_typescript  # noqa: F401
    _TS_AVAILABLE = True
except ImportError:
    _TS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _TS_AVAILABLE, reason="tree-sitter not installed in this environment"
)

from services.syntax_validator import validate_syntax


def test_production_repro_ff34af1d_ampersand_in_jsx_text_no_longer_blocks():
    """Byte-for-byte production repro: '& many more' inside a <Typography>
    must NOT be flagged as a syntax error."""
    code = (
        "const PublicHome = () => {\n"
        "  return (\n"
        "    <div>\n"
        "      <Typography>+122</Typography>\n"
        "      <Typography>& many more</Typography>\n"
        "    </div>\n"
        "  );\n"
        "};\n"
    )
    assert validate_syntax(code, "PublicHome.jsx") == []


def test_benign_ampersand_ampersand_in_jsx_text_not_flagged():
    code = "const X = () => <div><Typography>foo && bar as text</Typography></div>;"
    assert validate_syntax(code, "x.jsx") == []


def test_real_bug_bare_gt_in_jsx_text_still_flagged():
    """Babel-verified: bare '>' in JSX text is a REAL compile error
    ("Unexpected token `>`"), unlike '&'. Must stay flagged — the fix
    must not over-suppress based on tree shape alone."""
    code = "const X = () => <div><Typography>5 > 3 items</Typography></div>;"
    errs = validate_syntax(code, "x.jsx")
    assert len(errs) >= 1


def test_real_bug_bare_lt_in_jsx_text_still_flagged():
    """Babel-verified: bare '<' in JSX text is also a real compile error
    and must stay flagged."""
    code = "const X = () => <div><Typography>Score < 100</Typography></div>;"
    errs = validate_syntax(code, "x.jsx")
    assert len(errs) >= 1


def test_real_bug_adjacent_jsx_elements_still_flagged():
    code = "const X = () => <div>a</div><div>b</div>;"
    errs = validate_syntax(code, "x.jsx")
    assert len(errs) >= 1


def test_real_bug_unclosed_tag_still_flagged():
    code = "const X = () => <div><span>oops</div>;"
    errs = validate_syntax(code, "x.jsx")
    assert len(errs) >= 1


def test_real_bug_broken_expression_block_still_flagged():
    code = "const X = () => <div>{foo(</div>;"
    errs = validate_syntax(code, "x.jsx")
    assert len(errs) >= 1


def test_error_text_extraction_uses_byte_offsets_not_char_offsets():
    """Guards the byte/char slicing bug found alongside the false positive:
    a multi-byte UTF-8 character earlier in the file used to desync
    node.start_byte/end_byte (byte offsets) against a Python str slice,
    garbling error text (observed in production as "Spacing: '-" instead of
    the real offending snippet). A genuinely broken JSX element placed after
    a multi-byte character must still produce a coherent, non-garbled error
    message that actually contains the offending text.
    """
    code = (
        "const label = 'caf\u00e9 \u2014 espresso'; "  # multi-byte chars: é, — before the bug
        "const X = () => <div>a</div><div>b</div>;"
    )
    errs = validate_syntax(code, "x.jsx")
    assert len(errs) >= 1
    assert "div" in errs[0]["text"] or "div" in errs[0]["message"]
