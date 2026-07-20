"""Element Picker — local-install-only browser element inspector.

Uses Playwright's `connectOverCDP` to attach to a Chrome instance the user
already has running (e.g. `chrome --remote-debugging-port=9222`). This is a
lightweight *client* connection — it does NOT download or launch a Chromium
binary, and closing the connection does not kill the user's browser.

Zero Railway footprint by design:
  - `playwright` lives in `requirements-local.txt`, which Railway's
    nixpacks build never reads (only `requirements.txt`).
  - The `playwright` import below is deferred into each function body so
    importing this module never raises on a host where the package isn't
    installed. Every function fails soft with a clear RuntimeError instead.
  - The router that calls into this module is additionally gated on
    `is_hosted` (see routers/element_picker.py), so none of this ever runs
    on a hosted deploy even if playwright somehow were present.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_DLOG_PATH = "/tmp/element_picker_dlog.jsonl"


def _dlog(event: str, **kwargs):
    """Same pattern as database.py's _dlog: logger + flat-file, never raises."""
    try:
        ts = _dt.datetime.utcnow().isoformat() + "Z"
        record = {"ts": ts, "event": event, **kwargs}
        logger.info("[element_picker] %s", json.dumps(record, default=str))
        try:
            with open(_DLOG_PATH, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass
    except Exception:
        pass


class ElementPickerError(RuntimeError):
    """Raised for any element-picker failure the router should surface as 4xx."""


class _PickerState:
    """Holds the single active CDP connection (one at a time, one user machine)."""

    def __init__(self):
        self.lock = threading.Lock()
        self._playwright = None   # sync_playwright() context object
        self._browser = None      # playwright Browser (CDP-attached)
        self._page = None         # playwright Page (active tab)
        self.cdp_url: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self._browser is not None

    def connect(self, cdp_url: str) -> dict:
        with self.lock:
            if self._browser is not None:
                # Idempotent: reconnecting to the same URL is a no-op success;
                # reconnecting to a different URL requires an explicit disconnect first.
                if self.cdp_url == cdp_url:
                    _dlog("connect_noop_already_connected", cdp_url=cdp_url)
                    return self._status_locked()
                raise ElementPickerError(
                    f"Already connected to {self.cdp_url}. Disconnect first."
                )
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as e:
                _dlog("connect_playwright_missing", error=str(e))
                raise ElementPickerError(
                    "Playwright is not installed. This feature is local-install "
                    "only — run `pip install -r backend/requirements-local.txt`."
                ) from e

            try:
                pw = sync_playwright().start()
                browser = pw.chromium.connect_over_cdp(cdp_url)
                contexts = browser.contexts
                if not contexts or not contexts[0].pages:
                    pw.stop()
                    raise ElementPickerError(
                        "Connected to Chrome, but no open tabs were found. "
                        "Open at least one tab in the target Chrome window."
                    )
                page = contexts[0].pages[0]
            except ElementPickerError:
                raise
            except Exception as e:
                _dlog("connect_failed", cdp_url=cdp_url, error=str(e))
                raise ElementPickerError(
                    f"Could not connect to Chrome at {cdp_url}: {e}"
                ) from e

            self._playwright = pw
            self._browser = browser
            self._page = page
            self.cdp_url = cdp_url
            _dlog("connect_ok", cdp_url=cdp_url, page_url=page.url)
            return self._status_locked()

    def screenshot(self) -> bytes:
        with self.lock:
            if self._page is None:
                raise ElementPickerError("Not connected. Call connect first.")
            try:
                return self._page.screenshot(type="jpeg", quality=70, full_page=False)
            except Exception as e:
                _dlog("screenshot_failed", error=str(e))
                raise ElementPickerError(f"Screenshot failed: {e}") from e

    def pick(self, x: float, y: float) -> dict:
        with self.lock:
            if self._page is None:
                raise ElementPickerError("Not connected. Call connect first.")
            try:
                result = self._page.evaluate(
                    """([x, y]) => {
                        const el = document.elementFromPoint(x, y);
                        if (!el) return null;
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: el.tagName.toLowerCase(),
                            id: el.id || null,
                            className: (el.className && typeof el.className === 'string') ? el.className : null,
                            text: (el.innerText || el.textContent || '').trim().slice(0, 500),
                            outerHTML: el.outerHTML.slice(0, 2000),
                            rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                        };
                    }""",
                    [x, y],
                )
            except Exception as e:
                _dlog("pick_failed", x=x, y=y, error=str(e))
                raise ElementPickerError(f"Pick failed: {e}") from e

            if result is None:
                raise ElementPickerError("No element found at that position.")
            _dlog("pick_ok", x=x, y=y, tag=result.get("tag"))
            return result

    def disconnect(self) -> dict:
        with self.lock:
            if self._browser is not None:
                try:
                    self._browser.close()
                except Exception as e:
                    # Non-fatal: close() disconnects the CDP client link only —
                    # it does NOT kill the user's actual Chrome process. If it
                    # raises we still want to clear local state below.
                    _dlog("disconnect_browser_close_error", error=str(e))
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception as e:
                    _dlog("disconnect_playwright_stop_error", error=str(e))
            prev_url = self.cdp_url
            self._browser = None
            self._page = None
            self._playwright = None
            self.cdp_url = None
            _dlog("disconnect_ok", cdp_url=prev_url)
            return self._status_locked()

    def status(self) -> dict:
        with self.lock:
            return self._status_locked()

    def _status_locked(self) -> dict:
        page_url = None
        if self._page is not None:
            try:
                page_url = self._page.url
            except Exception:
                page_url = None
        return {
            "connected": self._browser is not None,
            "cdp_url": self.cdp_url,
            "page_url": page_url,
        }


# Single process-wide instance — one Chrome connection at a time, matching
# the one-user-one-machine local-install usage model.
_state = _PickerState()


def connect(cdp_url: str) -> dict:
    return _state.connect(cdp_url)


def screenshot_b64() -> str:
    raw = _state.screenshot()
    return base64.b64encode(raw).decode("ascii")


def pick(x: float, y: float) -> dict:
    return _state.pick(x, y)


def disconnect() -> dict:
    return _state.disconnect()


def status() -> dict:
    return _state.status()
