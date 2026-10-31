"""Hosted deploys must not expose the in-app browser."""
import asyncio
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _hosted(monkeypatch, hosted=True, disabled=False):
    import database
    monkeypatch.setattr(database, "USE_POSTGRES", hosted)
    if disabled:
        monkeypatch.setenv("SURGICALAI_BROWSER_DISABLED", "1")
    else:
        monkeypatch.delenv("SURGICALAI_BROWSER_DISABLED", raising=False)


def _assert_404(call):
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 404


def test_every_route_404s_when_hosted(monkeypatch):
    _hosted(monkeypatch, hosted=True)
    from routers import inapp_browser as mod

    _assert_404(lambda: asyncio.run(mod.browser_status()))
    _assert_404(lambda: asyncio.run(mod.browser_start()))
    _assert_404(lambda: asyncio.run(mod.browser_stop()))
    _assert_404(lambda: asyncio.run(mod.browser_navigate(mod.UrlBody(url="http://example.com"))))
    _assert_404(lambda: asyncio.run(mod.browser_back()))
    _assert_404(lambda: asyncio.run(mod.browser_forward()))
    _assert_404(lambda: asyncio.run(mod.browser_reload()))
    _assert_404(lambda: asyncio.run(mod.browser_stop_loading()))
    _assert_404(lambda: asyncio.run(mod.browser_pick_mode(mod.PickBody(on=True))))
    _assert_404(lambda: asyncio.run(mod.browser_console()))
    _assert_404(lambda: asyncio.run(mod.browser_style(mod.StyleBody(token="abc", css={"color": "red"}))))
    _assert_404(lambda: asyncio.run(mod.browser_clear_style("abc")))
    _assert_404(lambda: asyncio.run(mod.browser_locate(mod.LocateBody(session_id="s", node={}), request=None)))
    _assert_404(lambda: asyncio.run(mod.browser_screenshot()))
    _assert_404(lambda: asyncio.run(mod.browser_new_tab()))
    _assert_404(lambda: asyncio.run(mod.browser_activate_tab(0)))
    _assert_404(lambda: asyncio.run(mod.browser_close_tab(0)))


def test_disabled_env_404s_on_a_local_install(monkeypatch):
    _hosted(monkeypatch, hosted=False, disabled=True)
    from routers import inapp_browser as mod
    _assert_404(lambda: asyncio.run(mod.browser_status()))


def test_websocket_closes_when_hosted(monkeypatch):
    _hosted(monkeypatch, hosted=True)
    from starlette.websockets import WebSocketDisconnect
    from routers.inapp_browser import router as browser_router

    app = FastAPI()
    app.include_router(browser_router, prefix="/api/browser")
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/browser/ws") as ws:
            ws.receive_text()
    assert exc.value.code == 1008


def test_router_imports_without_playwright(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    for name in ("routers.inapp_browser", "services.inapp_browser"):
        sys.modules.pop(name, None)
    import routers.inapp_browser as mod
    assert mod.router is not None
    assert sys.modules["playwright.async_api"] is None
