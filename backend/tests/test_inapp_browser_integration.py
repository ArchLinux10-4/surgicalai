"""Real Chromium pick. Skipped unless Playwright and Chromium are installed.

pytest.ini excludes this marker by default. Run with:
  pytest tests/test_inapp_browser_integration.py -m browser
"""
import asyncio
import time

import pytest


@pytest.mark.browser
def test_pick_returns_the_heading():
    pytest.importorskip("playwright")

    async def _run():
        from services.inapp_browser import BrowserError, BrowserSession
        browser = BrowserSession()
        try:
            await browser.start()
        except BrowserError as exc:
            pytest.skip(str(exc))
        try:
            from urllib.parse import quote
            html = '<html><body><h1 id="target">Hello Picker</h1></body></html>'
            await browser._page.goto("data:text/html," + quote(html))
            await browser.pick_mode(True)
            box = await browser._page.locator("#target").bounding_box()
            assert box
            queue = browser.subscribe()
            x = box["x"] + box["width"] / 2
            y = box["y"] + min(4, box["height"] / 2)
            await browser.mouse("mouseMoved", x, y)
            await browser.mouse("mousePressed", x, y, click_count=1, buttons=1)
            await browser.mouse("mouseReleased", x, y, click_count=1, buttons=0)
            node = None
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if event.get("type") == "pick":
                    node = event["node"]
                    break
            assert node is not None, "click did not produce a pick"
            assert node["tag"] == "h1"
            assert node["el_id"] == "target"
            assert node["screenshot_png_b64"]
            count = await browser._page.evaluate(
                "(path) => document.querySelectorAll(path).length",
                node["css_path"],
            )
            assert count == 1
        finally:
            await browser.stop()

    asyncio.run(_run())
