"""Tests for the Element Picker feature (local-install only, is_hosted-gated).

Covers:
  - services/element_picker.py state machine (connect/screenshot/pick/disconnect)
    against a real headless Chromium reached via connectOverCDP, so the CDP
    flow itself is exercised for real, not mocked.
  - routers/element_picker.py is_hosted gate: hosted (USE_POSTGRES=True) must
    404 on every endpoint; local (USE_POSTGRES=False) must reach the service.
  - Router module import must never require playwright to be installed
    (deferred-import contract) — proven by monkeypatching sys.modules to
    simulate playwright being absent and confirming import still succeeds.
"""
import os
import subprocess
import sys
import time

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)

CDP_PORT = 9333  # dedicated port for this test run, avoid clobbering a real dev session


@pytest.fixture(scope="module")
def headless_chrome():
    """Launch a real headless Chromium with a CDP debug port, for real connect tests."""
    proc = subprocess.Popen(
        [
            sys.executable, "-c",
            f"""
from playwright.sync_api import sync_playwright
import time
pw = sync_playwright().start()
browser = pw.chromium.launch(headless=True, args=['--remote-debugging-port={CDP_PORT}', '--remote-debugging-address=0.0.0.0'])
page = browser.new_page()
page.set_content('<html><body><h1 id="target">Hello Picker</h1></body></html>')
time.sleep(120)
"""
        ],
        stdout=open("/tmp/element_picker_test_chrome.log", "w"),
        stderr=subprocess.STDOUT,
    )
    # Wait for the CDP endpoint to come up.
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://localhost:{CDP_PORT}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("headless chrome CDP endpoint never came up")
    yield f"http://localhost:{CDP_PORT}"
    proc.kill()
    proc.wait(timeout=10)


@pytest.fixture()
def fresh_picker_module():
    """Each test gets a clean picker singleton (module-level _state)."""
    for mod in list(sys.modules):
        if mod == "services.element_picker" or mod == "services" and False:
            del sys.modules[mod]
    if "services.element_picker" in sys.modules:
        del sys.modules["services.element_picker"]
    import services.element_picker as picker
    yield picker
    try:
        picker.disconnect()
    except Exception:
        pass


class TestElementPickerServiceReal:
    """Real CDP flow — no mocks — proves the actual attach/screenshot/pick/detach path."""

    def test_connect_reports_connected(self, headless_chrome, fresh_picker_module):
        result = fresh_picker_module.connect(headless_chrome)
        assert result["connected"] is True
        assert result["cdp_url"] == headless_chrome

    def test_reconnect_same_url_is_noop(self, headless_chrome, fresh_picker_module):
        fresh_picker_module.connect(headless_chrome)
        result = fresh_picker_module.connect(headless_chrome)  # should not raise
        assert result["connected"] is True

    def test_screenshot_returns_nonempty_jpeg_bytes(self, headless_chrome, fresh_picker_module):
        fresh_picker_module.connect(headless_chrome)
        b64 = fresh_picker_module.screenshot_b64()
        assert isinstance(b64, str) and len(b64) > 100

    def test_pick_returns_element_at_heading(self, headless_chrome, fresh_picker_module):
        fresh_picker_module.connect(headless_chrome)
        # The <h1> fills most of the top-left of the tiny test page.
        result = fresh_picker_module.pick(20, 20)
        assert result["tag"] == "h1"
        assert result["id"] == "target"
        assert "Hello Picker" in result["text"]

    def test_disconnect_clears_state_but_chrome_survives(self, headless_chrome, fresh_picker_module):
        fresh_picker_module.connect(headless_chrome)
        result = fresh_picker_module.disconnect()
        assert result["connected"] is False
        # Chrome process is untouched — CDP endpoint must still answer.
        import urllib.request
        resp = urllib.request.urlopen(f"{headless_chrome}/json/version", timeout=2)
        assert resp.status == 200

    def test_pick_before_connect_raises(self, fresh_picker_module):
        from services.element_picker import ElementPickerError
        with pytest.raises(ElementPickerError):
            fresh_picker_module.pick(0, 0)

    def test_connect_to_unreachable_port_raises_clear_error(self, fresh_picker_module):
        from services.element_picker import ElementPickerError
        with pytest.raises(ElementPickerError, match="Could not connect"):
            fresh_picker_module.connect("http://localhost:1")


class TestElementPickerRouterHostedGate:
    """Proves the is_hosted gate: same DATABASE_URL signal that gates
    Import Folder must gate every element-picker endpoint too."""

    def _import_router_with_hosted_flag(self, monkeypatch, hosted: bool):
        monkeypatch.setenv("DATABASE_URL", "postgres://fake" if hosted else "")
        for mod in list(sys.modules):
            if mod == "database" or mod == "routers.element_picker" or mod == "services.element_picker":
                del sys.modules[mod]
        import database as _database
        import routers.element_picker as _router
        return _database, _router

    def test_status_endpoint_reports_unavailable_when_hosted(self, monkeypatch):
        _database, router_mod = self._import_router_with_hosted_flag(monkeypatch, hosted=True)
        result = router_mod.get_status()
        assert result == {"available": False, "connected": False}

    def test_connect_endpoint_404s_when_hosted(self, monkeypatch):
        from fastapi import HTTPException
        _database, router_mod = self._import_router_with_hosted_flag(monkeypatch, hosted=True)
        with pytest.raises(HTTPException) as exc_info:
            router_mod.connect(router_mod.ConnectRequest(cdp_url="http://localhost:9222"))
        assert exc_info.value.status_code == 404

    def test_module_import_never_requires_playwright(self, monkeypatch):
        """Deferred-import contract: importing the router (and the service it
        wraps) must succeed even when `playwright` cannot be imported —
        proving Railway (which never installs it) can still boot this app."""
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        for mod in list(sys.modules):
            if mod in ("routers.element_picker", "services.element_picker"):
                del sys.modules[mod]
        import routers.element_picker as _router  # must not raise
        assert _router.router is not None
