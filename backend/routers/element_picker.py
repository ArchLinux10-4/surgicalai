"""Element Picker API — local-install only, zero Railway footprint.

Every endpoint checks `USE_POSTGRES` (the same signal `settings.is_hosted`
already exposes to the frontend) and returns 404 on a hosted deploy, so this
never activates on Railway/Vercel even though the router is registered
unconditionally at import time. Importing this module never imports
playwright — that stays deferred inside services/element_picker.py so a
host without the local-only dependency installed still boots cleanly.
"""
import asyncio
import base64
import json
import logging
import struct
import threading
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from database import USE_POSTGRES
from services import element_picker as picker_service
from services.element_picker import ElementPickerError

logger = logging.getLogger(__name__)

router = APIRouter()


def _guard_local_only():
    if USE_POSTGRES:
        # Hosted deploy — feature does not exist here. 404, not 403, so it
        # reads as "this endpoint doesn't exist" rather than "you're not
        # allowed", matching how the rest of the app hides hosted-unsafe
        # surfaces (see Import Folder / is_hosted precedent).
        raise HTTPException(status_code=404, detail="Not available on hosted deployments.")


class ConnectRequest(BaseModel):
    cdp_url: str = "http://localhost:9222"


class LaunchRequest(BaseModel):
    cdp_url: str = "http://localhost:9222"


class PickRequest(BaseModel):
    x: float
    y: float


class ReloadRequest(BaseModel):
    hard: bool = False


@router.get("/status")
def get_status():
    if USE_POSTGRES:
        return {"available": False, "connected": False}
    return {"available": True, **picker_service.status()}


@router.post("/launch")
def launch(body: LaunchRequest):
    """One-click Chrome start for non-technical users — launches a dedicated
    picker-profile Chrome window with the debug port open (or no-ops if a
    debug-mode Chrome is already listening on that port). Never touches the
    user's everyday Chrome window/profile. See services/element_picker.py
    for the full rationale."""
    _guard_local_only()
    try:
        return picker_service.launch(body.cdp_url)
    except ElementPickerError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/connect")
def connect(body: ConnectRequest):
    _guard_local_only()
    try:
        return picker_service.connect(body.cdp_url)
    except ElementPickerError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/screenshot")
def screenshot():
    _guard_local_only()
    try:
        b64 = picker_service.screenshot_b64()
    except ElementPickerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"image_base64": b64, "mime_type": "image/jpeg"}


@router.post("/pick")
def pick(body: PickRequest):
    _guard_local_only()
    try:
        return picker_service.pick(body.x, body.y)
    except ElementPickerError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/reload")
def reload(body: ReloadRequest):
    """Reload the live page — `hard=true` bypasses the browser cache (the
    CDP equivalent of Ctrl/Cmd+Shift+R). Needed for frontend dev loops:
    without this the user has no way to force the picker's live view to
    pick up freshly-built JS/CSS short of disconnecting and reconnecting."""
    _guard_local_only()
    try:
        return picker_service.reload(body.hard)
    except ElementPickerError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/disconnect")
def disconnect():
    _guard_local_only()
    try:
        return picker_service.disconnect()
    except ElementPickerError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── Live view — WebSocket screencast ───────────────────────────────────────
#
# Why WebSocket, not another poll-and-refresh REST call: `Page.startScreencast`
# is a push stream (Chrome sends a new JPEG the instant the page repaints,
# throttled by our frame-ack), so a persistent socket is the natural fit — the
# same reasoning already applied to the chat streaming endpoints elsewhere in
# this router package (see routers/chat.py's WS transport docstring).
#
# Auth: the HTTP `auth_middleware` in main.py does not run for WebSocket
# scopes (Starlette limitation), so we re-authenticate here exactly the same
# way routers/chat.py's `_ws_pump` does: `?token=` query param → decode_token.
# This mirrors an existing, already-reviewed pattern rather than inventing a
# new one.
async def _authenticate_ws(websocket: WebSocket) -> bool:
    from auth_utils import decode_token

    token = websocket.query_params.get("token", "") or ""
    try:
        decode_token(token)
        return True
    except Exception:
        return False


