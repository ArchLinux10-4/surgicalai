"""Image Studio router — GPT-powered image generation and editing.

Uses the OpenAI Responses API with the built-in `image_generation` tool
(official docs: /api/docs/guides/tools-image-generation). GPT-5.5 interprets
the prompt, optionally uses an uploaded image as edit context, and returns
the result as base64.

Design rules:
- Fully self-contained: only main.py registration touches existing code.
- Direct HTTPS call via httpx (no SDK-version dependency on `responses`).
- Every known real-world failure mode has an explicit, user-readable handler:
    * 403 "organization must be verified"  -> actionable message + fix link
    * moderation blocked                   -> clear content-policy message
    * slow generation / timeout            -> 180s budget + clean timeout error
    * text-only response (no image)        -> surface the model's text
- Never raises to the client unhandled: all errors return structured JSON.
"""
import base64
import logging
import time

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from database import get_setting, get_user_api_key
from crypto_utils import decrypt_api_key

logger = logging.getLogger("image_studio")

router = APIRouter()

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
IMAGE_MODEL = "gpt-5.5"          # must match the id exposed by /api/settings/models
REQUEST_TIMEOUT_S = 180          # image generation routinely takes 30-120s
MAX_INPUT_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB decoded — sanity cap

ALLOWED_INPUT_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


class ImageRequest(BaseModel):
    prompt: str
    image_base64: str | None = None   # bare base64, no data: prefix
    image_mime: str | None = None     # e.g. "image/jpeg"


def _dlog(msg: str) -> None:
    """Debug log — mirrors the pipeline convention of loud, greppable lines."""
    logger.info("[image_studio] %s", msg)


def _resolve_openai_key(user_id: str) -> str:
    """Per-user key first (encrypted), then global setting. Same precedence
    as settings.py:_resolve_api_key. Any failure degrades to global key."""
    try:
        encrypted = get_user_api_key(user_id, "openai")
        if encrypted:
            key = decrypt_api_key(encrypted)
            if key:
                _dlog(f"key_resolution=user user_id={user_id}")
                return key
    except Exception as e:
        _dlog(f"key_resolution user-key failed, falling back to global: {e}")
    key = get_setting("openai_api_key", "")
    _dlog(f"key_resolution=global present={bool(key)}")
    return key


def _error(code: str, detail: str, status: int = 200) -> dict:
    """Structured error payload. Always ok=False; frontend renders `detail`."""
    _dlog(f"error code={code} detail={detail[:200]}")
    return {"ok": False, "error_code": code, "detail": detail}


def _validate(body: ImageRequest) -> dict | None:
    """Returns an error dict if the request is invalid, else None."""
    if not body.prompt or not body.prompt.strip():
        return _error("empty_prompt", "Prompt is required.")
    if body.image_base64:
        mime = (body.image_mime or "").lower()
        if mime not in ALLOWED_INPUT_MIMES:
            return _error(
                "bad_mime",
                f"Unsupported image type '{body.image_mime}'. "
                "Use PNG, JPEG, WebP, or GIF.",
            )
        try:
            raw = base64.b64decode(body.image_base64, validate=True)
        except Exception:
            return _error("bad_base64", "Uploaded image is not valid base64.")
        if len(raw) > MAX_INPUT_IMAGE_BYTES:
            return _error(
                "image_too_large",
                f"Image is {len(raw) // (1024 * 1024)} MB — max is 20 MB.",
            )
        _dlog(f"input_image mime={mime} decoded_bytes={len(raw)}")
    return None


def _build_payload(body: ImageRequest) -> dict:
    """Builds the official Responses API payload.

    Docs contract:
      - tools: [{"type": "image_generation"}]
      - input image: {"type": "input_image", "image_url": "data:<mime>;base64,<b64>"}
      - action left at default "auto": docs state "edit" errors when no image
        is in context, so "auto" is the safe, documented default for both modes.
    """
    content: list[dict] = [{"type": "input_text", "text": body.prompt.strip()}]
    if body.image_base64:
        data_url = f"data:{body.image_mime};base64,{body.image_base64}"
        content.append({"type": "input_image", "image_url": data_url})

    payload = {
        "model": IMAGE_MODEL,
        "input": [{"role": "user", "content": content}],
        "tools": [{"type": "image_generation"}],
    }
    _dlog(
        f"payload model={IMAGE_MODEL} has_input_image={bool(body.image_base64)} "
        f"prompt_len={len(body.prompt)}"
    )
    return payload


