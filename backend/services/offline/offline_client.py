"""
Offline model client — talks to Ollama directly (Qwen2.5-Coder:7b by default).

Every mitigation here is evidence-based (see OFFLINE_MODE.md for sources):
  1. num_ctx is ALWAYS set explicitly. Ollama's default (2048) silently
     truncates the start of the messages array (i.e. the system prompt) when
     context overflows, which looks like "the model ignores instructions" but
     is actually silent context starvation. (Aider-AI/aider#2371)
  2. num_predict is sized to the task (file length) since Ollama has no
     "stop on sentence boundary" — it hard-cuts mid-token at the limit.
     (ollama/ollama#4230, maintainer-confirmed expected behavior)
  3. keep_alive is set long (30m) so the model isn't unloaded between
     requests, avoiding a slow cold-start reload on every message.
  4. A hard client-side timeout + single retry-with-backoff, because Ollama
     itself has no request timeout and can hang indefinitely under memory
     pressure (StackOverflow #79040747: 67-minute hang; continuedev/continue#10124: 500s).
  5. Output is defensively stripped of leaked chat-template tokens
     (e.g. <|im_start|>, <|im_end|>) that quantized builds sometimes echo.
"""
from __future__ import annotations

import re
import time
from typing import AsyncIterator, Optional

import httpx

from database import get_setting

# Conservative defaults — override via Settings.
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_NUM_CTX = 16384          # explicit — never rely on Ollama's 2048 default
DEFAULT_KEEP_ALIVE = "30m"
CONNECT_TIMEOUT_S = 10
REQUEST_TIMEOUT_S = 180          # hard ceiling; Ollama has no server-side timeout
MAX_RETRIES = 1                  # one retry on hang/timeout, then surface an error

# Leaked chat-template / instruction-echo artifacts seen in quantized builds.
_TEMPLATE_LEAK_RE = re.compile(
    r"<\|im_start\|>.*?<\|im_end\|>|<\|im_start\|>|<\|im_end\|>|###<\|.*?\|>",
    re.DOTALL,
)


class OfflineModelError(Exception):
    """Raised when the local model fails or hangs after retries."""


def resolve_offline_model() -> str:
    """The model the picker chose, else the ollama_model setting."""
    picked = (get_setting("architect_model", "") or "").strip()
    if picked.startswith("ollama:"):
        name = picked[len("ollama:"):].strip()
        if name:
            return name
    return get_setting("ollama_model", DEFAULT_MODEL)


def get_offline_config() -> dict:
    return {
        "base_url": get_setting("ollama_base_url", DEFAULT_BASE_URL),
        "model": resolve_offline_model(),
        "num_ctx": int(get_setting("ollama_num_ctx", str(DEFAULT_NUM_CTX)) or DEFAULT_NUM_CTX),
    }


def _request_extras(model: str) -> dict:
    return {"think": False} if "qwen3" in (model or "").lower() else {}


_THINK_BLOCK_RE = re.compile(r"<think>.*?<\/think>\s*", re.DOTALL)
_THINK_OPEN_TAIL_RE = re.compile(r"<think>.*$", re.DOTALL)


_THINK_CLOSE_RE = re.compile(r"</think>\s*")


def _strip_think(text: str) -> str:
    text = _THINK_BLOCK_RE.sub("", text or "")
    text = _THINK_OPEN_TAIL_RE.sub("", text)
    return _THINK_CLOSE_RE.sub("", text)


class ThinkStripper:
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self._buf = ""
        self._inside = False

    def _partial(self, tag: str) -> int:
        for n in range(min(len(tag) - 1, len(self._buf)), 0, -1):
            if tag.startswith(self._buf[-n:]):
                return n
        return 0

    def feed(self, delta: str) -> str:
        self._buf += delta or ""
        out = []
        while True:
            if self._inside:
                i = self._buf.find(self.CLOSE)
                if i == -1:
                    keep = self._partial(self.CLOSE)
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                self._buf = self._buf[i + len(self.CLOSE):].lstrip("\n")
                self._inside = False
            else:
                i_open = self._buf.find(self.OPEN)
                i_close = self._buf.find(self.CLOSE)
                if i_close != -1 and (i_open == -1 or i_close < i_open):
                    out.append(self._buf[:i_close])
                    self._buf = self._buf[i_close + len(self.CLOSE):].lstrip("\n")
                    continue
                if i_open == -1:
                    keep = max(self._partial(self.OPEN), self._partial(self.CLOSE))
                    emit = self._buf[: len(self._buf) - keep] if keep else self._buf
                    out.append(emit)
                    self._buf = self._buf[len(emit):]
                    break
                out.append(self._buf[:i_open])
                self._buf = self._buf[i_open + len(self.OPEN):]
                self._inside = True
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self._inside else self._buf
        self._buf = ""
        return rest


