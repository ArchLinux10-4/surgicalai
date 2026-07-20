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
import os
import platform
import queue
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional
from urllib.parse import urlparse

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
        # Live-view screencast (see start_screencast/stop_screencast below).
        # Guarded by its own lock — separate from the connect/disconnect lock
        # above — because frame delivery must not block on a long-held
        # `self.lock` the way pick()/screenshot() legitimately do.
        self._cdp_session = None       # playwright CDPSession, when streaming
        self._screencast_lock = threading.Lock()
        # Bounded to 2: a screencast frame handler runs on Playwright's own
        # dispatch thread, not ours. Keeping only the newest 1-2 frames (and
        # dropping older ones on overflow) means a slow consumer never builds
        # an unbounded backlog / memory leak — we always show the latest
        # frame, never a queue of stale ones.
        self._frame_queue: "queue.Queue[dict]" = queue.Queue(maxsize=2)

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

            pw = None
            try:
                pw = sync_playwright().start()
                # `no_defaults=True` (Playwright >=1.60) tells Playwright not
                # to issue its own overrides (Browser.setDownloadBehavior,
                # focus emulation, media emulation) on the pre-existing
                # default context when attaching over CDP. Without it, real
                # Chrome (the user's daily-driver browser, not a Playwright-
                # launched one) rejects Browser.setDownloadBehavior with
                # "Browser context management is not supported" and the
                # connection fails outright. This is exactly the documented
                # use case for the flag: "attaching to a user's daily-driver
                # browser where these overrides would interfere with
                # existing browser state."
                # https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp-option-no-defaults
                try:
                    browser = pw.chromium.connect_over_cdp(cdp_url, no_defaults=True)
                except TypeError as te:
                    # Older Playwright (<1.60) installed locally doesn't know
                    # this kwarg yet. Fail soft: retry without it rather than
                    # crashing connect() outright, and log so we can tell the
                    # user to upgrade if they hit the CDP error downstream.
                    _dlog("connect_no_defaults_unsupported_old_playwright", error=str(te))
                    browser = pw.chromium.connect_over_cdp(cdp_url)
                contexts = browser.contexts
                if not contexts or not contexts[0].pages:
                    raise ElementPickerError(
                        "Connected to Chrome, but no open tabs were found. "
                        "Open at least one tab in the target Chrome window."
                    )
                page = contexts[0].pages[0]
            except Exception as e:
                # IMPORTANT: always stop the Playwright driver instance we
                # just started on ANY failure path (bad tabs, connect_over_cdp
                # raising on a bad URL/cert, etc). Leaving it running is what
                # causes the *next* connect attempt to fail with "It looks
                # like you are using Playwright Sync API inside the asyncio
                # loop" — that error is Playwright's own guard against a
                # second sync_playwright().start() while a prior one is
                # still alive, not an asyncio/FastAPI issue. See
                # https://github.com/microsoft/playwright-python/issues/462
                if pw is not None:
                    try:
                        pw.stop()
                    except Exception as stop_err:
                        _dlog("connect_pw_stop_after_failure_error", error=str(stop_err))
                if isinstance(e, ElementPickerError):
                    _dlog("connect_failed_no_tabs", cdp_url=cdp_url)
                    raise
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

    # ── Live view — CDP screencast ───────────────────────────────────────
    #
    # Real technique (not a custom hack): Chrome DevTools Protocol's
    # `Page.startScreencast` streams a continuous sequence of JPEG frames
    # over the same CDP connection Playwright already holds. This is the
    # exact mechanism Chrome's own remote-debugging inspector and hosted
    # "live view" browser tools use — no extra software, no VNC, no
    # extension. Backpressure is built into the protocol: Chrome will not
    # send the next frame until we ack the current one via
    # `Page.screencastFrameAck`, so a slow consumer naturally throttles the
    # frame rate instead of the browser flooding us.
    def start_screencast(self) -> None:
        with self._screencast_lock:
            if self._cdp_session is not None:
                return  # already streaming — idempotent
            if self._page is None:
                raise ElementPickerError("Not connected. Call connect first.")
            try:
                session = self._page.context.new_cdp_session(self._page)

                def _on_frame(event: dict):
                    try:
                        session.send(
                            "Page.screencastFrameAck",
                            {"sessionId": event["sessionId"]},
                        )
                    except Exception as ack_err:
                        _dlog("screencast_ack_failed", error=str(ack_err))
                    frame = {
                        "data": event.get("data", ""),
                        "metadata": event.get("metadata", {}),
                    }
                    # Drop the oldest queued frame on overflow rather than
                    # blocking the CDP dispatch thread or growing unbounded.
                    try:
                        self._frame_queue.put_nowait(frame)
                    except queue.Full:
                        try:
                            self._frame_queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._frame_queue.put_nowait(frame)
                        except queue.Full:
                            pass

                session.on("Page.screencastFrame", _on_frame)
                session.send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": 70,
                        "maxWidth": 1400,
                        "maxHeight": 900,
                        "everyNthFrame": 1,
                    },
                )
                self._cdp_session = session
                _dlog("screencast_started")
            except Exception as e:
                _dlog("screencast_start_failed", error=str(e))
                raise ElementPickerError(f"Could not start live view: {e}") from e

    def stop_screencast(self) -> None:
        with self._screencast_lock:
            if self._cdp_session is None:
                return
            try:
                self._cdp_session.send("Page.stopScreencast")
            except Exception as e:
                _dlog("screencast_stop_error", error=str(e))
            self._cdp_session = None
            # Drain any queued frames so a later restart doesn't hand back
            # a stale frame from the previous session.
            try:
                while True:
                    self._frame_queue.get_nowait()
            except queue.Empty:
                pass
            _dlog("screencast_stopped")

    def get_next_frame(self, timeout: float = 1.0) -> Optional[dict]:
        """Blocking pop from the frame queue — call via a worker thread from
        async code (e.g. `asyncio.to_thread`), never directly on the event
        loop. Returns None on timeout (caller should just poll again)."""
        try:
            return self._frame_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def navigate(self, url: str) -> dict:
        with self.lock:
            if self._page is None:
                raise ElementPickerError("Not connected. Call connect first.")
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                _dlog("navigate_failed", url=url, error=str(e))
                raise ElementPickerError(f"Could not navigate to {url}: {e}") from e
            _dlog("navigate_ok", url=url)
            return self._status_locked()

    def dispatch_mouse(self, kind: str, x: float, y: float, button: str = "left",
                        delta_x: float = 0.0, delta_y: float = 0.0) -> None:
        """Relay a real mouse event (move/down/up/wheel) into the live page —
        used for Browse-mode scrolling/clicking, never for Pick mode (which
        stays non-mutating via pick()/elementFromPoint)."""
        with self._screencast_lock:
            session = self._cdp_session
        if session is None:
            raise ElementPickerError("Live view is not active.")
        try:
            params = {"type": kind, "x": x, "y": y, "button": button, "clickCount": 1}
            if kind == "mouseWheel":
                params = {"type": "mouseWheel", "x": x, "y": y,
                          "deltaX": delta_x, "deltaY": delta_y}
            session.send("Input.dispatchMouseEvent", params)
        except Exception as e:
            _dlog("dispatch_mouse_failed", kind=kind, error=str(e))
            raise ElementPickerError(f"Mouse event failed: {e}") from e

    def dispatch_text(self, text: str) -> None:
        """Relay real typed characters into the focused field on the live
        page (e.g. typing into a search box while browsing to the target)."""
        with self._screencast_lock:
            session = self._cdp_session
        if session is None:
            raise ElementPickerError("Live view is not active.")
        try:
            session.send("Input.insertText", {"text": text})
        except Exception as e:
            _dlog("dispatch_text_failed", error=str(e))
            raise ElementPickerError(f"Type event failed: {e}") from e

    def dispatch_key(self, key: str, code: str) -> None:
        """Relay a special key (Enter/Backspace/Tab/Arrow*) into the live
        page — insertText alone can't express these."""
        with self._screencast_lock:
            session = self._cdp_session
        if session is None:
            raise ElementPickerError("Live view is not active.")
        try:
            session.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "code": code})
            session.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code})
        except Exception as e:
            _dlog("dispatch_key_failed", key=key, error=str(e))
            raise ElementPickerError(f"Key event failed: {e}") from e

    def disconnect(self) -> dict:
        with self.lock:
            # Stop any live-view streaming before tearing down the CDP
            # connection it depends on.
            try:
                self.stop_screencast()
            except Exception as e:
                _dlog("disconnect_stop_screencast_error", error=str(e))
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


