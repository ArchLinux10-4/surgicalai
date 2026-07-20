"""Element Picker API — local-install only, zero Railway footprint.

Every endpoint checks `USE_POSTGRES` (the same signal `settings.is_hosted`
already exposes to the frontend) and returns 404 on a hosted deploy, so this
never activates on Railway/Vercel even though the router is registered
unconditionally at import time. Importing this module never imports
playwright — that stays deferred inside services/element_picker.py so a
host without the local-only dependency installed still boots cleanly.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import USE_POSTGRES
from services import element_picker as picker_service
from services.element_picker import ElementPickerError

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


class PickRequest(BaseModel):
    x: float
    y: float


@router.get("/status")
def get_status():
    if USE_POSTGRES:
        return {"available": False, "connected": False}
    return {"available": True, **picker_service.status()}


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


@router.post("/disconnect")
def disconnect():
    _guard_local_only()
    try:
        return picker_service.disconnect()
    except ElementPickerError as e:
        raise HTTPException(status_code=400, detail=str(e))
