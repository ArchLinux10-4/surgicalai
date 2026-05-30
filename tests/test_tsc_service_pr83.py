"""
PR #83 regression tests — tsc via Vercel serverless sidecar (Option #2).

Covers:
  1. Feature flag default OFF (fail-safe)
  2. linter_available() reflects flag + URL config
  3. _run_tsc routes to the remote service when enabled
  4. Remote path degrades to a SAFE SKIP ([]) on unreachable / HTTP error
  5. Successful parse maps remote JSON -> backend error-dict shape
  6. Shared secret is sent as x-tsc-secret header
  7. .py (pyflakes) path is unaffected by the flag

Run: python3 -m pytest tests/test_tsc_service_pr83.py -q
"""
import importlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SVC_DIR = os.path.join(os.path.dirname(__file__), "..", "backend", "services")
sys.path.insert(0, os.path.abspath(SVC_DIR))


def _fresh(monkeypatch_env):
    for k, v in monkeypatch_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if "linter_validator" in sys.modules:
        del sys.modules["linter_validator"]
    return importlib.import_module("linter_validator")


def test_flag_off_by_default():
    lv = _fresh({"TSC_ENABLED": None, "TSC_SERVICE_URL": "https://x/api/tsc"})
    # flag off + no local tsc in CI -> not available, honest skip
    assert lv.linter_available("a.ts") is False
    assert lv._tsc_service_enabled() is False


def test_flag_on_with_url_is_available():
    lv = _fresh({"TSC_ENABLED": "1", "TSC_SERVICE_URL": "https://x/api/tsc"})
    assert lv.linter_available("a.ts") is True


def test_flag_on_without_url_not_available():
    lv = _fresh({"TSC_ENABLED": "true", "TSC_SERVICE_URL": None})
    assert lv.linter_available("a.ts") is False


def test_unreachable_service_is_safe_skip():
    lv = _fresh({"TSC_ENABLED": "1", "TSC_SERVICE_URL": "http://127.0.0.1:9/api/tsc"})
    assert lv.validate_linters('const x: number = "no";', "a.ts") == []


class _Handler(BaseHTTPRequestHandler):
    captured = {}
    response = {
        "errors": [
            {"line": 1, "column": 7,
             "message": "Type 'string' is not assignable to type 'number'.",
             "detail": "line 1, col 7: bad"}
        ],
        "tool": "tsc",
    }

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        _Handler.captured["secret"] = self.headers.get("x-tsc-secret")
        _Handler.captured["filename"] = body.get("filename")
        _Handler.captured["content"] = body.get("content")
        out = json.dumps(_Handler.response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()


def test_successful_parse_and_secret_header(stub_server):
    port = stub_server
    lv = _fresh({
        "TSC_ENABLED": "1",
        "TSC_SERVICE_URL": f"http://127.0.0.1:{port}/api/tsc",
        "TSC_SERVICE_SECRET": "s3cr3t",
    })
    errs = lv.validate_linters('const x: number = "no";', "a.ts")
    assert len(errs) == 1
    assert errs[0] == {
        "line": 1, "column": 7,
        "message": "Type 'string' is not assignable to type 'number'.",
        "detail": "line 1, col 7: bad",
    }
    assert lv.count_linter_errors('const x: number = "no";', "a.ts") == 1
    assert _Handler.captured["secret"] == "s3cr3t"
    assert _Handler.captured["filename"] == "a.ts"
    assert "const x" in _Handler.captured["content"]


def test_py_path_unaffected_by_flag():
    lv = _fresh({"TSC_ENABLED": "1", "TSC_SERVICE_URL": "https://x/api/tsc"})
    # pyflakes (if installed) decides .py availability; flag must not force it on
    assert lv.linter_tool_name("x.py") == "pyflakes"
    assert lv.linter_tool_name("x.ts") == "tsc"
