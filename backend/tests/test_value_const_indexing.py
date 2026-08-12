"""
Regression guard: module-level VALUE constants — template literals and large
object/array literals — must be captured by the JS/TS symbol indexer so the
surgical editor can target them. Without this, a 500-line CSS-in-JS template
constant (a very common React pattern) is invisible, so any edit
that targets it can never resolve and is silently dropped (the QA gate then runs
on 0 changes and the run reports a phantom "done").  The constant is written as
``const CSS = backtick ... backtick`` — a template literal, not an arrow fn.

Bug history: a landing-page edit that added a section needed to touch both the
component JSX *and* the separate `const CSS` template block. The CSS const was
not a symbol, so that edit dropped, and combined with snippet-anchor misses the
whole run shipped nothing while reporting success.
"""
from services.ast_parser import ASTParser


SRC = """\
import React from 'react';

const SMALL = 5;
const NAME = 'hi';

const ARROW = () => {
  return 1;
};

export const Comp: React.FC = () => {
  return null;
};

const CFG = {
  a: 1,
  b: { c: `tmpl ${cond ? `nested ${1}` : 'no'}` },
  d: [1, 2, 3],
};

const CSS = `
  .sai-hero { color: red; }
  .sai-cta-section::before { content: "}"; }
  .sai-task-card { opacity: 0; }
`;

const ARR = [
  'one',
  'two',
  'three',
];

function realFn() {
  const innerSmall = 1;
  return innerSmall;
}
"""


def _names(src):
    sm = ASTParser().parse(src, "LandingPage.tsx")
    return {s.name: s for s in sm.symbols}


def test_template_literal_const_is_captured():
    syms = _names(SRC)
    assert "CSS" in syms, f"CSS template const not indexed; got {list(syms)}"
    css = syms["CSS"]
    assert css.symbol_type.value == "variable"
    # span must cover the whole template, incl. the `}` that lives inside a string
    assert ".sai-hero" in css.code
    assert ".sai-task-card" in css.code
    assert 'content: "}"' in css.code


def test_object_and_array_consts_captured():
    syms = _names(SRC)
    assert "CFG" in syms, "object literal const not indexed"
    assert "ARR" in syms, "array literal const not indexed"
    # nested template inside the object must not truncate the span
    assert "d: [1, 2, 3]" in syms["CFG"].code


def test_small_scalars_are_skipped():
    syms = _names(SRC)
    # plain scalar consts add noise without being editable regions — skip them
    assert "SMALL" not in syms
    assert "NAME" not in syms


def test_arrow_consts_not_double_counted():
    syms = _names(SRC)
    assert syms["ARROW"].symbol_type.value == "arrow_function"
    assert syms["Comp"].symbol_type.value == "arrow_function"


def test_existing_symbols_still_captured():
    syms = _names(SRC)
    assert "realFn" in syms
    # local consts inside a function body must NOT be promoted to symbols
    assert "innerSmall" not in syms


# ---------------------------------------------------------------------------
# Value-producing call RHS must NOT be labeled [function]
# (session 3d9da3fd_round6 — RESEARCH_COUNTRIES = LIST.reduce(...))
# ---------------------------------------------------------------------------

REDUCE_SRC = """\
const RESEARCH_COUNTRY_LIST = [
  { value: 'us', label: 'United States', tavilySlug: 'united states', serperGl: 'us' },
  { value: 'ca', label: 'Canada', tavilySlug: 'canada', serperGl: 'ca' },
];

// `RESEARCH_COUNTRIES` is the canonical lookup map keyed by lowercased name.
const RESEARCH_COUNTRIES = RESEARCH_COUNTRY_LIST.reduce((index, entry) => {
  index[entry.value] = entry;
  index[entry.label.toLowerCase()] = entry;
  return index;
}, {});

const MAPPED = RESEARCH_COUNTRY_LIST.map((entry) => ({
  value: entry.value,
  label: entry.label,
}));

const FROM_ENTRIES = Object.fromEntries([
  ['us', { label: 'United States' }],
  ['ca', { label: 'Canada' }],
]);

const useAppStore = create((set) => ({
  count: 0,
  inc: () => set((s) => ({ count: s.count + 1 })),
}));

const MemoComp = memo(function MemoComp(props) {
  return null;
});

function realFn() {
  return RESEARCH_COUNTRIES['us'];
}
"""


def test_reduce_const_is_variable_not_function():
    """Proven false-positive: labeled [function] → QA hard-blocked bracket lookup."""
    syms = _names(REDUCE_SRC)
    assert "RESEARCH_COUNTRIES" in syms, f"missing; got {list(syms)}"
    assert syms["RESEARCH_COUNTRIES"].symbol_type.value == "variable"
    assert "reduce" in syms["RESEARCH_COUNTRIES"].code


def test_map_and_fromEntries_consts_are_variables():
    syms = _names(REDUCE_SRC)
    assert syms["MAPPED"].symbol_type.value == "variable"
    assert syms["FROM_ENTRIES"].symbol_type.value == "variable"


def test_known_factory_callees_remain_function():
    syms = _names(REDUCE_SRC)
    assert syms["useAppStore"].symbol_type.value == "function"
    assert syms["MemoComp"].symbol_type.value == "function"


def test_list_const_still_variable():
    syms = _names(REDUCE_SRC)
    assert syms["RESEARCH_COUNTRY_LIST"].symbol_type.value == "variable"


if __name__ == "__main__":
    test_template_literal_const_is_captured()
    test_object_and_array_consts_captured()
    test_small_scalars_are_skipped()
    test_arrow_consts_not_double_counted()
    test_existing_symbols_still_captured()
    test_reduce_const_is_variable_not_function()
    test_map_and_fromEntries_consts_are_variables()
    test_known_factory_callees_remain_function()
    test_list_const_still_variable()
    print("ALL VALUE-CONST INDEXING TESTS PASSED")