def is_offline_mode_enabled() -> bool:
    return get_setting("ollama_enabled", "false") == "true"


def _strip_template_leaks(text: str) -> str:
    """Defensively remove leaked chat-template tokens from model output."""
    if not text:
        return text
    return _TEMPLATE_LEAK_RE.sub("", text).strip()


def _size_aware_num_predict(input_chars: int) -> int:
    """
    Size num_predict to the task so a whole-file rewrite isn't hard-truncated
    mid-file. Roughly 1 token ~= 4 chars; rewritten output is usually similar
    size to input plus some headroom for explanation text.
    """
    est_tokens = max(512, (input_chars // 3))
    return min(est_tokens + 1024, 8192)


async def ollama_chat_once(
    messages: list,
    *,
    input_chars_for_sizing: int = 2000,
    num_predict: Optional[int] = None,
    temperature: float = 0.2,
) -> dict:
    """
    Non-streaming call. Returns {"content": str, "truncated": bool, "error": None}
    or raises OfflineModelError after retries are exhausted.
    """
    cfg = get_offline_config()
    predict_budget = num_predict or _size_aware_num_predict(input_chars_for_sizing)

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "keep_alive": DEFAULT_KEEP_ALIVE,
        "options": {
            "temperature": temperature,
            "num_ctx": cfg["num_ctx"],
            "num_predict": predict_budget,
        },
    }
    payload.update(_request_extras(cfg["model"]))

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        t0 = time.time()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
            ) as client:
                resp = await client.post(f"{cfg['base_url']}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
                raw = (data.get("message") or {}).get("content", "")
                done_reason = data.get("done_reason", "stop")
                content = _strip_template_leaks(_strip_think(raw))
                return {
                    "content": content,
                    "truncated": done_reason == "length",
                    "duration_s": round(time.time() - t0, 1),
                    "error": None,
                }
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_err = e
            continue
        except httpx.HTTPStatusError as e:
            # 500s from Ollama are often "not enough memory to load the model"
            # (continuedev/continue#10124) — not worth retrying.
            raise OfflineModelError(
                f"Ollama returned {e.response.status_code}: {e.response.text[:300]}. "
                "This usually means there isn't enough memory to load the model, "
                "or the model isn't pulled yet (`ollama pull qwen2.5-coder:7b`)."
            ) from e

    raise OfflineModelError(
        f"Ollama did not respond within {REQUEST_TIMEOUT_S}s after {MAX_RETRIES + 1} attempt(s) "
        f"at {cfg['base_url']}. Local models can hang under memory pressure — "
        "check `ollama ps` and available RAM/VRAM."
    ) from last_err


async def ollama_chat_stream(
    messages: list,
    *,
    input_chars_for_sizing: int = 2000,
    num_predict: Optional[int] = None,
    temperature: float = 0.2,
) -> AsyncIterator[str]:
    """
    Streaming call. Yields raw content deltas (already stripped of template
    leaks token-by-token is not reliable, so leaks are stripped once at the
    end by the caller if it buffers the full text; callers doing true
    token-by-token UI streaming should treat individual deltas as best-effort).
    """
    cfg = get_offline_config()
    predict_budget = num_predict or _size_aware_num_predict(input_chars_for_sizing)

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "keep_alive": DEFAULT_KEEP_ALIVE,
        "options": {
            "temperature": temperature,
            "num_ctx": cfg["num_ctx"],
            "num_predict": predict_budget,
        },
    }
    payload.update(_request_extras(cfg["model"]))

    import json as _json

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        ) as client:
            async with client.stream("POST", f"{cfg['base_url']}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                strip = ThinkStripper()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = _json.loads(line)
                    except ValueError:
                        continue
                    delta = (chunk.get("message") or {}).get("content", "")
                    if delta:
                        cleaned = strip.feed(delta)
                        if cleaned:
                            yield cleaned
                    if chunk.get("done"):
                        tail = strip.flush()
                        if tail:
                            yield tail
                        return
                tail = strip.flush()
                if tail:
                    yield tail
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        raise OfflineModelError(
            f"Ollama did not respond within {REQUEST_TIMEOUT_S}s at {cfg['base_url']}. "
            "Check that Ollama is running and the model is loaded."
        ) from e
    except httpx.HTTPStatusError as e:
        raise OfflineModelError(
            f"Ollama returned {e.response.status_code}: {e.response.text[:300]}"
        ) from e
