"""Bookkeeping for the in-app browser. No Chromium."""
import asyncio

from services.inapp_browser import (
    BrowserSession,
    _Tab,
    coalesce_continuous,
    normalize_url,
    picked_node_from_payload,
    rewrite_for_docker,
)


class _Page:
    def __init__(self):
        self.urls = []
        self.viewport = None
        self.closed = False
        self.front = False
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    async def goto(self, url, **kwargs):
        self.urls.append(url)

    async def title(self):
        return "Title"

    async def evaluate(self, *args, **kwargs):
        return ""

    async def screenshot(self, **kwargs):
        return b"\x89PNG\r\n"

    async def set_viewport_size(self, size):
        self.viewport = size

    async def bring_to_front(self):
        self.front = True

    async def close(self):
        self.closed = True


class _CDP:
    def __init__(self, browser):
        self.browser = browser
        self.calls = []

    def on(self, event, callback):
        return None

    async def send(self, method, params=None):
        qsize = self.browser._frame_queue.qsize() if self.browser is not None else 0
        self.calls.append((method, params, qsize))


def test_normalize_and_docker_rewrite(monkeypatch):
    assert normalize_url("example.com/a") == "https://example.com/a"
    assert normalize_url("localhost:5173/app") == "http://localhost:5173/app"
    monkeypatch.delenv("SURGICALAI_IN_DOCKER", raising=False)
    nav, display = rewrite_for_docker(normalize_url("localhost:5173/app"))
    assert nav == display == "http://localhost:5173/app"
    monkeypatch.setenv("SURGICALAI_IN_DOCKER", "1")
    nav, display = rewrite_for_docker(normalize_url("localhost:5173/app"))
    assert display == "http://localhost:5173/app"
    assert nav == "http://host.docker.internal:5173/app"
    nav, display = rewrite_for_docker(normalize_url("example.com"))
    assert nav == display == "https://example.com"


def test_navigate_rewrites_localhost_and_history(monkeypatch):
    monkeypatch.setenv("SURGICALAI_IN_DOCKER", "1")

    async def _run():
        browser = BrowserSession()
        page = _Page()
        browser._browser = object()
        browser._page = page
        await browser.navigate("localhost:5173/app")
        assert page.urls == ["http://host.docker.internal:5173/app"]
        assert browser._display_url == "http://localhost:5173/app"
        await browser.navigate("https://example.com/x")
        await browser.back()
        assert browser._display_url == "http://localhost:5173/app"
        assert browser._history_idx == 0
        assert browser.status()["can_go_forward"] is True
        assert page.urls[-1] == "http://host.docker.internal:5173/app"
        await browser.forward()
        assert browser._display_url == "https://example.com/x"
        assert browser._history_idx == 1

    asyncio.run(_run())


def test_screencast_acks_before_enqueue_and_drops_oldest():
    async def _run():
        browser = BrowserSession()
        cdp = _CDP(browser)
        browser._cdp = cdp
        await browser._on_screencast_frame({"sessionId": 1, "data": "one", "metadata": {"deviceWidth": 10}})
        assert cdp.calls[0][0] == "Page.screencastFrameAck"
        assert cdp.calls[0][2] == 0
        assert browser._frame_queue.qsize() == 1
        await browser._on_screencast_frame({"sessionId": 2, "data": "two", "metadata": {}})
        await browser._on_screencast_frame({"sessionId": 3, "data": "three", "metadata": {}})
        assert cdp.calls[2][2] == 2
        assert browser._frame_queue.qsize() == 2
        assert browser._frame_queue.get_nowait()["data"] == "two"
        assert browser._frame_queue.get_nowait()["data"] == "three"

    asyncio.run(_run())


def test_wheel_deltas_accumulate():
    pending = {}
    coalesce_continuous(pending, "mouseWheel", {"x": 1, "y": 2, "dx": 0, "dy": 4})
    coalesce_continuous(pending, "mouseWheel", {"x": 3, "y": 5, "dx": 1, "dy": 6})
    assert pending["mouseWheel"]["x"] == 3
    assert pending["mouseWheel"]["y"] == 5
    assert pending["mouseWheel"]["dx"] == 1
    assert pending["mouseWheel"]["dy"] == 10


def test_pick_payload_becomes_a_node_and_is_broadcast():
    node = picked_node_from_payload({
        "tag": "h1", "id": "target", "classes": ["title"], "text": "Hello",
        "cssPath": "#target", "rect": {"x": 1, "y": 2, "width": 10, "height": 8},
    }, screenshot_b64="abc", page_url="http://localhost:5000")
    assert node["tag"] == "h1"
    assert node["el_id"] == "target"
    assert node["css_path"] == "#target"
    assert node["screenshot_png_b64"] == "abc"
    assert node["page_url"] == "http://localhost:5000"

    async def _run():
        browser = BrowserSession()
        browser._page = _Page()
        browser._display_url = "http://localhost:5000"
        q = browser.subscribe()
        await browser._on_pick(None, {
            "tag": "h1", "id": "target", "classes": ["title"], "text": "Hello",
            "cssPath": "#target", "rect": {"x": 4, "y": 6, "width": 20, "height": 12},
        })
        event = q.get_nowait()
        assert event["type"] == "pick"
        assert event["node"]["tag"] == "h1"
        assert event["node"]["el_id"] == "target"
        assert event["node"]["screenshot_png_b64"]
        assert event["node"]["page_url"] == "http://localhost:5000"

    asyncio.run(_run())


