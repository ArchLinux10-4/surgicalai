"""In-app browser API. Local install only.

Every route 404s when USE_POSTGRES is set (Railway / Vercel) or when
SURGICALAI_BROWSER_DISABLED is set. Importing this module does not import
Playwright.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import time

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

from services.inapp_browser import BrowserError, browser_enabled, coalesce_continuous, session
from services.inapp_browser import _dlog as browser_log
from services.inapp_browser_locate import locate_in_files
from services.session_auth import require_session_access_from_request

router = APIRouter()


def _guard_local_only() -> None:
    if not browser_enabled():
        raise HTTPException(status_code=404, detail="Not available on hosted deployments.")


class UrlBody(BaseModel):
    url: str


class PickBody(BaseModel):
    on: bool = False


class StyleBody(BaseModel):
    token: str
    css: dict = {}


class LocateBody(BaseModel):
    session_id: str
    node: dict = {}


class TabBody(BaseModel):
    url: str | None = None


def _load_session_files(session_id: str) -> list[dict]:
    from database import get_db_ctx
    with get_db_ctx() as conn:
        rows = conn.execute(
            "SELECT id, filename, content, file_type FROM session_files WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    files = []
    for row in rows:
        data = dict(row) if hasattr(row, "keys") else {
            "id": row[0], "filename": row[1], "content": row[2], "file_type": row[3],
        }
        files.append({
            "id": data.get("id"),
            "filename": data.get("filename") or "",
            "content": data.get("content") or "",
            "file_type": data.get("file_type") or "",
        })
    return files


@router.get("/status")
async def browser_status():
    _guard_local_only()
    return session.status()


@router.post("/start")
async def browser_start():
    _guard_local_only()
    try:
        return await session.start()
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/stop")
async def browser_stop():
    _guard_local_only()
    return await session.stop()


@router.post("/navigate")
async def browser_navigate(body: UrlBody):
    _guard_local_only()
    try:
        return await session.navigate(body.url)
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/back")
async def browser_back():
    _guard_local_only()
    try:
        return await session.back()
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/forward")
async def browser_forward():
    _guard_local_only()
    try:
        return await session.forward()
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/reload")
async def browser_reload():
    _guard_local_only()
    try:
        return await session.reload()
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/tabs")
async def browser_new_tab(body: TabBody | None = None):
    _guard_local_only()
    try:
        return await session.new_tab(body.url if body and body.url else None)
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/tabs/{index}/activate")
async def browser_activate_tab(index: int):
    _guard_local_only()
    try:
        return await session.switch_tab(index)
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/tabs/{index}")
async def browser_close_tab(index: int):
    _guard_local_only()
    try:
        return await session.close_tab(index)
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/stop-loading")
async def browser_stop_loading():
    _guard_local_only()
    try:
        return await session.stop_loading()
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/pick-mode")
async def browser_pick_mode(body: PickBody):
    _guard_local_only()
    try:
        return await session.pick_mode(body.on)
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/console")
async def browser_console(since: float = 0):
    _guard_local_only()
    return {"entries": session.console_entries(since)}


@router.post("/style")
async def browser_style(body: StyleBody):
    _guard_local_only()
    try:
        return await session.eval_style(body.token, body.css)
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/style/{token}")
async def browser_clear_style(token: str):
    _guard_local_only()
    try:
        return await session.clear_styles(token)
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/locate")
async def browser_locate(body: LocateBody, request: Request):
    _guard_local_only()
    require_session_access_from_request(body.session_id, request)
    hits = locate_in_files(_load_session_files(body.session_id), body.node or {})
    browser_log("browser_locate", top_score=(hits[0]["score"] if hits else 0), candidates=len(hits))
    return {"candidates": hits}


@router.get("/screenshot")
async def browser_screenshot():
    _guard_local_only()
    try:
        png = await session.screenshot_page()
    except BrowserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=png, media_type="image/png")


def _query_viewport(websocket: WebSocket) -> tuple[int, int, float]:
    try:
        width = int(float(websocket.query_params.get("w", "1280")))
        height = int(float(websocket.query_params.get("h", "800")))
        dpr = float(websocket.query_params.get("dpr", "1"))
    except (TypeError, ValueError):
        return 1280, 800, 1.0
    return width, height, dpr


async def _send_frame(websocket: WebSocket, frame: dict) -> int:
    raw_b64 = frame.get("data") or ""
    if not raw_b64:
        return 0
    try:
        jpeg = base64.b64decode(raw_b64)
    except Exception:
        return 0
    meta = dict(frame.get("metadata") or {})
    meta["captured_at"] = frame.get("captured_at")
    meta_bytes = json.dumps(meta).encode("utf-8")
    await websocket.send_bytes(struct.pack(">I", len(meta_bytes)) + meta_bytes + jpeg)
    return len(jpeg)


async def _sender(websocket: WebSocket, sub_q: asyncio.Queue) -> None:
    frame_count = 0
    latency_total = 0.0
    window_start = time.monotonic()
    last_jpeg = 0
    frame_task = asyncio.create_task(session.next_frame())
    event_task = asyncio.create_task(sub_q.get())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {frame_task, event_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if frame_task in done:
                frame = frame_task.result()
                frame_task = asyncio.create_task(session.next_frame())
                last_jpeg = await _send_frame(websocket, frame)
                captured = frame.get("captured_at")
                if captured:
                    latency_total += max(0.0, time.time() - captured)
                    frame_count += 1
                elapsed = time.monotonic() - window_start
                if elapsed >= 5.0:
                    if frame_count:
                        browser_log(
                            "browser_screencast_perf",
                            fps=round(frame_count / elapsed, 1),
                            avg_capture_to_wire_ms=round((latency_total / frame_count) * 1000, 1),
                            last_jpeg_bytes=last_jpeg,
                        )
                    frame_count = 0
                    latency_total = 0.0
                    window_start = time.monotonic()
            if event_task in done:
                event = event_task.result()
                event_task = asyncio.create_task(sub_q.get())
                await websocket.send_text(json.dumps(event))
    finally:
        frame_task.cancel()
        event_task.cancel()


async def _receiver(websocket: WebSocket) -> None:
    pending: dict[str, dict] = {}
    dispatching: set[str] = set()
    resize_holder: dict[str, asyncio.Task | None] = {"task": None}

    async def flush(kind: str) -> None:
        try:
            while True:
                ev = pending.pop(kind, None)
                if ev is None:
                    return
                try:
                    await session.mouse(
                        kind, ev["x"], ev["y"], ev.get("button") or "left",
                        ev.get("buttons") or 0, ev.get("clickCount") or 0,
                        ev.get("modifiers") or 0, ev.get("dx") or 0, ev.get("dy") or 0,
                    )
                except BrowserError:
                    return
        finally:
            dispatching.discard(kind)

    def queue_continuous(kind: str, msg: dict) -> None:
        coalesce_continuous(pending, kind, msg)
        if kind not in dispatching:
            dispatching.add(kind)
            asyncio.create_task(flush(kind))

    def schedule_resize(w, h, dpr) -> None:
        current = resize_holder["task"]
        if current and not current.done():
            current.cancel()

        async def _apply() -> None:
            try:
                await asyncio.sleep(0.15)
                await session.set_viewport(w, h, dpr)
            except asyncio.CancelledError:
                return
            except BrowserError:
                return

        resize_holder["task"] = asyncio.create_task(_apply())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("t")
            try:
                if kind == "mouse":
                    mouse_kind = msg.get("kind")
                    if mouse_kind in ("mouseMoved", "mouseWheel"):
                        queue_continuous(mouse_kind, msg)
                    else:
                        await session.mouse(
                            mouse_kind, msg.get("x") or 0, msg.get("y") or 0,
                            msg.get("button") or "left", msg.get("buttons") or 0,
                            msg.get("clickCount") or 0, msg.get("modifiers") or 0,
                            msg.get("dx") or 0, msg.get("dy") or 0,
                        )
                elif kind == "key":
                    await session.key(
                        msg.get("kind"), msg.get("key"), msg.get("code"),
                        msg.get("text"), msg.get("modifiers") or 0, msg.get("keyCode") or 0,
                    )
                elif kind == "text":
                    await session.insert_text(msg.get("text") or "")
                elif kind == "resize":
                    schedule_resize(msg.get("w"), msg.get("h"), msg.get("dpr"))
                elif kind == "nav":
                    await session.navigate(msg.get("url") or "")
                elif kind == "pick":
                    await session.pick_mode(bool(msg.get("on")))
            except BrowserError as exc:
                await websocket.send_text(json.dumps({"type": "nav_error", "message": str(exc)}))
    finally:
        current = resize_holder["task"]
        if current and not current.done():
            current.cancel()


async def _reject(websocket: WebSocket) -> None:
    try:
        await websocket.close(code=1008)
    except Exception:
        pass


@router.websocket("/ws")
async def browser_ws(websocket: WebSocket):
    if not browser_enabled():
        await _reject(websocket)
        return
    from auth_utils import decode_token
    token = websocket.query_params.get("token", "") or ""
    try:
        decode_token(token)
    except Exception:
        await _reject(websocket)
        return
    await websocket.accept()
    width, height, dpr = _query_viewport(websocket)
    try:
        await session.ensure_started()
        await session.set_viewport(width, height, dpr)
        await session.start_screencast()
    except BrowserError as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close(code=1011)
        return
    session.note_client(1)
    sub_q = session.subscribe()
    try:
        await websocket.send_text(json.dumps({"type": "hello", "status": session.status()}))
        sender = asyncio.create_task(_sender(websocket, sub_q))
        receiver = asyncio.create_task(_receiver(websocket))
        done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    finally:
        session.unsubscribe(sub_q)
        session.note_client(-1)
        if session.clients == 0:
            await session.stop_screencast()
