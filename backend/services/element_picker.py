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
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse

# Playwright's sync API is explicitly documented as NOT thread-safe: every
# call touching a Playwright object (browser/page/cdp session) must happen
# on the exact same OS thread that first called `sync_playwright().start()`,
# or it raises `greenlet.error: Cannot switch to a different thread`.
#
# Our callers do not naturally share one thread: `POST /connect` lands on
# whichever FastAPI/anyio threadpool worker services that request, while the
# WebSocket handler drives `start_screencast`/`navigate`/mouse-dispatch via
# `asyncio.to_thread`, which pulls from asyncio's own separate default
# executor. Two different thread pools racing to touch the same Playwright
# objects is exactly what production hit (see connect_ok immediately
# followed by screencast_start_failed in the debug log).
#
# Fix: pin every Playwright-touching call onto one dedicated single-worker
# executor, created once for the process lifetime. Because it has exactly
# one worker thread, every job submitted to it — no matter which request
# thread or asyncio task submitted it — always runs on that same OS thread.
# This makes the failure mode structurally impossible rather than papering
# over one call site.
_PW_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="element-picker-pw")


def _run_on_pw_thread(fn, *args, **kwargs):
    """Submit fn to the single dedicated Playwright thread and block for the
    result, re-raising any exception as-is so existing ElementPickerError /
    HTTPException handling upstream is untouched."""
    return _PW_EXECUTOR.submit(fn, *args, **kwargs).result()

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
        # Live hover-highlight (Pick mode). Tracked so we can re-assert it
        # after navigate()/reload() — `Overlay.setInspectMode` is UI state
        # tied to the current document and is not guaranteed to survive a
        # full navigation, so we defensively re-send it every time rather
        # than assume persistence.
        self._inspect_mode_enabled = False
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
                # Bounded retry (defense-in-depth): on a freshly-launched
                # Chrome (fresh profile dir), the CDP port can accept a
                # connection before the initial `about:blank` tab has
                # finished registering as a page target. Give it up to 3s
                # to appear rather than failing on the very first check —
                # this mirrors the same race launch() now guards against
                # (see _cdp_has_page_target), but connect() can also be
                # called standalone (power-user manual-flag path) so it
                # needs its own guard too.
                page = None
                retry_deadline = time.time() + 3.0
                attempts = 0
                while True:
                    attempts += 1
                    contexts = browser.contexts
                    if contexts and contexts[0].pages:
                        page = contexts[0].pages[0]
                        break
                    if time.time() >= retry_deadline:
                        break
                    time.sleep(0.25)
                if page is None:
                    # The Chrome process is alive (we got this far) but has
                    # zero open tabs. This is a real, common state: on macOS,
                    # closing a window's last tab does NOT quit Chrome — the
                    # process (and the CDP port) stays up with no tabs at
                    # all. Waiting longer never helps here, so create a tab
                    # ourselves instead of failing outright.
                    try:
                        if browser.contexts:
                            page = browser.contexts[0].new_page()
                        else:
                            page = browser.new_context().new_page()
                        page.goto("about:blank")
                        _dlog("connect_created_new_page", cdp_url=cdp_url, attempts=attempts)
                    except Exception as create_err:
                        _dlog(
                            "connect_create_page_failed",
                            cdp_url=cdp_url,
                            attempts=attempts,
                            error=str(create_err),
                        )
                if page is None:
                    _dlog("connect_no_tabs_after_retry", cdp_url=cdp_url, attempts=attempts)
                    raise ElementPickerError(
                        "Connected to Chrome, but no open tabs were found and "
                        "a new tab could not be created automatically. Please "
                        "quit Chrome completely and click Launch again."
                    )
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
    def start_screencast(self, max_width: int = 1400, max_height: int = 900, quality: int = 85) -> None:
        # Resolution/quality are caller-supplied (see routers/element_picker.py
        # ws_stream, which sizes these from the client's actual panel size ×
        # devicePixelRatio) so a Retina/HiDPI screen gets a genuinely sharp
        # frame instead of a fixed 1400×900 JPEG stretched to fill a much
        # larger CSS box — that upscaling was the exact cause of the
        # "blurry" complaint. Clamped server-side regardless of what the
        # client sends, so a bad value can never request an unreasonably
        # large/slow encode.
        max_width = max(320, min(int(max_width), 2400))
        max_height = max(240, min(int(max_height), 1600))
        quality = max(40, min(int(quality), 95))
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
                        # Wall-clock capture time — lets the WS sender compute a
                        # real capture-to-wire latency number instead of guessing
                        # where time is going (encode vs. queue vs. network).
                        "_captured_at": time.time(),
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
                # Chrome only treats scroll/wheel input as "live" on a page it
                # considers focused+active. Our real Chrome window is a
                # background window on the user's desktop (the live view
                # panel is what they actually interact with), so without
                # this Chrome periodically stops honoring CDP-dispatched
                # mouseWheel events until the user manually clicks the real
                # window to give it OS focus — proven root cause of "scroll
                # stops working, have to click the real Chrome tab and back".
                # `Emulation.setFocusEmulationEnabled` (official CDP,
                # Emulation domain) tells Chrome to simulate a permanently
                # focused+active page regardless of real OS window focus, so
                # every dispatched event keeps working continuously. Non-fatal
                # if unsupported on an old Chrome — live view still works,
                # just may re-exhibit the background-focus quirk.
                try:
                    session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
                except Exception as focus_err:
                    _dlog("focus_emulation_enable_failed", error=str(focus_err))
                session.send(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": quality,
                        "maxWidth": max_width,
                        "maxHeight": max_height,
                        "everyNthFrame": 1,
                    },
                )
                self._cdp_session = session
                _dlog("screencast_started", max_width=max_width, max_height=max_height, quality=quality, focus_emulation=True)
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
            try:
                # Best-effort: hand focus emulation back to the real OS
                # window state now that the live view session is ending.
                # Non-fatal — the CDP session detaching normally clears this
                # anyway, this is just belt-and-suspenders cleanup.
                self._cdp_session.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
            except Exception:
                pass
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
            if self._inspect_mode_enabled:
                with self._screencast_lock:
                    session = self._cdp_session
                if session is not None:
                    try:
                        self._apply_inspect_mode_locked(session, True)
                    except Exception:
                        pass  # already _dlog'd inside; navigation itself still succeeded
            return self._status_locked()

    def reload(self, hard: bool = False) -> dict:
        """Reload the live page. `hard=True` bypasses the browser cache
        (the CDP equivalent of Ctrl/Cmd+Shift+R) — needed for frontend dev
        loops where the page under test is served with cache-friendly
        headers and a plain reload would keep showing stale JS/CSS. Uses
        the same CDP session the screencast already holds (Playwright's
        sync `Page.reload()` has no cache-bypass option), so this only
        works while live view is active — same precondition as every other
        `dispatch_*`/session-based call in this class."""
        with self.lock:
            if self._page is None:
                raise ElementPickerError("Not connected. Call connect first.")
            with self._screencast_lock:
                session = self._cdp_session
            if session is None:
                raise ElementPickerError("Live view is not active — cannot reload.")
            try:
                # Page.enable is idempotent (safe to call repeatedly) and
                # ensures the reload command is honored even if this CDP
                # session hasn't had the Page domain explicitly enabled yet.
                session.send("Page.enable")
                session.send("Page.reload", {"ignoreCache": bool(hard)})
            except Exception as e:
                _dlog("reload_failed", hard=hard, error=str(e))
                raise ElementPickerError(f"Could not reload page: {e}") from e
            _dlog("reload_ok", hard=hard)
            if self._inspect_mode_enabled:
                self._apply_inspect_mode_locked(session, True)
            return self._status_locked()

    def _apply_inspect_mode_locked(self, session, enabled: bool) -> None:
        """Real technique (not a custom hack): this is the exact mechanism
        Chrome's own DevTools "inspect element" tool uses — confirmed
        against the official CDP Overlay domain docs
        (chromedevtools.github.io/devtools-protocol/tot/Overlay/) and a
        working reference implementation (Stack Overflow #59710285).

        `Overlay.setInspectMode(mode='searchForNode')` makes Chrome itself
        track whatever DOM node is under the mouse (driven by the
        `Input.dispatchMouseEvent(mouseMoved)` calls `dispatch_mouse()`
        already sends) and paint a highlight box as part of its own
        rendering — i.e. it shows up for free in the existing
        `Page.startScreencast` JPEG frames. No new rendering pipeline, no
        frontend canvas-drawing code, no extra wire format.

        Requires `DOM.enable` + `Overlay.enable` first (both idempotent —
        official docs: "Enables domain notifications", safe to call every
        time). Must be explicitly turned back off with mode='none' — it
        does NOT auto-exit after a pick (documented gap, confirmed by
        real-world reports), and while it's on Chrome intercepts clicks
        for node-selection instead of letting them reach the page — which
        is exactly Pick-mode's desired semantics, but wrong for Browse
        mode, so callers must only enable this while in Pick mode and
        must disable it the instant the user switches back to Browse.
        """
        try:
            session.send("DOM.enable")
            session.send("Overlay.enable")
            if enabled:
                session.send(
                    "Overlay.setInspectMode",
                    {
                        "mode": "searchForNode",
                        "highlightConfig": {
                            "showInfo": True,
                            "showExtensionLines": False,
                            # Matches the app's existing accent color
                            # (--c-accent: 88 166 255 / #58a6ff) used for
                            # the Pick-mode ring/badge elsewhere in
                            # ElementPickerPanel.tsx, so the live highlight
                            # visually matches the rest of the picker UI
                            # instead of introducing an unrelated color.
                            "contentColor": {"r": 88, "g": 166, "b": 255, "a": 0.20},
                            "borderColor": {"r": 88, "g": 166, "b": 255, "a": 0.9},
                        },
                    },
                )
            else:
                session.send("Overlay.setInspectMode", {"mode": "none"})
        except Exception as e:
            _dlog("inspect_mode_apply_failed", enabled=enabled, error=str(e))
            raise

    def set_inspect_mode(self, enabled: bool) -> dict:
        """Turn the live hover-highlight on/off. Requires an active
        screencast session (same precondition as reload()) since the
        highlight is only visible through the live-view frames."""
        with self.lock:
            if self._page is None:
                raise ElementPickerError("Not connected. Call connect first.")
            with self._screencast_lock:
                session = self._cdp_session
            if session is None:
                raise ElementPickerError("Live view is not active — cannot set inspect mode.")
            try:
                self._apply_inspect_mode_locked(session, enabled)
            except Exception as e:
                raise ElementPickerError(f"Could not set inspect mode: {e}") from e
            self._inspect_mode_enabled = bool(enabled)
            _dlog("inspect_mode_set", enabled=enabled)
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
            # Reset so a future connect()+navigate() doesn't silently try to
            # re-assert inspect mode from a stale prior session's state.
            self._inspect_mode_enabled = False
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


