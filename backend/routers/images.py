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
    * expired/unknown previous_response_id -> "session expired" message
- `tool_choice` forces the image_generation tool: the model must always
  produce an image instead of optionally answering in text (docs: "To force
  the image generation tool call, set tool_choice to {type: image_generation}").
  Text can now only appear via refusals — the frontend labels it as such.
- Multi-turn editing: the client may send `previous_response_id` (docs:
  Multi-turn editing) so follow-up prompts edit the previous result. Requires
  OpenAI response storage, which is on by default (`store` is never disabled).
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
IMAGE_MODEL = "gpt-5.5"          # default — battle-tested, never changed
ALLOWED_IMAGE_MODELS = {
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
}
REQUEST_TIMEOUT_S = 180          # image generation routinely takes 30-120s
MAX_INPUT_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB decoded — sanity cap

ALLOWED_INPUT_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


class ImageRequest(BaseModel):
    prompt: str
    model: str | None = None          # user-selected model; None → IMAGE_MODEL default
    image_base64: str | None = None   # bare base64, no data: prefix
    image_mime: str | None = None     # e.g. "image/jpeg"
    quality: str | None = None        # docs: low | medium | high | auto
    previous_response_id: str | None = None  # multi-turn editing (docs: resp_…)


# Official values per docs (tools-image-generation). Anything else is ignored.
ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}


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
    if body.previous_response_id is not None:
        rid = body.previous_response_id.strip()
        if not rid or not rid.startswith("resp_") or len(rid) > 200:
            # A malformed id would silently produce an unrelated image —
            # fail loud instead so the user knows the session is broken.
            _dlog(f"previous_response_id invalid value={body.previous_response_id!r}")
            return _error(
                "bad_previous_response_id",
                "This edit session is no longer valid. "
                "Start a new session (↺ New session) and try again.",
            )
        _dlog(f"previous_response_id ok={rid}")
    return None


def _build_payload(body: ImageRequest) -> dict:
    """Builds the official Responses API payload.

    Docs contract:
      - tools: [{"type": "image_generation"}]
      - input image: {"type": "input_image", "image_url": "data:<mime>;base64,<b64>"}
      - action left at default "auto": docs state "edit" errors when no image
        is in context, so "auto" is the safe, documented default for both modes.
    """
    # Resolve model: user pick if whitelisted, else safe default
    resolved_model = body.model if body.model in ALLOWED_IMAGE_MODELS else IMAGE_MODEL
    _dlog(f"model_resolution requested={body.model!r} resolved={resolved_model} rejected={body.model not in ALLOWED_IMAGE_MODELS and body.model is not None}")

    content: list[dict] = [{"type": "input_text", "text": body.prompt.strip()}]
    if body.image_base64:
        data_url = f"data:{body.image_mime};base64,{body.image_base64}"
        content.append({"type": "input_image", "image_url": data_url})

    # Quality is an official tool option (docs: size/quality/format on the tool
    # object). Whitelist-validated; anything unexpected is ignored so a bad
    # value can never break generation. Omitting it == today's default (auto).
    tool: dict = {"type": "image_generation"}
    if body.quality:
        if body.quality in ALLOWED_QUALITIES and body.quality != "auto":
            tool["quality"] = body.quality
            _dlog(f"quality applied={body.quality}")
        else:
            _dlog(f"quality ignored value={body.quality!r}")

    payload = {
        "model": resolved_model,
        "input": [{"role": "user", "content": content}],
        "tools": [tool],
        # Docs: "To force the image generation tool call, you can set the
        # parameter tool_choice to {"type": "image_generation"}". Without this
        # the model may legally answer in text (design chat) instead of
        # generating — observed in production. Text now only appears on refusal.
        "tool_choice": {"type": "image_generation"},
    }

    # Multi-turn editing (docs: "Multi-turn editing" / previous_response_id):
    # chain this turn onto the prior response so the model edits its own
    # last output with full conversation context. Validated in _validate().
    if body.previous_response_id:
        payload["previous_response_id"] = body.previous_response_id.strip()

    _dlog(
        f"payload model={resolved_model} has_input_image={bool(body.image_base64)} "
        f"prompt_len={len(body.prompt)} quality={tool.get('quality', 'auto')} "
        f"chained={bool(body.previous_response_id)} tool_choice=image_generation"
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

    # The Responses API response id (resp_…) — returned to the client so the
    # next prompt can chain onto this turn via previous_response_id.
    response_id = data.get("id") or ""
    _dlog(
        f"extract image_found={bool(image_b64)} text_len={len(text)} "
        f"response_id={response_id or 'MISSING'}"
    )

    if image_b64:
        return {
            "ok": True,
            "image_base64": image_b64,
            "image_mime": f"image/{output_format}",
            "text": text,
            "response_id": response_id,
        }
    if text:
        # tool_choice forces the image tool, so reaching here means the model
        # refused (e.g. content policy). Surface its explanation — the
        # frontend renders it under a "no image was generated" warning label.
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
    if "previous response" in lower and ("not found" in lower or "not_found" in lower):
        # Chained edit references a response OpenAI no longer has (expired,
        # deleted, or a zero-data-retention org). Session cannot continue.
        return _error(
            "session_expired",
            "This edit session has expired on OpenAI's side. "
            "Start a new session (↺ New session) and re-upload your image.",
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
    _dlog(f"request user_id={user_id} mode={'edit' if body.image_base64 else 'generate'} requested_model={body.model!r}")

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

    result = _extract_result(data)

    # Audit trail: one greppable line per completed call — who spent what.
    # Wrapped so a logging hiccup can never break a successful generation.
    try:
        _dlog(
            f"audit user_id={user_id} model={payload['model']} ok={result.get('ok')} "
            f"mode={'edit' if body.image_base64 else 'generate'} "
            f"chained={bool(body.previous_response_id)} "
            f"quality={body.quality or 'auto'} elapsed={elapsed:.1f}s "
            f"image_b64_len={len(result.get('image_base64') or '')} "
            f"prompt={body.prompt.strip()[:120]!r}"
        )
    except Exception:
        pass

    return result
