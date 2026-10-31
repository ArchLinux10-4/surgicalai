"""Local in-app browser.

One headless Chromium per process, driven with Playwright's async API on the
uvicorn loop. The page is screencast to the Browser tab. Nothing here imports
Playwright until start(), so a hosted deploy that never installed it still boots.

Hosted (Railway / Vercel, DATABASE_URL postgres) and SURGICALAI_BROWSER_DISABLED
both turn the feature off. Callers still have to 404; start() refuses too.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_ALLOWED_PREFIXES = ("http://", "https://", "about:")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CSS_PROP = re.compile(r"^[a-z-]{1,40}$")
_MOUSE_TYPES = {"mouseMoved", "mousePressed", "mouseReleased", "mouseWheel"}
_KEY_TYPES = {"keyDown", "keyUp", "char"}
_MAX_TABS = 5


@dataclass
class _Tab:
    page: object
    cdp: object = None
    url: str = ""
    display_url: str = ""
    title: str = ""
    favicon: str = ""
    loading: bool = False
    history: list = field(default_factory=list)
    history_idx: int = -1
    history_nav: bool = False


class BrowserError(RuntimeError):
    """Surfaced to the client as a 400, or as a nav_error event."""


def browser_enabled() -> bool:
    """False on Railway/Vercel and when the escape hatch is set.

    Reads USE_POSTGRES off the database module at call time so tests can
    monkeypatch it without reimporting this module.
    """
    flag = os.environ.get("SURGICALAI_BROWSER_DISABLED", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return False
    import database
    return not bool(getattr(database, "USE_POSTGRES", False))


def _dlog(event: str, **kwargs) -> None:
    try:
        from database import _dlog as emit
        emit(event, **kwargs)
    except Exception:
        pass


def normalize_url(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise BrowserError("Enter a URL.")
    if len(text) > 2000:
        raise BrowserError("That URL is too long.")
    lower = text.lower()
    if lower.startswith(_ALLOWED_PREFIXES):
        url = text
    elif re.match(r"^(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?(/|$)", lower):
        url = "http://" + text
    elif re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", text):
        raise BrowserError("Only http and https pages can be opened.")
    else:
        url = "https://" + text
    if not url.lower().startswith(_ALLOWED_PREFIXES):
        raise BrowserError("Only http and https pages can be opened.")
    return url


def rewrite_for_docker(url: str) -> tuple[str, str]:
    """Return (url the container opens, url shown in the address bar).

    Inside Docker, the page's localhost is the container, not the user's
    dev server. host.docker.internal is the host machine. The address bar
    keeps the localhost the user typed.
    """
    display = url
    if os.environ.get("SURGICALAI_IN_DOCKER") != "1":
        return url, display
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host not in _LOCAL_HOSTS:
        return url, display
    port = f":{parts.port}" if parts.port else ""
    user = ""
    if parts.username:
        user = parts.username
        if parts.password:
            user += ":" + parts.password
        user += "@"
    nav = urlunsplit((parts.scheme, f"{user}host.docker.internal{port}", parts.path, parts.query, parts.fragment))
    return nav, display


def display_from_live_url(url: str) -> str:
    """Turn the container's host.docker.internal back into localhost for the bar."""
    if not url or os.environ.get("SURGICALAI_IN_DOCKER") != "1":
        return url or ""
    parts = urlsplit(url)
    if (parts.hostname or "") != "host.docker.internal":
        return url
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((parts.scheme, f"localhost{port}", parts.path, parts.query, parts.fragment))


def clamp_viewport(w, h, dpr) -> tuple[int, int, float]:
    try:
        width = int(float(w))
    except (TypeError, ValueError):
        width = 1280
    try:
        height = int(float(h))
    except (TypeError, ValueError):
        height = 800
    try:
        scale = float(dpr or 1)
    except (TypeError, ValueError):
        scale = 1.0
    return max(320, min(3840, width)), max(240, min(2400, height)), max(1.0, min(3.0, scale))


def enqueue_latest(q: asyncio.Queue, item) -> None:
    """Keep the newest item. A slow consumer never builds a backlog."""
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass


def coalesce_continuous(pending: dict, kind: str, msg: dict) -> None:
    """Merge mouse-move and wheel bursts. Wheel deltas accumulate."""
    x = float(msg.get("x") or 0)
    y = float(msg.get("y") or 0)
    existing = pending.get(kind)
    if kind == "mouseWheel" and existing is not None:
        existing["x"] = x
        existing["y"] = y
        existing["dx"] = float(existing.get("dx") or 0) + float(msg.get("dx") or msg.get("deltaX") or 0)
        existing["dy"] = float(existing.get("dy") or 0) + float(msg.get("dy") or msg.get("deltaY") or 0)
        return
    pending[kind] = {
        "x": x,
        "y": y,
        "button": msg.get("button") or "left",
        "buttons": int(msg.get("buttons") or 0),
        "clickCount": int(msg.get("clickCount") or 0),
        "modifiers": int(msg.get("modifiers") or 0),
        "dx": float(msg.get("dx") or msg.get("deltaX") or 0),
        "dy": float(msg.get("dy") or msg.get("deltaY") or 0),
    }


def picked_node_from_payload(payload: dict, screenshot_b64: str | None = None, page_url: str = "") -> dict:
    payload = payload or {}
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    rect = payload.get("rect") if isinstance(payload.get("rect"), dict) else {}
    classes = payload.get("classes") if isinstance(payload.get("classes"), list) else []
    attributes = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
    computed = payload.get("computed") if isinstance(payload.get("computed"), dict) else {}
    react = payload.get("react") if isinstance(payload.get("react"), dict) else None
    el_id = payload.get("elId") or payload.get("id") or None
    return {
        "tag": str(payload.get("tag") or "div"),
        "el_id": el_id or None,
        "classes": [str(c) for c in classes][:40],
        "attributes": {str(k): str(v)[:200] for k, v in list(attributes.items())[:40]},
        "text": str(payload.get("text") or "")[:500],
        "outer_html": str(payload.get("outerHTML") or payload.get("outer_html") or "")[:4000],
        "css_path": str(payload.get("cssPath") or payload.get("css_path") or ""),
        "xpath": str(payload.get("xpath") or ""),
        "rect": {
            "x": float(rect.get("x") or 0),
            "y": float(rect.get("y") or 0),
            "width": float(rect.get("width") or 0),
            "height": float(rect.get("height") or 0),
        },
        "computed": {str(k): str(v) for k, v in computed.items()},
        "react": react,
        "page_url": page_url or str(payload.get("pageUrl") or payload.get("page_url") or ""),
        "screenshot_png_b64": screenshot_b64,
        "token": str(payload.get("token") or ""),
        "shift_key": bool(payload.get("shiftKey") or payload.get("shift_key")),
    }


def load_pick_script() -> str:
    return Path(__file__).with_name("inapp_browser_script.js").read_text(encoding="utf-8")


def _running_as_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _launch_args() -> list[str]:
    args = ["--disable-dev-shm-usage", "--disable-gpu", "--font-render-hinting=none"]
    if os.environ.get("SURGICALAI_IN_DOCKER") == "1" or _running_as_root():
        args.append("--no-sandbox")
    return args


def _friendly_launch_error(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, ImportError) or "playwright" in text.lower() or "Executable doesn't exist" in text:
        return (
            "Chromium is not installed. Rebuild the local app with ./docker-rebuild-local.sh. "
            + text
        )
    return text


def _clean_token(token: str) -> str:
    token = str(token or "")
    if not _TOKEN.match(token):
        raise BrowserError("That element is no longer on the page.")
    return token


def _clean_css(css: dict) -> dict:
    out = {}
    for key, value in (css or {}).items():
        name = str(key).strip().lower()
        if not _CSS_PROP.match(name):
            continue
        out[name] = str(value)[:200]
    return out