def _cdp_has_page_target(port: int, host: str = "localhost") -> bool:
    """True once Chrome's CDP endpoint actually lists a `page`-type target.

    The debug port can start accepting TCP connections before the initial
    tab (e.g. the `about:blank` opened by launch()) has finished registering
    as a CDP-attachable target — especially on a brand-new profile dir,
    where Chrome is still doing first-run profile setup. `_port_open()`
    alone is not sufficient readiness evidence; this closes that gap.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://{host}:{port}/json/list", timeout=0.5) as resp:
            targets = json.loads(resp.read().decode("utf-8", "replace"))
        return any(isinstance(t, dict) and t.get("type") == "page" for t in targets)
    except Exception as e:
        _dlog("cdp_has_page_target_check_failed", port=port, error=str(e))
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
    # instead of assuming it's instantly ready. Readiness requires BOTH the
    # TCP port accepting connections AND the CDP endpoint listing an actual
    # page target; the port alone opens before the first tab is registered,
    # which raced connect() into a false "no open tabs" failure.
    deadline = time.time() + 12
    while time.time() < deadline:
        if _port_open(port) and _cdp_has_page_target(port):
            _dlog("launch_ready", port=port, binary=binary)
            return {"launched": True, "already_running": False}
        time.sleep(0.3)

    _dlog("launch_timeout", port=port, binary=binary)
    raise ElementPickerError(
        "Chrome was launched but didn't become reachable in time. "
        "Try clicking Connect again in a moment."
    )


def connect(cdp_url: str) -> dict:
    # Touches Playwright (sync_playwright().start() on first call) — must
    # run on the single dedicated Playwright thread. See _PW_EXECUTOR above.
    return _run_on_pw_thread(_state.connect, cdp_url)


def screenshot_b64() -> str:
    raw = _run_on_pw_thread(_state.screenshot)
    return base64.b64encode(raw).decode("ascii")


def pick(x: float, y: float) -> dict:
    return _run_on_pw_thread(_state.pick, x, y)


def disconnect() -> dict:
    return _run_on_pw_thread(_state.disconnect)


def status() -> dict:
    # NOTE: touches `self._page.url`, a real Playwright property getter, not
    # a plain Python attribute — must go through the single dedicated
    # Playwright thread like every other call here.
    return _run_on_pw_thread(_state.status)


def start_screencast(max_width: int = 1400, max_height: int = 900, quality: int = 85) -> None:
    _run_on_pw_thread(_state.start_screencast, max_width=max_width, max_height=max_height, quality=quality)


def stop_screencast() -> None:
    _run_on_pw_thread(_state.stop_screencast)


def get_next_frame(timeout: float = 1.0) -> Optional[dict]:
    # Only reads from the bounded frame queue that Playwright's own event
    # dispatcher fills — no direct Playwright object access, safe from any
    # thread, no dispatch needed (and must stay off the single PW thread so
    # a slow consumer never blocks screencast frame delivery).
    return _state.get_next_frame(timeout=timeout)


def navigate(url: str) -> dict:
    return _run_on_pw_thread(_state.navigate, url)


def reload(hard: bool = False) -> dict:
    return _run_on_pw_thread(_state.reload, hard=hard)


def set_inspect_mode(enabled: bool) -> dict:
    return _run_on_pw_thread(_state.set_inspect_mode, enabled)


def dispatch_mouse(kind: str, x: float, y: float, button: str = "left",
                    delta_x: float = 0.0, delta_y: float = 0.0) -> None:
    _run_on_pw_thread(_state.dispatch_mouse, kind, x, y, button=button, delta_x=delta_x, delta_y=delta_y)


def dispatch_text(text: str) -> None:
    _run_on_pw_thread(_state.dispatch_text, text)


def dispatch_key(key: str, code: str) -> None:
    _run_on_pw_thread(_state.dispatch_key, key, code)
