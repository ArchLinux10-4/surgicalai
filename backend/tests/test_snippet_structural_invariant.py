"""Structural snippet invariant (session aa1584f4).

Evidence (Blocked — LLM QA.jsonl / session aa1584f4):
  Grok emitted short-prefix old_code (155ch head / 60ch body) + complete
  new_code for HTML <head>/<body>. ``_apply_snippet_to_symbol`` did
  symbol.replace(old, new, 1), leaving the original tail after </head>/</body>.
  QA correctly blocked score 1 (duplication). Body recovered via correction;
  head did not.

Durable contract (not an HTML-only heuristic):
  After every successful snippet splice, reject (or gated-promote to full
  symbol) when delimiter parity fails, post-splice structure worsens, or a
  superseded closer remains in the unmatched ``after`` tail.
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.pipeline import (  # noqa: E402
    _apply_snippet_to_symbol,
    _finalize_snippet_apply,
    _fragment_reason,
    _superseded_tail_reason,
    _delimiter_parity_reason,
)


# ── Fixtures shaped like aa1584f4 ──────────────────────────────────────────

def _html_head_symbol():
    """~original head: open tags + large style block + close."""
    style_lines = "\n".join(
        f"    .rule-{i} {{ color: rgb({i}, {i}, {i}); }}" for i in range(80)
    )
    return (
        "<head>\n"
        "  <meta charset=\"UTF-8\" />\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
        "  <title>TalentMatch — Candidate Match</title>\n"
        "  <style>\n"
        f"{style_lines}\n"
        "  </style>\n"
        "</head>"
    )


def _html_head_old_prefix():
    return (
        "<head>\n"
        "  <meta charset=\"UTF-8\" />\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
        "  <title>TalentMatch — Candidate Match</title>"
    )


def _html_head_full_new():
    return (
        "<head>\n"
        "  <meta charset=\"UTF-8\" />\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
        "  <title>TalentMatch — Candidate Match</title>\n"
        "  <link rel=\"preconnect\" href=\"https://fonts.googleapis.com\" />\n"
        "  <style>\n"
        "    :root { --bg: #0b0d10; }\n"
        "    body { margin: 0; }\n"
        "  </style>\n"
        "</head>"
    )


def _html_body_symbol():
    return (
        "<body>\n"
        "  <div class=\"app-shell\">\n"
        "    <header class=\"topbar\">Old topbar</header>\n"
        "    <main>Old main content block with script deps</main>\n"
        "  </div>\n"
        "  <script>\n"
        "    const scoreDetailsButton = document.getElementById('score-details-button');\n"
        "  </script>\n"
        "</body>"
    )


def _html_body_old_prefix():
    return (
        "<body>\n"
        "  <div class=\"app-shell\">\n"
        "    <header class=\"topbar\">"
    )


def _html_body_full_new():
    return (
        "<body>\n"
        "  <div class=\"app\">\n"
        "    <aside class=\"rail\">Nav</aside>\n"
        "    <main>Briefing workspace</main>\n"
        "  </div>\n"
        "  <script>\n"
        "    const scoreDetailsButton = document.getElementById('score-details-button');\n"
        "  </script>\n"
        "</body>"
    )


def _python_def_symbol():
    lines = ["def process(data):"]
    lines += [f"    step_{i} = data.get('{i}', 0)" for i in range(40)]
    lines += ["    total = sum(data.values())", "    return total"]
    return "\n".join(lines) + "\n"


def _python_def_old_prefix():
    return (
        "def process(data):\n"
        "    step_0 = data.get('0', 0)\n"
        "    step_1 = data.get('1', 0)\n"
    )


def _python_def_full_new():
    return (
        "def process(data):\n"
        "    step_0 = data.get('0', 0)\n"
        "    step_1 = data.get('1', 0)\n"
        "    cleaned = {k: v for k, v in data.items() if v}\n"
        "    return sum(cleaned.values())\n"
    )


# ── Red / green: aa1584f4 HTML ─────────────────────────────────────────────

def test_aa1584f4_head_short_prefix_does_not_orphan_closer():
    """Short prefix + full </head> rewrite must not leave original after first closer."""
    sym = _html_head_symbol()
    old = _html_head_old_prefix()
    new = _html_head_full_new()
    # Raw replace still demonstrates the historical failure mode.
    raw = sym.replace(old, new, 1)
    assert raw.count("</head>") >= 2 or "</style>" in raw.split("</head>")[-1], (
        "fixture must reproduce the replace-prefix orphan-tail shape"
    )
    result, ok, reason = _apply_snippet_to_symbol(sym, old, new)
    assert ok, reason
    # No orphan: either full promote (== new) or a splice without a second </head>
    assert result.count("</head>") == 1, (reason, result[-200:])
    assert "</style>\n</head>" not in result or result.strip() == new.strip() or (
        # promoted full rewrite
        result.strip() == new.strip()
    )
    # Strongest: composed must equal the intended full new_code (gated promote)
    assert result.strip() == new.strip(), (
        f"expected gated full-symbol promote; reason={reason!r}"
    )


def test_aa1584f4_body_short_prefix_does_not_orphan_closer():
    sym = _html_body_symbol()
    old = _html_body_old_prefix()
    new = _html_body_full_new()
    raw = sym.replace(old, new, 1)
    assert raw.count("</body>") >= 2
    result, ok, reason = _apply_snippet_to_symbol(sym, old, new)
    assert ok, reason
    assert result.count("</body>") == 1
    assert result.strip() == new.strip()


# ── Python same class ──────────────────────────────────────────────────────

def test_python_short_prefix_full_def_no_duplicated_tail():
    sym = _python_def_symbol()
    old = _python_def_old_prefix()
    new = _python_def_full_new()
    assert sym.startswith(old)
    raw = sym.replace(old, new, 1)
    assert "step_39" in raw  # leftover original tail
    assert "cleaned" in raw
    result, ok, reason = _apply_snippet_to_symbol(sym, old, new)
    assert ok, reason
    assert "step_39" not in result
    assert "cleaned" in result
    assert result.strip() == new.strip()


# ── Neighbor: mid-symbol insert must still work ────────────────────────────

def test_mid_symbol_short_old_large_insert_still_ok():
    sym = _html_body_symbol()
    old = "    <main>Old main content block with script deps</main>"
    new = (
        "    <main>\n"
        "      <section class=\"briefing\">New large inserted block</section>\n"
        "      <section class=\"meta\">More content</section>\n"
        "    </main>"
    )
    result, ok, reason = _apply_snippet_to_symbol(sym, old, new)
    assert ok and (
        reason == "exact"
        or reason.startswith("promoted_full_symbol")
        or reason in ("whitespace-tolerant", "exact-after-strip")
    ), reason
    assert "briefing" in result
    assert 'class="topbar"' in result  # prefix preserved
    assert result.count("</body>") == 1
    # Must NOT have promoted away the rest of the body
    assert 'class="app-shell"' in result or 'class="app"' in result or "topbar" in result
    assert "</script>" in result


# ── Neighbor: JS brace-delta still rejects ─────────────────────────────────

def test_js_short_open_unbalanced_new_still_rejected():
    sym = (
        "function Dashboard() {\n"
        "  const [x, setX] = useState(0);\n"
        "  return <div>{x}</div>;\n"
        "}\n"
    )
    old = "function Dashboard() {\n  const [x, setX] = useState(0);"
    # new adds an extra closing brace relative to old (net change)
    new = (
        "function Dashboard() {\n"
        "  const [x, setX] = useState(0);\n"
        "  const [y, setY] = useState(1);\n"
        "}\n"  # closes function early — brace delta vs old
    )
    # Delimiter parity: old net braces = 1 open, new net = 0 → reject or promote
    # Promotion: new has declaration + closing — fragment_reason may be None.
    # If promoted, result is `new` (complete-looking). If rejected, ok=False.
    result, ok, reason = _apply_snippet_to_symbol(sym, old, new)
    if ok:
        # Must not leave the original `return`/`}` tail after the early close
        assert result.strip() == new.strip()
        assert result.count("return <div>") <= new.count("return <div>")
    else:
        assert "brace" in reason.lower() or "delimiter" in reason.lower() or (
            "superseded" in reason.lower()
        )


# ── Promotion must not ship fragments ──────────────────────────────────────

def test_incomplete_fragment_new_code_not_promoted():
    sym = _html_head_symbol()
    old = _html_head_old_prefix()
    # Fragment: tiny new_code missing declaration-scale content / much smaller
    new = "  <meta charset=\"UTF-8\" />\n"
    assert _fragment_reason(sym, new) is not None
    result, ok, reason = _apply_snippet_to_symbol(sym, old, new)
    # Must reject — not promote a fragment to full symbol
    assert not ok, (reason, result)
    assert "fragment" in reason.lower() or "superseded" in reason.lower() or (
        "delimiter" in reason.lower() or "tag" in reason.lower()
    )


# ── Helper unit checks ─────────────────────────────────────────────────────

def test_superseded_tail_detects_html_closer():
    sym = _html_head_symbol()
    old = _html_head_old_prefix()
    new = _html_head_full_new()
    reason = _superseded_tail_reason(sym, old, new)
    assert reason is not None
    assert "head" in reason.lower() or "superseded" in reason.lower()


def test_delimiter_parity_tag_mismatch():
    old = "<div><span>a</span>"
    new = "<div><span>a</span></div>"  # closes div that old left open
    reason = _delimiter_parity_reason(old, new)
    assert reason is not None


def test_finalize_promotes_when_complete():
    sym = _html_head_symbol()
    old = _html_head_old_prefix()
    new = _html_head_full_new()
    raw = sym.replace(old, new, 1)
    out, ok, reason = _finalize_snippet_apply(sym, old, new, raw, "exact")
    assert ok
    assert out.strip() == new.strip()
    assert "promot" in reason.lower()


# ── Reachability: live call sites + Path-3 windowed skip preserved ─────────

def test_pipeline_wires_finalize_on_option_a_and_path2():
    from services import pipeline as p
    src = inspect.getsource(p)
    assert "_finalize_snippet_apply" in src
    # Option A and Path 2 must go through finalize (via _apply_snippet_to_symbol
    # return path or explicit call).
    assert src.count("_apply_snippet_to_symbol") >= 2
    apply_src = inspect.getsource(p._apply_snippet_to_symbol)
    assert "_finalize_snippet_apply" in apply_src


def test_path3_still_skips_when_windowed_attempted():
    """Plan: do NOT accept full new_code when _windowed_path_attempted."""
    from services import pipeline as p
    src = inspect.getsource(p.run_natural_pipeline_stream)
    assert "not _windowed_path_attempted" in src
    assert "Path 3: Full new_code replacement" in src or (
        "Full new_code replacement" in src
    )