def _extract_result(data: dict) -> dict:
    """Extracts image and/or text from a Responses API result.

    Docs contract: response.output is a list; image results are items with
    type == "image_generation_call" and the base64 image in `.result`.
    The model may legally answer text-only (refusal / clarifying question),
    so text is always collected as a fallback.
    """
    output = data.get("output") or []
    _dlog(f"extract output_items={len(output)} types={[o.get('type') for o in output]}")

    image_b64 = None
    output_format = "png"
    for item in output:
        if item.get("type") == "image_generation_call":
            status = item.get("status")
            _dlog(f"image_generation_call status={status}")
            if item.get("result"):
                image_b64 = item["result"]
                output_format = item.get("output_format") or "png"
                break

    text_parts = []
    for item in output:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text" and part.get("text"):
                    text_parts.append(part["text"])
    text = "\n".join(text_parts).strip()
    _dlog(f"extract image_found={bool(image_b64)} text_len={len(text)}")

    if image_b64:
        return {
            "ok": True,
            "image_base64": image_b64,
            "image_mime": f"image/{output_format}",
            "text": text,
        }
    if text:
        # Model chose to answer in text (refusal or question) — surface it.
        return _error("no_image_text_response", text)
    return _error(
        "no_image",
        "The model returned neither an image nor an explanation. Try rephrasing.",
    )


def _map_api_error(status_code: int, body_text: str) -> dict:
    """Maps documented OpenAI error responses to actionable messages."""
    lower = body_text.lower()
    if status_code == 403 and "verified" in lower:
        return _error(
            "org_not_verified",
            "Your OpenAI organization must be verified to use image generation. "
            "Visit https://platform.openai.com/settings/organization/general and "
            "click Verify Organization. Note: access can take ~15 minutes to "
            "propagate after verifying; generating a fresh API key can help.",
        )
    if "moderation" in lower or "content_policy" in lower or "safety" in lower:
        return _error(
            "moderation_blocked",
            "The request was blocked by OpenAI's content policy. "
            "Adjust the prompt or image and try again.",
        )
    if status_code == 401:
        return _error("bad_key", "OpenAI rejected the API key. Check it in Settings.")
    if status_code == 429:
        return _error("rate_limited", "OpenAI rate limit or quota hit. Try again shortly.")
    return _error(
        "openai_error",
        f"OpenAI returned HTTP {status_code}: {body_text[:300]}",
    )


@router.post("/generate")
def generate_image(body: ImageRequest, request: Request):
    """Generate a new image, or edit the uploaded one, from a text prompt."""
    user_id = getattr(request.state, "user_id", "")
    _dlog(f"request user_id={user_id} mode={'edit' if body.image_base64 else 'generate'}")

    invalid = _validate(body)
    if invalid:
        return invalid

    api_key = _resolve_openai_key(user_id)
    if not api_key:
        return _error(
            "no_api_key",
            "No OpenAI API key configured. Add one in Settings to use Image Studio.",
        )

    payload = _build_payload(body)
    started = time.time()
    try:
        resp = httpx.post(
            OPENAI_RESPONSES_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT_S,
        )
    except httpx.TimeoutException:
        return _error(
            "timeout",
            f"Image generation timed out after {REQUEST_TIMEOUT_S}s. "
            "This can happen under load — try again.",
        )
    except Exception as e:
        return _error("network_error", f"Could not reach OpenAI: {e}")

    elapsed = time.time() - started
    _dlog(f"openai_response status={resp.status_code} elapsed={elapsed:.1f}s")

    if resp.status_code != 200:
        return _map_api_error(resp.status_code, resp.text)

    try:
        data = resp.json()
    except Exception:
        return _error("bad_json", "OpenAI returned an unreadable response.")

    # Responses API can also report failure inside a 200 body.
    if data.get("status") == "failed":
        err = (data.get("error") or {}).get("message", "unknown error")
        _dlog(f"response_status=failed error={err[:200]}")
        return _map_api_error(200, err)

    return _extract_result(data)
