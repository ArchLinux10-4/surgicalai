"""
Tests for _infer_new_file_from_raw — GPT raw-content fallback for <new_file> blocks.
Covers: header-line detection, user-request filename extraction, content signature
detection, and rejection of garbage/too-short content.
"""
import sys, os, re

# Extract _infer_new_file_from_raw directly from pipeline.py source
# without importing the full module (which pulls in database, openai, etc.)
_pipeline_path = os.path.join(
    os.path.dirname(__file__), "..", "backend", "services", "pipeline.py"
)

def _load_func():
    with open(_pipeline_path) as _f:
        _src = _f.read()
    _func_start = _src.index("\ndef _infer_new_file_from_raw(")
    _func_end = _src.index("\n\nasync def _run_claude_direct_rewrite(")
    _func_src = _src[_func_start:_func_end]
    _ns = {"re": re}
    exec(compile(_func_src, "pipeline.py", "exec"), _ns)
    return _ns["_infer_new_file_from_raw"]

_infer_new_file_from_raw = _load_func()


# ── Header-line filename detection ────────────────────────────────────────

def test_header_js_comment():
    raw = '// filename: src/utils/helpers.ts\nimport { foo } from "bar";\nexport const x = 1;'
    result = _infer_new_file_from_raw(raw, "create a helpers file")
    assert result is not None
    assert result["filename"] == "src/utils/helpers.ts"
    assert "import { foo }" in result["content"]
    # Header line should be stripped from content
    assert "// filename:" not in result["content"]


def test_header_hash_comment():
    raw = '# filename: app.py\nfrom flask import Flask\napp = Flask(__name__)'
    result = _infer_new_file_from_raw(raw, "create a flask app")
    assert result is not None
    assert result["filename"] == "app.py"
    assert "from flask" in result["content"]


def test_header_html_comment():
    raw = '<!-- filename: index.html -->\n<!DOCTYPE html>\n<html><body>Hello</body></html>'
    result = _infer_new_file_from_raw(raw, "create a landing page")
    assert result is not None
    assert result["filename"] == "index.html"


# ── User request filename extraction ─────────────────────────────────────

def test_user_request_filename_html():
    raw = '<!DOCTYPE html>\n<html>\n<head><title>Test</title></head>\n<body><h1>Hello</h1></body>\n</html>'
    result = _infer_new_file_from_raw(raw, "Create a file called landing-page.html with a hero section")
    assert result is not None
    assert result["filename"] == "landing-page.html"


def test_user_request_filename_tsx():
    raw = 'import React from "react";\n\nexport default function Card() {\n  return <div>Card</div>;\n}'
    result = _infer_new_file_from_raw(raw, "Create src/components/Card.tsx")
    assert result is not None
    assert result["filename"] == "src/components/Card.tsx"


def test_user_request_filename_py():
    raw = 'def hello():\n    print("hello world")\n\nif __name__ == "__main__":\n    hello()'
    result = _infer_new_file_from_raw(raw, "Build me a script.py that prints hello")
    assert result is not None
    assert result["filename"] == "script.py"


# ── Content signature detection (no filename in header or request) ───────

def test_signature_html():
    raw = '<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="UTF-8"></head>\n<body>Content here</body>\n</html>'
    result = _infer_new_file_from_raw(raw, "create a web page")
    assert result is not None
    assert result["filename"] == "index.html"
    assert result["language"] == "html"


def test_signature_python():
    raw = 'import os\nimport sys\n\ndef main():\n    print("hello")\n\nif __name__ == "__main__":\n    main()'
    result = _infer_new_file_from_raw(raw, "create a new module")
    assert result is not None
    assert result["filename"] == "module.py"
    assert result["language"] == "python"


def test_signature_react():
    raw = '"use client"\nimport { useState } from "react";\n\nexport default function Counter() {\n  const [count, setCount] = useState(0);\n  return <button onClick={() => setCount(c => c + 1)}>{count}</button>;\n}'
    result = _infer_new_file_from_raw(raw, "make a counter component")
    assert result is not None
    assert result["filename"] == "Component.tsx"
    assert result["language"] == "typescript"


def test_signature_go():
    raw = 'package main\n\nimport "fmt"\n\nfunc main() {\n\tfmt.Println("Hello")\n}'
    result = _infer_new_file_from_raw(raw, "write a go program")
    assert result is not None
    assert result["filename"] == "main.go"
    assert result["language"] == "go"


# ── Priority: header > user request > signature ─────────────────────────

def test_header_overrides_user_request():
    """Header-line filename takes precedence over user_request filename."""
    raw = '// filename: real-name.ts\nimport { x } from "y";'
    result = _infer_new_file_from_raw(raw, "Create other-name.ts")
    assert result is not None
    assert result["filename"] == "real-name.ts"


def test_user_request_overrides_signature():
    """User-request filename takes precedence over content-signature default."""
    raw = '<!DOCTYPE html>\n<html><body>Hello</body></html>'
    result = _infer_new_file_from_raw(raw, "Create dashboard.html")
    assert result is not None
    assert result["filename"] == "dashboard.html"  # Not "index.html"


# ── Rejection cases ─────────────────────────────────────────────────────

def test_reject_too_short():
    result = _infer_new_file_from_raw("hi", "create something")
    assert result is None


def test_reject_empty():
    result = _infer_new_file_from_raw("", "create something")
    assert result is None


def test_reject_unrecognizable():
    """If we can't determine a filename at all, return None — don't guess."""
    raw = "This is just a paragraph of text that doesn't look like code at all and has no file patterns."
    result = _infer_new_file_from_raw(raw, "help me with this")
    assert result is None


# ── GPT exact reproduction: 37510-char HTML block ───────────────────────

def test_gpt_html_reproduction():
    """Reproduce the exact GPT 5.6 failure: raw HTML inside <new_file> tag."""
    # Simulate a large HTML file (like GPT's 37510-char output)
    raw = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<title>App</title>\n</head>\n<body>\n' + '<div class="section">\n<p>Content block</p>\n</div>\n' * 500 + '</body>\n</html>'
    result = _infer_new_file_from_raw(
        raw,
        "Create a responsive single-page HTML file called app.html"
    )
    assert result is not None
    assert result["filename"] == "app.html"
    assert result["language"] == "html"
    assert "<!DOCTYPE html>" in result["content"]
    assert len(result["content"]) > 1000


# ── Content integrity ───────────────────────────────────────────────────

def test_content_preserved_fully():
    """Raw content (minus header line) must be preserved exactly."""
    body = 'from typing import List\n\ndef process(items: List[str]) -> None:\n    for item in items:\n        print(item)\n'
    raw = f'# file: processor.py\n{body}'
    result = _infer_new_file_from_raw(raw, "create processor")
    assert result is not None
    assert result["content"] == body.strip()