@router.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket):
    """Live view of the connected page. On open: authenticates, starts the
    CDP screencast, then runs two concurrent loops for the life of the
    socket — one forwarding frames to the client, one receiving control
    messages (navigate / mouse / key / scroll) from the client and relaying
    them into the real page. Disconnect always stops the screencast but
    never closes the underlying CDP connection (browser stays attached)."""
    if USE_POSTGRES:
        # Hosted deploy — reject the handshake before accept(), same 404-style
        # "doesn't exist here" contract as the HTTP endpoints above.
        await websocket.close(code=1008)
        return

    if not await _authenticate_ws(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    if not picker_service.status().get("connected"):
        await websocket.send_text(json.dumps({"type": "error", "message": "Not connected to a browser."}))
        await websocket.close(code=1003)
        return

    # Client tells us its actual panel size + devicePixelRatio so the
    # screencast is captured at the real pixel resolution it'll be shown
    # at — a fixed 1400x900 request stretched across a bigger, high-DPI
    # panel is what produced the "blurry" complaint. Values are re-clamped
    # server-side in start_screencast regardless of what's sent here.
    try:
        req_w = int(float(websocket.query_params.get("w", "1400")))
        req_h = int(float(websocket.query_params.get("h", "900")))
        req_dpr = float(websocket.query_params.get("dpr", "1"))
    except (TypeError, ValueError):
        req_w, req_h, req_dpr = 1400, 900, 1.0

    try:
        await asyncio.to_thread(
            picker_service.start_screencast,
            max_width=round(req_w * req_dpr),
            max_height=round(req_h * req_dpr),
            quality=85,
        )
    except ElementPickerError as e:
        await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        await websocket.close(code=1011)
        return

    async def _frame_sender():
        # Binary framing instead of JSON+base64 text: CDP already hands us
        # base64 JPEG bytes, so round-tripping that through JSON.stringify
        # (another text-escaping pass) then having the browser JSON.parse +
        # decode base64 again was two redundant encode/decode passes on
        # every single frame — a real, measurable chunk of the reported
        # slowness. Wire format: [4-byte big-endian metadata length][UTF-8
        # JSON metadata][raw JPEG bytes]. Client reads it with a DataView
        # and decodes the JPEG via createImageBitmap — no base64 involved
        # at all after this point.
        #
        # Frame pump: one dedicated background thread blocks on
        # get_next_frame() and hands frames to this coroutine via an
        # asyncio.Queue, instead of the previous `await
        # asyncio.to_thread(get_next_frame, ...)` called fresh for every
        # single frame. The old approach paid threadpool task-scheduling
        # overhead once per frame (i.e. at full frame rate) for no benefit —
        # get_next_frame is a plain blocking queue.get, so one long-lived
        # thread is strictly cheaper than one short-lived executor hop per
        # frame.
        loop = asyncio.get_event_loop()
        out_queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=2)
        stop_event = threading.Event()

        def _pump():
            while not stop_event.is_set():
                frame = picker_service.get_next_frame(1.0)
                if frame is None:
                    continue  # timeout — just poll again, keeps the loop cancellable
                def _enqueue(f=frame):
                    if out_queue.full():
                        try:
                            out_queue.get_nowait()  # drop oldest, never let stale frames pile up
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        out_queue.put_nowait(f)
                    except asyncio.QueueFull:
                        pass
                try:
                    loop.call_soon_threadsafe(_enqueue)
                except RuntimeError:
                    break  # loop already closed (socket tearing down)

        pump_thread = threading.Thread(target=_pump, daemon=True, name="element-picker-frame-pump")
        pump_thread.start()

        # Real timing data instead of guessing where "laggy" comes from:
        # logs frame rate + average capture-to-wire latency every 5s while
        # the panel is open, so the next reproduction gives concrete numbers.
        frame_count = 0
        latency_total = 0.0
        window_start = time.monotonic()

        try:
            while True:
                frame = await out_queue.get()
                try:
                    jpeg_bytes = base64.b64decode(frame.get("data") or "")
                except Exception:
                    continue
                captured_at = frame.get("_captured_at")
                if captured_at:
                    latency_total += max(0.0, time.time() - captured_at)
                    frame_count += 1
                meta_bytes = json.dumps(frame.get("metadata") or {}).encode("utf-8")
                header = struct.pack(">I", len(meta_bytes))
                await websocket.send_bytes(header + meta_bytes + jpeg_bytes)

                now = time.monotonic()
                elapsed = now - window_start
                if elapsed >= 5.0:
                    if frame_count:
                        picker_service._dlog(
                            "screencast_perf",
                            fps=round(frame_count / elapsed, 1),
                            avg_capture_to_wire_ms=round((latency_total / frame_count) * 1000, 1),
                            jpeg_bytes_last_frame=len(jpeg_bytes),
                        )
                    frame_count = 0
                    latency_total = 0.0
                    window_start = now
        finally:
            stop_event.set()
            pump_thread.join(timeout=2)

    # Continuous-event coalescing (mouseWheel / mouseMoved only) — the exact
    # same "drop stale, keep latest" idea as the frame pipeline's
    # drop-if-busy, just applied to the input direction instead of the
    # output direction. Without this, a trackpad scroll gesture (which fires
    # dozens of wheel events/sec) queues one `await asyncio.to_thread(...)`
    # CDP round-trip per event, strictly in order — the receive loop can't
    # even read the *next* queued message until the current one's Chrome
    # round-trip finishes. A 2s scroll gesture then takes several real
    # seconds to fully drain, so the page keeps visibly catching up to
    # stale, already-superseded deltas long after the hand stops — this is
    # the proven cause of the reported "1-2 seconds behind my mouse" lag.
    # Fix: while a wheel/move dispatch is in flight, newer events of the
    # same kind merge into a single pending slot (wheel deltas accumulate,
    # x/y take the latest) instead of queuing individually. Discrete events
    # (click/key/text/navigate) are untouched — already low-frequency and
    # must stay strictly ordered.
    _pending_continuous: dict[str, dict] = {}
    _dispatching_continuous: set[str] = set()

    async def _flush_continuous(kind: str):
        try:
            while True:
                ev = _pending_continuous.pop(kind, None)
                if ev is None:
                    return
                try:
                    await asyncio.to_thread(
                        picker_service.dispatch_mouse, kind,
                        ev["x"], ev["y"], ev.get("button", "left"),
                        ev.get("deltaX", 0.0), ev.get("deltaY", 0.0),
                    )
                except ElementPickerError as e:
                    logger.info("[element_picker] ws control error: %s", e)
                except Exception as e:
                    logger.info("[element_picker] ws control unexpected error: %s", e)
        finally:
            _dispatching_continuous.discard(kind)

    def _queue_continuous(kind: str, msg: dict):
        x = float(msg.get("x", 0))
        y = float(msg.get("y", 0))
        existing = _pending_continuous.get(kind)
        if kind == "mouseWheel" and existing is not None:
            # Accumulate deltas so a burst of small scroll ticks still
            # produces the correct total scroll distance, not just the
            # latest tiny tick.
            existing["x"] = x
            existing["y"] = y
            existing["deltaX"] = existing.get("deltaX", 0.0) + float(msg.get("deltaX", 0))
            existing["deltaY"] = existing.get("deltaY", 0.0) + float(msg.get("deltaY", 0))
        else:
            _pending_continuous[kind] = {
                "x": x, "y": y,
                "button": msg.get("button", "left"),
                "deltaX": float(msg.get("deltaX", 0)),
                "deltaY": float(msg.get("deltaY", 0)),
            }
        if kind not in _dispatching_continuous:
            _dispatching_continuous.add(kind)
            asyncio.create_task(_flush_continuous(kind))

    async def _control_receiver():
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            try:
                if kind == "navigate":
                    await asyncio.to_thread(picker_service.navigate, msg.get("url", ""))
                elif kind in ("mouseMoved", "mouseWheel"):
                    _queue_continuous(kind, msg)
                elif kind in ("mousePressed", "mouseReleased"):
                    await asyncio.to_thread(
                        picker_service.dispatch_mouse, kind,
                        float(msg.get("x", 0)), float(msg.get("y", 0)),
                        msg.get("button", "left"),
                        float(msg.get("deltaX", 0)), float(msg.get("deltaY", 0)),
                    )
                elif kind == "text":
                    await asyncio.to_thread(picker_service.dispatch_text, msg.get("text", ""))
                elif kind == "key":
                    await asyncio.to_thread(picker_service.dispatch_key, msg.get("key", ""), msg.get("code", ""))
            except ElementPickerError as e:
                logger.info("[element_picker] ws control error: %s", e)
            except Exception as e:
                logger.info("[element_picker] ws control unexpected error: %s", e)

    sender_task = asyncio.create_task(_frame_sender())
    receiver_task = asyncio.create_task(_control_receiver())
    try:
        await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in (sender_task, receiver_task):
            t.cancel()
        for t in (sender_task, receiver_task):
            try:
                await t
            except BaseException:
                pass
        try:
            await asyncio.to_thread(picker_service.stop_screencast)
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
