"""
Regression guard: Express/Fastify/Koa route handlers (anonymous callbacks) must be
captured by the JS/TS symbol indexer so the surgical editor can target them with
full QA — instead of degrading to manual find/replace instructions.

Bug history: `app.post('/api/batch-search', (req, res) => {...})` matched none of the
four existing JS patterns (class / method / function decl / arrow-const), so the
entire route body was invisible. A real edit to the batch-search route scored
"no surgical edit / QA skipped" until route handlers became indexable symbols.
"""
from services.ast_parser import ASTParser


EXPRESS_SRC = """\
const express = require('express');
const router = express.Router();

function helperFn(x) {
  return x + 1;
}

const computeThing = (a, b) => {
  return a * b;
};

router.get('/api/items/:id', async (req, res) => {
  const id = req.params.id;
  res.json({ id });
});

app.post('/api/batch-search', async (req, res) => {
  const rows = req.body.split('\\n').length - 1;
  if (rows > 3000) return res.status(400).json({ error: 'CSV exceeds limit' });
  res.json({ ok: true });
});

router.delete('/api/items/:id', (req, res) => {
  res.status(204).end();
});

app.use('/static', express.static('public'));
"""

# A plain TS/React module with NO route handlers — must produce zero route_ symbols.
PLAIN_SRC = """\
import React from 'react';

export const LoginPage: React.FC = () => {
  return <div>hi</div>;
};

function parseValue(raw) {
  return Number(raw);
}

const cache = new Map();
cache.get('key');                      // Map.get — NOT a route
emitter.on('event', () => doThing());  // .on — NOT a route
"""


def _names(src):
    sm = ASTParser().parse(src, "routes.js")
    return {s.name: s for s in sm.symbols}


def test_route_handlers_are_captured():
    syms = _names(EXPRESS_SRC)
    route_names = [n for n in syms if n.startswith("route_")]
    # get + post + delete = 3 real handlers; app.use mount must be skipped
    assert len(route_names) == 3, f"expected 3 route symbols, got {route_names}"
    assert "route_get_api_items_id" in syms
    assert "route_post_api_batch_search" in syms
    assert "route_delete_api_items_id" in syms
    assert "route_use_static" not in syms  # mount, no handler body


def test_route_symbol_contains_full_body():
    syms = _names(EXPRESS_SRC)
    post = syms["route_post_api_batch_search"]
    assert "CSV exceeds limit" in post.code      # the editable line is inside
    assert post.code.strip().startswith("app.post(")
    assert "{" in post.code and "}" in post.code


def test_existing_symbols_still_captured():
    # The route additions must not regress named functions / arrow consts.
    syms = _names(EXPRESS_SRC)
    assert "helperFn" in syms
    assert "computeThing" in syms


def test_no_false_positive_route_symbols():
    syms = _names(PLAIN_SRC)
    route_names = [n for n in syms if n.startswith("route_")]
    assert route_names == [], f"unexpected route symbols on plain module: {route_names}"
    # and the real symbols are still there
    assert "LoginPage" in syms
    assert "parseValue" in syms


if __name__ == "__main__":
    test_route_handlers_are_captured()
    test_route_symbol_contains_full_body()
    test_existing_symbols_still_captured()
    test_no_false_positive_route_symbols()
    print("ALL ROUTE-SYMBOL TESTS PASSED")