# ─── Launch — one-click Chrome start, no Terminal required ──────────────────
#
# UX problem this solves: connect() only *attaches* to an already-running
# debug-mode Chrome. Asking a non-technical user to open a terminal and type
# `chrome --remote-debugging-port=9222` is a support-ticket generator.
#
# Design: launch a SEPARATE Chrome window with its own dedicated profile
# directory (~/.surgicalai/picker-chrome-profile), not the user's everyday
# Chrome profile. This means:
#   - The user's normal Chrome window is never touched, quit, or restarted —
#     no risk of losing their open tabs/session.
#   - The picker window starts logged out the first time (fresh profile),
#     but the profile directory persists across launches, so any logins the
#     user does inside the picker window are remembered next time.
#   - Only supported/target platforms: Debian Linux and macOS (M1/Intel).
#
# If port is already open (e.g. power user already started Chrome with the
# flag themselves), launch() is a no-op and the caller proceeds straight to
# connect() — the manual/advanced path still works unchanged.

_CHROME_MAC_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
_CHROME_LINUX_BINARY_NAMES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]

_PICKER_PROFILE_DIR = os.path.expanduser("~/.surgicalai/picker-chrome-profile")


def _find_chrome_binary() -> Optional[str]:
    system = platform.system()
    if system == "Darwin":
        for path in _CHROME_MAC_PATHS:
            if os.path.exists(path):
                return path
        return None
    if system == "Linux":
        for name in _CHROME_LINUX_BINARY_NAMES:
            path = shutil.which(name)
            if path:
                return path
        return None
    # Unsupported platform (target machines are Debian Linux + macOS only).
    return None


