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
from services.offline import offline_client
from services.offline.offline_client import ollama_connect_url, resolve_ollama_base_url
from services.offline.ollama_models import lookup, offline_model_choices


def test_resolve_ollama_base_url_rewrites_loopback_only_inside_docker():
    assert resolve_ollama_base_url("http://localhost:11434", inside_docker=True) == "http://host.docker.internal:11434"
    assert resolve_ollama_base_url("http://127.0.0.1:11434/", inside_docker=True) == "http://host.docker.internal:11434"
    assert resolve_ollama_base_url("http://[::1]:11434", inside_docker=True) == "http://host.docker.internal:11434"
    assert resolve_ollama_base_url("http://192.168.1.9:11434", inside_docker=True) == "http://192.168.1.9:11434"
    assert resolve_ollama_base_url("http://127.0.0.1:11434", inside_docker=False) == "http://127.0.0.1:11434"


def test_connect_url_pins_docker_host_to_ipv4(monkeypatch):
    monkeypatch.setattr(offline_client, "_ipv4_for", lambda host: "192.0.2.10")
    assert ollama_connect_url("http://127.0.0.1:11434", inside_docker=True) == "http://192.0.2.10:11434"
    monkeypatch.setattr(offline_client, "_ipv4_for", lambda host: None)
    assert ollama_connect_url("http://localhost:11434", inside_docker=True) == "http://host.docker.internal:11434"
    assert ollama_connect_url("http://127.0.0.1:11434", inside_docker=False) == "http://127.0.0.1:11434"


def test_offline_picker_lists_catalog_when_nothing_is_installed():
    choices = offline_model_choices([])
    ids = [m["id"] for m in choices]
    assert "ollama:qwen2.5-coder:7b" in ids
    assert any(not m["installed"] for m in choices)
    assert len(choices) >= 2


def test_offline_picker_marks_installed_and_keeps_extra_local_models():
    choices = offline_model_choices(["qwen2.5-coder:7b", "llama3.2:3b"])
    by_id = {m["id"]: m for m in choices}
    assert by_id["ollama:qwen2.5-coder:7b"]["installed"] is True
    assert by_id["ollama:llama3.2:3b"]["installed"] is True
    assert by_id["ollama:hf.co/RootMonsteR/Qwen3-14B-Abliterated-GGUF:Q5_K_M"]["installed"] is False


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
    assert out["base_url"] == "http://127.0.0.1:11434"
    assert out["detail"] == ""
    by_id = {m["id"]: m for m in out["models"]}
    assert by_id["qwen2.5-coder:7b"]["installed"] is True
    assert by_id["hf.co/RootMonsteR/Qwen3-14B-Abliterated-GGUF:Q5_K_M"]["installed"] is False

    def fail_get(url, timeout=3):
        raise httpx.ConnectError("nope", request=httpx.Request("GET", url))

    monkeypatch.setattr(settings_router.httpx, "get", fail_get)
    out2 = settings_router.ollama_catalog("http://127.0.0.1:11434")
    assert out2["reachable"] is False
    assert out2["base_url"] == "http://127.0.0.1:11434"
    assert "Open the Ollama app" in out2["detail"]
    assert "host.docker.internal" not in out2["detail"]


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