class BrowserSession:
    """One Chromium, up to five tabs. Idle-closes after ten minutes with no viewers.

    The active tab's page, CDP session, URL, and history are exposed as
    ``_page``, ``_cdp``, ``_url``, and the rest, so callers keep using those
    names. Assigning one of them before any tab exists creates a tab.
    """

    def __init__(self):
        self.idle_seconds = 600
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self._context = None
        self._tabs: list[_Tab] = []
        self._active = 0
        self._creating_page = False
        self._screencast_on = False
        self._subscribers: set[asyncio.Queue] = set()
        self._frame_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._console: deque = deque(maxlen=500)
        self._pick_mode = False
        self._clients = 0
        self._idle_task: asyncio.Task | None = None
        self._viewport = (1280, 800)
        self._dpr = 1.0
        self._last_error: str | None = None
        self._stopping = False

    def _active_tab(self) -> _Tab | None:
        if not self._tabs:
            return None
        index = min(max(self._active, 0), len(self._tabs) - 1)
        return self._tabs[index]

    def _ensure_tab(self) -> _Tab:
        tab = self._active_tab()
        if tab is None:
            tab = _Tab(page=None)
            self._tabs.append(tab)
            self._active = 0
        return tab

    def _tab_attr(self, name: str, default):
        tab = self._active_tab()
        if tab is None:
            return default() if callable(default) else default
        return getattr(tab, name)

    def _set_tab_attr(self, name: str, value) -> None:
        setattr(self._ensure_tab(), name, value)

    @property
    def _page(self):
        return self._tab_attr("page", None)

    @_page.setter
    def _page(self, value):
        self._set_tab_attr("page", value)

    @property
    def _cdp(self):
        return self._tab_attr("cdp", None)

    @_cdp.setter
    def _cdp(self, value):
        self._set_tab_attr("cdp", value)

    @property
    def _url(self) -> str:
        return self._tab_attr("url", "")

    @_url.setter
    def _url(self, value):
        self._set_tab_attr("url", value or "")

    @property
    def _display_url(self) -> str:
        return self._tab_attr("display_url", "")

    @_display_url.setter
    def _display_url(self, value):
        self._set_tab_attr("display_url", value or "")

    @property
    def _title(self) -> str:
        return self._tab_attr("title", "")

    @_title.setter
    def _title(self, value):
        self._set_tab_attr("title", value or "")

    @property
    def _favicon(self) -> str:
        return self._tab_attr("favicon", "")

    @_favicon.setter
    def _favicon(self, value):
        self._set_tab_attr("favicon", value or "")

    @property
    def _loading(self) -> bool:
        return self._tab_attr("loading", False)

    @_loading.setter
    def _loading(self, value):
        self._set_tab_attr("loading", bool(value))

    @property
    def _history(self) -> list:
        return self._tab_attr("history", list)

    @_history.setter
    def _history(self, value):
        self._set_tab_attr("history", list(value or []))

    @property
    def _history_idx(self) -> int:
        return self._tab_attr("history_idx", -1)

    @_history_idx.setter
    def _history_idx(self, value):
        self._set_tab_attr("history_idx", int(value))

    @property
    def _history_nav(self) -> bool:
        return self._tab_attr("history_nav", False)

    @_history_nav.setter
    def _history_nav(self, value):
        self._set_tab_attr("history_nav", bool(value))

    def _tabs_payload(self) -> list[dict]:
        return [
            {
                "index": index,
                "title": tab.title,
                "display_url": tab.display_url,
                "loading": tab.loading,
                "favicon": tab.favicon,
                "active": index == self._active,
            }
            for index, tab in enumerate(self._tabs)
        ]

    @property
    def clients(self) -> int:
        return self._clients

    def status(self) -> dict:
        return {
            "enabled": browser_enabled(),
            "running": self._browser is not None,
            "url": self._url,
            "display_url": self._display_url,
            "title": self._title,
            "favicon": self._favicon,
            "loading": self._loading,
            "can_go_back": self._history_idx > 0,
            "can_go_forward": self._history_idx < len(self._history) - 1,
            "viewport": [self._viewport[0], self._viewport[1]],
            "pick_mode": self._pick_mode,
            "clients": self._clients,
            "last_error": self._last_error,
            "tabs": self._tabs_payload(),
            "active_tab": self._active if self._tabs else 0,
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def next_frame(self) -> dict:
        return await self._frame_queue.get()

    def note_client(self, delta: int) -> None:
        self._clients = max(0, self._clients + delta)
        if self._clients == 0:
            self._arm_idle()
        else:
            self._cancel_idle()

    def _arm_idle(self) -> None:
        if self._clients != 0:
            return
        self._cancel_idle()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_task = loop.create_task(self._idle_shutdown())

    def _cancel_idle(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task and not task.done():
            task.cancel()

    async def _idle_shutdown(self) -> None:
        try:
            await asyncio.sleep(self.idle_seconds)
        except asyncio.CancelledError:
            return
        if self._clients == 0:
            await self.stop()

    async def _broadcast(self, event: dict) -> None:
        for q in list(self._subscribers):
            enqueue_latest(q, event)

    def _remember(self, entry: dict) -> None:
        self._console.append(entry)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._broadcast({"type": "console", "entry": entry}))

    def console_entries(self, since: float = 0) -> list[dict]:
        return [entry for entry in self._console if float(entry.get("ts") or 0) > float(since or 0)]

    def _push_history(self, display: str) -> None:
        if not display:
            return
        if self._history_idx < len(self._history) - 1:
            self._history = self._history[: self._history_idx + 1]
        if self._history and self._history[-1] == display:
            self._history_idx = len(self._history) - 1
            return
        self._history.append(display)
        self._history_idx = len(self._history) - 1

    async def ensure_started(self) -> None:
        if self._browser is not None and self._page is not None:
            return
        await self.start()

    async def start(self) -> dict:
        if not browser_enabled():
            raise BrowserError("Not available on hosted deployments.")
        async with self._lock:
            if self._browser is not None:
                return self.status()
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                self._last_error = _friendly_launch_error(exc)
                raise BrowserError(self._last_error) from exc
            pw = await async_playwright().start()
            try:
                browser = await pw.chromium.launch(headless=True, args=_launch_args())
                width, height = self._viewport
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=self._dpr,
                    ignore_https_errors=True,
                    locale="en-US",
                )
                await context.add_init_script(load_pick_script())
                await context.expose_binding("__surgicalPick", self._on_pick)
                await context.expose_binding("__surgicalHover", self._on_hover)
                self._pw = pw
                self._browser = browser
                self._context = context
                self._tabs = []
                self._active = 0
                context.on("page", lambda extra: asyncio.create_task(self._adopt_popup(extra)))
                await self._open_tab()
                self._last_error = None
                _dlog("browser_started")
            except Exception as exc:
                self._last_error = _friendly_launch_error(exc)
                try:
                    await pw.stop()
                except Exception:
                    pass
                self._pw = self._browser = self._context = None
                self._tabs = []
                self._active = 0
                raise BrowserError(self._last_error) from exc
            return self.status()

    def _wire_tab(self, tab: _Tab) -> None:
        page = tab.page
        page.on("console", lambda msg: self._on_console(msg))
        page.on("pageerror", lambda err: self._on_pageerror(err))
        page.on("requestfailed", lambda req: self._on_requestfailed(req))
        page.on("response", lambda res: asyncio.create_task(self._on_response(res)))
        page.on("framenavigated", lambda frame, t=tab: asyncio.create_task(self._on_frame(frame, t)))
        page.on("load", lambda t=tab: asyncio.create_task(self._on_load(t)))
        page.on("crash", lambda: asyncio.create_task(self._on_crash()))
        page.on("dialog", lambda dialog: asyncio.create_task(self._on_dialog(dialog)))
        page.on("filechooser", lambda chooser: self._on_filechooser(chooser))
        if tab.cdp is not None:
            tab.cdp.on(
                "Page.screencastFrame",
                lambda params, t=tab: asyncio.create_task(self._on_screencast_frame(params, t)),
            )

    async def stop(self) -> dict:
        if self._stopping:
            return self.status()
        self._stopping = True
        self._cancel_idle()
        try:
            async with self._lock:
                pw, browser = self._pw, self._browser
                self._pw = self._browser = self._context = None
                self._tabs = []
                self._active = 0
                self._screencast_on = False
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:
                    pass
            await self._broadcast({"type": "stopped"})
            return self.status()
        finally:
            self._stopping = False

    def _drain_frames(self) -> None:
        while True:
            try:
                self._frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _apply_viewport_to(self, page, cdp) -> None:
        width, height = self._viewport
        if page is not None:
            try:
                await page.set_viewport_size({"width": width, "height": height})
            except Exception:
                pass
        if cdp is None:
            return
        await cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": width,
            "height": height,
            "deviceScaleFactor": self._dpr,
            "mobile": False,
        })

    async def _open_tab(self, page=None) -> int:
        if self._context is None:
            raise BrowserError("Browser is not running.")
        created = page is None
        if created:
            self._creating_page = True
        try:
            if page is None:
                page = await self._context.new_page()
            for index, tab in enumerate(self._tabs):
                if tab.page is page:
                    return index
            cdp = await self._context.new_cdp_session(page)
            await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            await self._apply_viewport_to(page, cdp)
            tab = _Tab(page=page, cdp=cdp)
            self._wire_tab(tab)
            self._tabs.append(tab)
            return len(self._tabs) - 1
        finally:
            if created:
                self._creating_page = False

    async def _broadcast_tabs(self) -> None:
        await self._broadcast({
            "type": "tabs",
            "tabs": self._tabs_payload(),
            "active": self._active if self._tabs else 0,
        })

    async def new_tab(self, url: str | None = None) -> dict:
        await self.ensure_started()
        if len(self._tabs) >= _MAX_TABS:
            raise BrowserError("This browser keeps up to 5 tabs.")
        index = await self._open_tab()
        await self.switch_tab(index)
        if url:
            return await self.navigate(url)
        await self._broadcast_tabs()
        return self.status()

    async def switch_tab(self, index: int) -> dict:
        if index < 0 or index >= len(self._tabs):
            raise BrowserError("No such tab.")
        if index != self._active:
            was_on = self._screencast_on
            old = self._active_tab()
            if was_on and old is not None and old.cdp is not None:
                try:
                    await old.cdp.send("Page.stopScreencast")
                except Exception:
                    pass
                self._screencast_on = False
            self._active = index
            self._drain_frames()
            tab = self._tabs[index]
            if tab.page is not None:
                try:
                    await tab.page.bring_to_front()
                except Exception:
                    pass
            if was_on:
                await self.start_screencast(force=True)
            if self._pick_mode:
                await self.pick_mode(True)
        await self._broadcast_tabs()
        await self._broadcast_nav()
        return self.status()

    async def close_tab(self, index: int) -> dict:
        if index < 0 or index >= len(self._tabs):
            raise BrowserError("No such tab.")
        if len(self._tabs) <= 1:
            raise BrowserError("The last tab stays open.")
        was_active = index == self._active
        was_on = self._screencast_on and was_active
        tab = self._tabs.pop(index)
        if was_on and tab.cdp is not None:
            try:
                await tab.cdp.send("Page.stopScreencast")
            except Exception:
                pass
            self._screencast_on = False
        if tab.page is not None:
            try:
                await tab.page.close()
            except Exception:
                pass
        if self._active > index:
            self._active -= 1
        elif was_active:
            self._active = min(index, len(self._tabs) - 1)
        if was_on:
            self._drain_frames()
            await self.start_screencast(force=True)
        await self._broadcast_tabs()
        await self._broadcast_nav()
        return self.status()

    async def _adopt_popup(self, page) -> None:
        if self._creating_page or any(tab.page is page for tab in self._tabs):
            return
        if len(self._tabs) >= _MAX_TABS:
            self._remember({
                "ts": time.time(),
                "kind": "warn",
                "text": "A popup was closed. This browser keeps 5 tabs.",
                "url": self._display_url or None,
                "line": None,
            })
            try:
                await page.close()
            except Exception:
                pass
            return
        index = await self._open_tab(page)
        await self.switch_tab(index)

    async def navigate(self, url: str) -> dict:
        display = normalize_url(url)
        nav, display = rewrite_for_docker(display)
        await self.ensure_started()
        self._loading = True
        self._last_error = None
        await self._broadcast_nav()
        history_nav = self._history_nav
        self._history_nav = False
        try:
            await self._page.goto(nav, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            self._loading = False
            self._last_error = str(exc)
            await self._broadcast({"type": "nav_error", "message": self._last_error})
            await self._broadcast_nav()
            return self.status()
        self._loading = False
        self._url = nav
        self._display_url = display
        if not history_nav:
            self._push_history(display)
        await self._read_page_meta()
        await self._broadcast_nav()
        await self._broadcast_tabs()
        if self._pick_mode:
            await self.pick_mode(True)
        return self.status()

    async def back(self) -> dict:
        if self._history_idx <= 0:
            return self.status()
        self._history_idx -= 1
        self._history_nav = True
        return await self.navigate(self._history[self._history_idx])

    async def forward(self) -> dict:
        if self._history_idx >= len(self._history) - 1:
            return self.status()
        self._history_idx += 1
        self._history_nav = True
        return await self.navigate(self._history[self._history_idx])

    async def reload(self) -> dict:
        await self.ensure_started()
        self._loading = True
        await self._broadcast_nav()
        try:
            await self._page.reload(wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            self._last_error = str(exc)
            await self._broadcast({"type": "nav_error", "message": self._last_error})
        self._loading = False
        await self._read_page_meta()
        await self._broadcast_nav()
        await self._broadcast_tabs()
        if self._pick_mode:
            await self.pick_mode(True)
        return self.status()

    async def stop_loading(self) -> dict:
        await self.ensure_started()
        if self._cdp is not None:
            try:
                await self._cdp.send("Page.stopLoading")
            except Exception as exc:
                self._last_error = str(exc)
        self._loading = False
        await self._broadcast_nav()
        return self.status()

    async def set_viewport(self, w, h, dpr) -> dict:
        width, height, scale = clamp_viewport(w, h, dpr)
        if (
            abs(width - self._viewport[0]) < 16
            and abs(height - self._viewport[1]) < 16
            and scale == self._dpr
        ):
            return self.status()
        self._viewport = (width, height)
        self._dpr = scale
        for tab in list(self._tabs):
            try:
                await self._apply_viewport_to(tab.page, tab.cdp)
            except Exception as exc:
                self._last_error = str(exc)
        if self._screencast_on:
            await self.start_screencast(force=True)
        return self.status()

    async def start_screencast(self, force: bool = False) -> None:
        await self.ensure_started()
        if self._cdp is None:
            return
        if self._screencast_on and not force:
            return
        if self._screencast_on and force:
            try:
                await self._cdp.send("Page.stopScreencast")
            except Exception:
                pass
            self._screencast_on = False
        width, height = self._viewport
        await self._cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 85,
            "maxWidth": int(width * self._dpr),
            "maxHeight": int(height * self._dpr),
            "everyNthFrame": 1,
        })
        self._screencast_on = True

    async def stop_screencast(self) -> None:
        self._screencast_on = False
        if self._cdp is None:
            return
        try:
            await self._cdp.send("Page.stopScreencast")
        except Exception:
            pass

    async def mouse(self, kind, x, y, button="left", buttons=0, click_count=0, modifiers=0, delta_x=0.0, delta_y=0.0) -> None:
        if kind not in _MOUSE_TYPES:
            return
        await self.ensure_started()
        if self._cdp is None:
            raise BrowserError("Browser is not running.")
        await self._cdp.send("Input.dispatchMouseEvent", {
            "type": kind,
            "x": float(x),
            "y": float(y),
            "button": button or "left",
            "buttons": int(buttons or 0),
            "clickCount": int(click_count or 0),
            "modifiers": int(modifiers or 0),
            "deltaX": float(delta_x or 0),
            "deltaY": float(delta_y or 0),
        })

    async def key(self, kind, key, code, text, modifiers, key_code) -> None:
        if kind not in _KEY_TYPES:
            return
        await self.ensure_started()
        if self._cdp is None:
            raise BrowserError("Browser is not running.")
        payload = {
            "type": kind,
            "modifiers": int(modifiers or 0),
            "key": str(key or ""),
            "code": str(code or ""),
            "windowsVirtualKeyCode": int(key_code or 0),
            "nativeVirtualKeyCode": int(key_code or 0),
        }
        if text and kind in ("keyDown", "char"):
            payload["text"] = str(text)
        await self._cdp.send("Input.dispatchKeyEvent", payload)

    async def insert_text(self, text: str) -> None:
        await self.ensure_started()
        if self._cdp is None or not text:
            return
        await self._cdp.send("Input.insertText", {"text": str(text)[:4000]})

    async def pick_mode(self, on: bool) -> dict:
        self._pick_mode = bool(on)
        if self._page is not None:
            try:
                await self._page.evaluate(
                    "(on) => { if (window.__surgicalSetPick) window.__surgicalSetPick(!!on) }",
                    self._pick_mode,
                )
            except Exception as exc:
                self._last_error = str(exc)
        await self._broadcast({"type": "pick_mode", "on": self._pick_mode})
        return self.status()

    async def element_screenshot(self, rect: dict, page=None) -> str | None:
        page = page or self._page
        if page is None or not rect:
            return None
        try:
            x = float(rect.get("x") or 0)
            y = float(rect.get("y") or 0)
            width = float(rect.get("width") or 0)
            height = float(rect.get("height") or 0)
        except (TypeError, ValueError):
            return None
        if width < 1 or height < 1:
            return None
        pad = 40
        vx, vy = self._viewport
        left = max(0.0, x - pad)
        top = max(0.0, y - pad)
        clip_w = min(width + pad * 2, vx - left)
        clip_h = min(height + pad * 2, vy - top)
        if clip_w < 1 or clip_h < 1:
            return None
        png = await page.screenshot(
            clip={"x": left, "y": top, "width": clip_w, "height": clip_h},
            type="png",
        )
        return base64.b64encode(png).decode("ascii")

    async def screenshot_page(self) -> bytes:
        await self.ensure_started()
        return await self._page.screenshot(type="png")

    async def eval_style(self, token: str, css: dict) -> dict:
        token = _clean_token(token)
        css = _clean_css(css)
        await self.ensure_started()
        await self._page.evaluate(
            """([token, css]) => {
              const el = document.querySelector('[data-surgical-pick="' + token + '"]')
              if (!el) return false
              if (el.dataset.surgicalStyleBackup == null) {
                el.dataset.surgicalStyleBackup = el.getAttribute('style') || ''
              }
              for (const [key, value] of Object.entries(css || {})) {
                el.style.setProperty(key, String(value))
              }
              return true
            }""",
            [token, css],
        )
        return {"ok": True, "token": token}

    async def clear_styles(self, token: str) -> dict:
        token = _clean_token(token)
        await self.ensure_started()
        await self._page.evaluate(
            """(token) => {
              const el = document.querySelector('[data-surgical-pick="' + token + '"]')
              if (!el || el.dataset.surgicalStyleBackup == null) return false
              if (el.dataset.surgicalStyleBackup === '') el.removeAttribute('style')
              else el.setAttribute('style', el.dataset.surgicalStyleBackup)
              delete el.dataset.surgicalStyleBackup
              return true
            }""",
            token,
        )
        return {"ok": True, "token": token}

    def _tab_for_page(self, page) -> _Tab | None:
        if page is None:
            return None
        for tab in self._tabs:
            if tab.page is page:
                return tab
        return None

    async def _on_pick(self, source, *args) -> None:
        payload = args[0] if args else {}
        page = source.get("page") if isinstance(source, dict) else None
        tab = self._tab_for_page(page)
        page_url = (tab.display_url if tab is not None else self._display_url) or ""
        node = picked_node_from_payload(payload, page_url=page_url)
        try:
            node["screenshot_png_b64"] = await self.element_screenshot(
                node.get("rect") or {}, page=page or self._page,
            )
        except Exception:
            node["screenshot_png_b64"] = None
        _dlog("browser_pick", tag=node["tag"], css_path=node["css_path"], has_react=bool(node.get("react")))
        await self._broadcast({"type": "pick", "node": node})

    async def _on_hover(self, source, *args) -> None:
        payload = args[0] if args else {}
        if not isinstance(payload, dict):
            return
        rect = payload.get("rect") if isinstance(payload.get("rect"), dict) else None
        await self._broadcast({"type": "hover", "rect": rect, "tag": payload.get("tag") or ""})
        if payload.get("cursor"):
            await self._broadcast({"type": "cursor", "value": str(payload.get("cursor"))})

    async def _on_screencast_frame(self, params: dict, tab: _Tab | None = None) -> None:
        params = params or {}
        session_id = params.get("sessionId")
        cdp = tab.cdp if tab is not None else self._cdp
        active = self._active_tab()
        if tab is not None and active is not None and tab is not active:
            if cdp is not None and session_id is not None:
                try:
                    await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
                except Exception:
                    pass
            return
        if cdp is not None and session_id is not None:
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        enqueue_latest(self._frame_queue, {
            "data": params.get("data") or "",
            "metadata": params.get("metadata") or {},
            "captured_at": time.time(),
        })

    async def _broadcast_nav(self) -> None:
        await self._broadcast({
            "type": "nav",
            "url": self._url,
            "display_url": self._display_url,
            "title": self._title,
            "favicon": self._favicon,
            "loading": self._loading,
            "can_go_back": self._history_idx > 0,
            "can_go_forward": self._history_idx < len(self._history) - 1,
        })

    async def _read_page_meta(self, tab: _Tab | None = None) -> None:
        tab = tab or self._active_tab()
        if tab is None or tab.page is None:
            return
        try:
            tab.title = await tab.page.title() or ""
        except Exception:
            tab.title = ""
        try:
            tab.favicon = await tab.page.evaluate(
                """() => {
                  const link = document.querySelector('link[rel~="icon"]')
                  return link && link.href ? link.href : ''
                }"""
            ) or ""
        except Exception:
            tab.favicon = ""

    async def _on_frame(self, frame, tab: _Tab | None = None) -> None:
        tab = tab or self._active_tab()
        if tab is None or tab.page is None:
            return
        try:
            if frame != tab.page.main_frame:
                return
            live = frame.url or ""
        except Exception:
            return
        if live and live != "about:blank":
            tab.url = live
            shown = display_from_live_url(live)
            if shown:
                tab.display_url = shown
        tab.loading = True
        if tab is not self._active_tab():
            return
        await self._broadcast_nav()
        await self._broadcast_tabs()
        if self._pick_mode and tab.page is not None:
            try:
                await tab.page.evaluate(
                    "(on) => { if (window.__surgicalSetPick) window.__surgicalSetPick(!!on) }",
                    True,
                )
            except Exception:
                pass

    async def _on_load(self, tab: _Tab | None = None) -> None:
        tab = tab or self._active_tab()
        if tab is None:
            return
        tab.loading = False
        await self._read_page_meta(tab)
        if tab is not self._active_tab():
            return
        await self._broadcast_nav()
        await self._broadcast_tabs()

    async def _on_crash(self) -> None:
        self._last_error = "The page crashed."
        await self._broadcast({"type": "nav_error", "message": self._last_error})
        await self.stop()

    def _on_console(self, msg) -> None:
        loc = getattr(msg, "location", None) or {}
        kind = getattr(msg, "type", "") or "log"
        if kind == "warning":
            kind = "warn"
        elif kind not in ("log", "warn", "error"):
            kind = "log"
        self._remember({
            "ts": time.time(),
            "kind": kind,
            "text": str(getattr(msg, "text", "") or "")[:500],
            "url": loc.get("url") if isinstance(loc, dict) else None,
            "line": loc.get("lineNumber") if isinstance(loc, dict) else None,
        })

    def _on_pageerror(self, err) -> None:
        self._remember({
            "ts": time.time(),
            "kind": "pageerror",
            "text": str(err)[:500],
            "url": self._display_url or None,
            "line": None,
        })

    def _on_requestfailed(self, request) -> None:
        failure = ""
        try:
            failure = request.failure or ""
        except Exception:
            failure = ""
        if isinstance(failure, dict):
            failure = failure.get("errorText") or ""
        failure = str(failure)
        if "ERR_ABORTED" in failure:
            return
        url = ""
        method = "GET"
        try:
            url = request.url
            method = request.method
        except Exception:
            pass
        self._remember({
            "ts": time.time(),
            "kind": "requestfailed",
            "text": f"{method} {url} {failure}".strip()[:500],
            "url": url or None,
            "line": None,
        })

    async def _on_response(self, response) -> None:
        try:
            status = response.status
            url = response.url
        except Exception:
            return
        if status < 400:
            return
        self._remember({
            "ts": time.time(),
            "kind": "response4xx5xx",
            "text": f"{status} {url}"[:500],
            "url": url,
            "line": None,
        })

    async def _on_dialog(self, dialog) -> None:
        message = ""
        try:
            message = dialog.message
        except Exception:
            pass
        self._remember({
            "ts": time.time(),
            "kind": "warn",
            "text": f"Dialog dismissed: {message}"[:500],
            "url": self._display_url or None,
            "line": None,
        })
        try:
            await dialog.dismiss()
        except Exception:
            pass

    def _on_filechooser(self, chooser) -> None:
        self._remember({
            "ts": time.time(),
            "kind": "warn",
            "text": "File chooser dismissed",
            "url": self._display_url or None,
            "line": None,
        })

    async def _close_extra(self, page) -> None:
        await self._adopt_popup(page)

    async def read_for_model(self, tab: int | None = None, max_chars: int = 6000) -> dict:
        """Visible text, recent console errors, and a CSS-scale JPEG of one tab."""
        await self.ensure_started()
        if not self._tabs:
            raise BrowserError("Browser is not running.")
        try:
            index = self._active if tab is None else int(tab)
        except (TypeError, ValueError):
            index = self._active
        if index < 0 or index >= len(self._tabs):
            index = self._active
        chosen = self._tabs[index]
        lines = ["Tabs:"]
        for i, item in enumerate(self._tabs):
            mark = "*" if i == self._active else ""
            title = item.title or "Untitled"
            url = item.display_url or item.url or "about:blank"
            lines.append(f"[{i}]{mark} {title} — {url}")
        visible = ""
        if chosen.page is not None:
            try:
                raw = await chosen.page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
                visible = " ".join(str(raw or "").split())
            except Exception as exc:
                visible = f"(could not read the page: {exc})"
        if len(visible) > max_chars:
            visible = visible[:max_chars] + "…"
        errors = [
            entry for entry in self._console
            if entry.get("kind") in {"error", "pageerror", "requestfailed", "response4xx5xx"}
        ][-10:]
        err_lines = [f"[{entry.get('kind')}] {entry.get('text')}" for entry in errors] or ["(none)"]
        text = (
            "\n".join(lines)
            + f"\n\nVisible text (tab {index}):\n{visible}\n\nConsole errors:\n"
            + "\n".join(err_lines)
        )
        image = None
        if chosen.page is not None:
            try:
                jpeg = await chosen.page.screenshot(type="jpeg", quality=70, scale="css")
                image = base64.b64encode(jpeg).decode("ascii")
            except Exception:
                image = None
        _dlog("browser_tool_read", tab=index, text_len=len(text), has_image=bool(image))
        return {
            "text": text,
            "image_jpeg_b64": image,
            "tab": index,
            "url": chosen.display_url or chosen.url or "",
        }


def model_browser_summary() -> str | None:
    """One line for the prompt when the local browser is already running."""
    if not browser_enabled() or session._browser is None or not session._tabs:
        return None
    parts = []
    for index, tab in enumerate(session._tabs):
        mark = "*" if index == session._active else ""
        title = tab.title or "Untitled"
        url = tab.display_url or tab.url or "about:blank"
        parts.append(f"[{index}]{mark} {title} — {url}")
    return "Browser is open: " + "; ".join(parts)


session = BrowserSession()