def test_idle_shutdown_arms_only_when_no_viewers():
    async def _run():
        browser = BrowserSession()
        browser.idle_seconds = 3600
        browser.note_client(1)
        assert browser._idle_task is None
        browser.note_client(1)
        assert browser._idle_task is None
        browser.note_client(-1)
        assert browser._idle_task is None
        browser.note_client(-1)
        assert browser._idle_task is not None
        browser.note_client(1)
        assert browser._idle_task is None
        browser._cancel_idle()

    asyncio.run(_run())


def test_tabs_switch_screencast_and_refuse_the_last_close():
    async def _run():
        browser = BrowserSession()
        browser._browser = object()
        page_a, page_b = _Page(), _Page()
        cdp_a, cdp_b = _CDP(browser), _CDP(browser)
        browser._tabs = [
            _Tab(page=page_a, cdp=cdp_a, display_url="http://a", title="A"),
            _Tab(page=page_b, cdp=cdp_b, display_url="http://b", title="B"),
        ]
        browser._active = 0
        browser._screencast_on = True
        await browser.switch_tab(1)
        assert browser._active == 1
        assert page_b.front is True
        assert any(call[0] == "Page.stopScreencast" for call in cdp_a.calls)
        assert any(call[0] == "Page.startScreencast" for call in cdp_b.calls)
        assert browser.status()["active_tab"] == 1
        assert browser.status()["tabs"][1]["active"] is True
        await browser._on_screencast_frame({"sessionId": 9, "data": "stale"}, browser._tabs[0])
        assert browser._frame_queue.qsize() == 0
        assert any(call[0] == "Page.screencastFrameAck" for call in cdp_a.calls)
        await browser.close_tab(1)
        assert len(browser._tabs) == 1
        assert browser._active == 0
        assert page_b.closed is True
        try:
            await browser.close_tab(0)
            raise AssertionError("last tab closed")
        except Exception as exc:
            assert "last tab" in str(exc).lower()

    asyncio.run(_run())


def test_open_tab_applies_device_scale_after_viewport():
    async def _run():
        browser = BrowserSession()
        browser._browser = object()
        browser._dpr = 2.0
        browser._viewport = (800, 600)

        class _Ctx:
            def __init__(self):
                self.page = _Page()
                self.cdp = _CDP(browser)

            async def new_page(self):
                return self.page

            async def new_cdp_session(self, page):
                return self.cdp

        ctx = _Ctx()
        browser._context = ctx
        index = await browser._open_tab()
        assert index == 0
        assert ctx.page.viewport == {"width": 800, "height": 600}
        methods = [call[0] for call in ctx.cdp.calls]
        assert methods.index("Emulation.setFocusEmulationEnabled") < methods.index("Emulation.setDeviceMetricsOverride")
        metrics = next(call[1] for call in ctx.cdp.calls if call[0] == "Emulation.setDeviceMetricsOverride")
        assert metrics["deviceScaleFactor"] == 2.0
        assert metrics["width"] == 800

    asyncio.run(_run())


def test_set_viewport_overrides_device_scale_and_ignores_tiny_changes():
    async def _run():
        browser = BrowserSession()
        page = _Page()
        cdp = _CDP(browser)
        browser._tabs = [_Tab(page=page, cdp=cdp)]
        browser._viewport = (100, 100)
        browser._dpr = 1.0
        await browser.set_viewport(400, 300, 2)
        assert page.viewport == {"width": 400, "height": 300}
        metrics = [call for call in cdp.calls if call[0] == "Emulation.setDeviceMetricsOverride"]
        assert metrics[-1][1]["deviceScaleFactor"] == 2.0
        assert metrics[-1][1]["width"] == 400
        page.viewport = None
        cdp.calls.clear()
        await browser.set_viewport(408, 308, 2)
        assert page.viewport is None
        assert cdp.calls == []

    asyncio.run(_run())


def test_read_for_model_text_shape():
    async def _run():
        browser = BrowserSession()
        browser._browser = object()

        class _ReadPage(_Page):
            async def evaluate(self, *args, **kwargs):
                return "Hello   world"

            async def screenshot(self, **kwargs):
                assert kwargs.get("type") == "jpeg"
                assert kwargs.get("scale") == "css"
                return b"jpeg-bytes"

        page = _ReadPage()
        browser._tabs = [_Tab(page=page, cdp=_CDP(browser), display_url="http://localhost:5000", title="Matcher")]
        browser._console.append({"kind": "error", "text": "boom", "ts": 1})
        result = await browser.read_for_model()
        assert "Tabs:" in result["text"]
        assert "[0]* Matcher — http://localhost:5000" in result["text"]
        assert "Visible text (tab 0):\nHello world" in result["text"]
        assert "[error] boom" in result["text"]
        assert result["tab"] == 0
        assert result["image_jpeg_b64"]

    asyncio.run(_run())
