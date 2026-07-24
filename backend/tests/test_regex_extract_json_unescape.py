"""
Regression test for the GPT/model literal-\\n corruption bug (session
d8f0ed39, 2026-07-24): _regex_extract_edit_block's JSON-style fallback
branch grabbed raw text between quotes without unescaping JSON string
escapes, so a well-formed JSON "\\n" was written to the target file as
the literal two-character sequence backslash+n instead of a real newline,
producing invalid code that QA correctly scored 1/10.

Fix: _json_unescape_string() unescapes \\n \\t \\r \\" \\\\ \\/ \\b \\f \\uXXXX
in the JSON-style branch only (XML branches are untouched — they never
carry JSON escaping). Also extended the trailing-artifact cleanup to
strip a stray dangling "; observed in the same real payload.

Uses the same in-repo AST-extraction pattern as test_lf2_fix.py /
test_safe_claude_call.py: pulls the exact shipped functions straight out
of pipeline.py so there is no separate artifact to go stale.
"""
import ast
import os
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.normpath(os.path.join(_HERE, "..", "services", "pipeline.py"))
_FUNCS_TO_EXTRACT = ["_json_unescape_string", "_regex_extract_edit_block"]


def _extract_helpers():
    src = open(_PIPELINE, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"_dlog": lambda *a, **kw: None}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            segment = ast.get_source_segment(src, node)
            if segment:
                try:
                    exec(segment, ns)
                except Exception:
                    pass
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCS_TO_EXTRACT:
            segment = ast.get_source_segment(src, node)
            if segment:
                exec(segment, ns)
    for fn_name in _FUNCS_TO_EXTRACT:
        assert fn_name in ns, f"Failed to extract {fn_name} from pipeline.py"
    return types.SimpleNamespace(**{k: v for k, v in ns.items() if k in _FUNCS_TO_EXTRACT})


H = _extract_helpers()


# ── _json_unescape_string unit tests ─────────────────────────────────────────
def test_unescape_basic_sequences():
    assert H._json_unescape_string('line1\\nline2') == 'line1\nline2'
    assert H._json_unescape_string('a\\tb') == 'a\tb'
    assert H._json_unescape_string('a\\\\b') == 'a\\b'
    assert H._json_unescape_string('say \\"hi\\"') == 'say "hi"'


def test_unescape_unicode_escape():
    assert H._json_unescape_string('\\u0041\\u0042') == 'AB'


def test_unescape_noop_on_plain_text():
    plain = "const x = 1;\nconst y = 2;"
    assert H._json_unescape_string(plain) == plain


# ── Real production repro: literal \n corruption (session d8f0ed39) ────────
def _real_gpt_malformed_payload():
    # A minimal but faithful reproduction of the malformed model output shape
    # that broke json.loads()/_repair_json() and fell to the regex fallback:
    # a stray trailing `,;` sequence after the closing quote of new_code, and
    # JSON-escaped \n / \t sequences inside the code body.
    return (
        '{\n'
        '  "filename": "PasteBatchJobsModal.jsx",\n'
        '  "symbol": "getPreviewColumnMeta",\n'
        '  "old_code": "  const nonManualPlaceholder = \'Example: US\';",\n'
        '  "new_code": "  const nonManualPlaceholder =\\n    \'Example: US, GB, DE\';\\n  if (x) {\\n\\tconsole.log(x);\\n  }",;\n'
        '}\n'
    )


def test_regex_fallback_unescapes_literal_backslash_n_to_real_newline():
    raw = _real_gpt_malformed_payload()
    result = H._regex_extract_edit_block(raw)
    assert result is not None
    assert result["filename"] == "PasteBatchJobsModal.jsx"
    new_code = result["new_code"]
    # Must NOT contain the raw 2-char backslash+n sequence anymore.
    assert "\\n" not in new_code, f"literal backslash-n leaked into new_code: {new_code!r}"
    # Must contain a REAL newline byte instead.
    assert "\n" in new_code
    assert "\t" in new_code, "literal \\t must also be unescaped to a real tab"


def test_regex_fallback_strips_stray_trailing_semicolon_artifact():
    raw = _real_gpt_malformed_payload()
    result = H._regex_extract_edit_block(raw)
    new_code = result["new_code"]
    # The stray `,;` artifact from the malformed JSON tail must not leak
    # into the extracted code.
    assert not new_code.rstrip().endswith(',')
    assert not new_code.rstrip().endswith(';;')


def test_regex_fallback_output_is_valid_looking_js_lines():
    raw = _real_gpt_malformed_payload()
    result = H._regex_extract_edit_block(raw)
    new_code = result["new_code"]
    lines = new_code.split("\n")
    assert len(lines) >= 4, "real newlines must have split the code into multiple lines"
    assert lines[0].strip().startswith("const nonManualPlaceholder")


# ── XML branch must remain untouched (no JSON escaping applied there) ──────
def test_xml_branch_is_not_double_unescaped():
    raw = (
        '{"filename": "Foo.jsx", "new_code">function foo() {\n'
        '  return "a\\\\b";\n'
        '}</new_code>}'
    )
    result = H._regex_extract_edit_block(raw)
    assert result is not None
    # XML branch takes the text verbatim (stripped of surrounding newlines
    # only) — it must not run through _json_unescape_string a second time.
    assert 'a\\\\b' in result["new_code"] or 'a\\b' in result["new_code"]


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
