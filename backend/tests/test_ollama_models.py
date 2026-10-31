import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from routers import settings as settings_router
from services.offline.ollama_models import lookup


def test_lookup_known_and_unknown():
    assert lookup("qwen2.5-coder:7b") is not None
    assert lookup("QWEN2.5-CODER:7B") is not None
    assert lookup("not-in-catalog") is None


def test_ollama_catalog_installed_and_unreachable(monkeypatch):
    monkeypatch.setattr(settings_router, "USE_POSTGRES", False)

    class TagsResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"models": [{"name": "qwen2.5-coder:7b", "size": 4_700_000_000}]}

    monkeypatch.setattr(settings_router.httpx, "get", lambda url, timeout=3: TagsResp())

    out = settings_router.ollama_catalog("http://127.0.0.1:11434")
    assert out["reachable"] is True
    by_id = {m["id"]: m for m in out["models"]}
    assert by_id["qwen2.5-coder:7b"]["installed"] is True
    assert by_id["hf.co/RootMonsteR/Qwen3-14B-Abliterated-GGUF:Q5_K_M"]["installed"] is False

    def fail_get(url, timeout=3):
        raise httpx.ConnectError("nope", request=httpx.Request("GET", url))

    monkeypatch.setattr(settings_router.httpx, "get", fail_get)
    out2 = settings_router.ollama_catalog("http://127.0.0.1:11434")
    assert out2["reachable"] is False


def test_ollama_pull_unknown_400(monkeypatch):
    monkeypatch.setattr(settings_router, "USE_POSTGRES", False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(settings_router.ollama_pull({"name": "evil-model"}))
    assert exc.value.status_code == 400


def test_ollama_pull_hosted_404(monkeypatch):
    monkeypatch.setattr(settings_router, "USE_POSTGRES", True)
    with pytest.raises(HTTPException) as exc:
        settings_router.ollama_catalog("")
    assert exc.value.status_code == 404


def test_ollama_pull_duplicate_409(monkeypatch):
    monkeypatch.setattr(settings_router, "USE_POSTGRES", False)
    settings_router._PULLS.add("qwen2.5-coder:7b")
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                settings_router.ollama_pull(
                    {"name": "qwen2.5-coder:7b", "base_url": "http://127.0.0.1:11434"}
                )
            )
        assert exc.value.status_code == 409
    finally:
        settings_router._PULLS.discard("qwen2.5-coder:7b")


def test_ollama_pull_relay_and_error_line(monkeypatch):
    monkeypatch.setattr(settings_router, "USE_POSTGRES", False)
    settings_router._PULLS.clear()

    lines = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "success"}),
    ]

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def aiter_lines(self):
            for ln in lines:
                yield ln

        async def aread(self):
            return b""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def stream(self, method, url, json=None):
            return FakeStream()

    monkeypatch.setattr(settings_router.httpx, "AsyncClient", lambda **k: FakeClient())

    async def _run():
        resp = await settings_router.ollama_pull(
            {"name": "qwen2.5-coder:7b", "base_url": "http://127.0.0.1:11434"}
        )
        chunks = []
        async for part in resp.body_iterator:
            chunks.append(part)
        return "".join(
            c.decode() if isinstance(c, (bytes, bytearray)) else c for c in chunks
        )

    text = asyncio.run(_run())
    assert "pulling manifest" in text
    assert "success" in text
    assert "qwen2.5-coder:7b" not in settings_router._PULLS