def _port_open(port: int, host: str = "localhost") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _parse_port(cdp_url: str) -> int:
    try:
        parsed = urlparse(cdp_url)
        return parsed.port or 9222
    except Exception:
        return 9222


def launch(cdp_url: str = "http://localhost:9222") -> dict:
    """Ensure a debug-mode Chrome is reachable at cdp_url's port, launching a
    dedicated picker-profile Chrome window if nothing is listening yet.
    Never touches the user's regular Chrome process."""
    port = _parse_port(cdp_url)

    if _port_open(port):
        _dlog("launch_already_running", port=port)
        return {"launched": False, "already_running": True}

    binary = _find_chrome_binary()
    if not binary:
        _dlog("launch_no_chrome_found", platform=platform.system())
        raise ElementPickerError(
            "Could not find a Chrome installation to launch automatically. "
            "Install Google Chrome, or start it yourself with "
            f"`--remote-debugging-port={port}` and click Connect."
        )

    os.makedirs(_PICKER_PROFILE_DIR, exist_ok=True)
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={_PICKER_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "about:blank",
    ]
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        _dlog("launch_popen_failed", error=str(e), binary=binary)
        raise ElementPickerError(f"Failed to launch Chrome: {e}") from e

    # First launch of a fresh profile can take a couple seconds — poll
    # instead of assuming it's instantly ready.
    deadline = time.time() + 12
    while time.time() < deadline:
        if _port_open(port):
            _dlog("launch_ready", port=port, binary=binary)
            return {"launched": True, "already_running": False}
        time.sleep(0.3)

    _dlog("launch_timeout", port=port, binary=binary)
    raise ElementPickerError(
        "Chrome was launched but didn't become reachable in time. "
        "Try clicking Connect again in a moment."
    )


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


def start_screencast() -> None:
    _state.start_screencast()


def stop_screencast() -> None:
    _state.stop_screencast()


def get_next_frame(timeout: float = 1.0) -> Optional[dict]:
    return _state.get_next_frame(timeout=timeout)


def navigate(url: str) -> dict:
    return _state.navigate(url)


def dispatch_mouse(kind: str, x: float, y: float, button: str = "left",
                    delta_x: float = 0.0, delta_y: float = 0.0) -> None:
    _state.dispatch_mouse(kind, x, y, button=button, delta_x=delta_x, delta_y=delta_y)


def dispatch_text(text: str) -> None:
    _state.dispatch_text(text)


def dispatch_key(key: str, code: str) -> None:
    _state.dispatch_key(key, code)
