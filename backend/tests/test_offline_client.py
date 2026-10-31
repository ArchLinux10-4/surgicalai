import asyncio
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import services.offline.offline_client as oc
from services.offline.offline_client import ThinkStripper, _strip_think
from services.offline.offline_prompts import (
    OFFLINE_CHAT_SYSTEM,
    OFFLINE_EDIT_SYSTEM,
    OFFLINE_UNRESTRICTED_HINT,
    offline_system,
)
from services.offline.ollama_models import is_uncensored


def _settings(monkeypatch, **vals):
    monkeypatch.setattr(
        oc,
        "get_setting",
        lambda k, d=None: vals.get(k, d if d is not None else ""),
    )


UNCENSORED = "hf.co/RootMonsteR/Qwen3-14B-Abliterated-GGUF:Q5_K_M"


def test_resolve_offline_model_architect_ollama_wins(monkeypatch):
    _settings(
        monkeypatch,
        architect_model=f"ollama:{UNCENSORED}",
        ollama_model="qwen2.5-coder:7b",
    )
    assert oc.resolve_offline_model() == UNCENSORED


def test_resolve_offline_model_cloud_falls_back(monkeypatch):
    _settings(
        monkeypatch,
        architect_model="gpt-5.6-luna",
        ollama_model="qwen2.5-coder:7b",
    )
    assert oc.resolve_offline_model() == "qwen2.5-coder:7b"


def test_resolve_offline_model_empty_architect_falls_back(monkeypatch):
    _settings(monkeypatch, architect_model="", ollama_model="qwen2.5-coder:7b")
    assert oc.resolve_offline_model() == "qwen2.5-coder:7b"


def test_strip_think_closed_and_unterminated():
    open_t, close_t = "<" + "think>", "</" + "think>"
    assert _strip_think(f"hello {open_t}secret{close_t} world") == "hello world"
    assert _strip_think(f"visible {open_t}cut off").strip() == "visible"


def test_think_stripper_split_across_chunks():
    s = ThinkStripper()
    assert s.feed("<thi") == ""
    assert s.feed("nk>hidden</th") == ""
    assert s.feed("ink>answer") == "answer"
    assert s.flush() == ""


def test_think_stripper_stray_close_tag():
    s = ThinkStripper()
    assert s.feed("</" + "think>\n\npong") == "pong"
    assert _strip_think("</" + "think>\n\npong").strip() == "pong"
    s2 = ThinkStripper()
    assert s2.feed("</th") == ""
    assert s2.feed("ink>\npong") == "pong"


def test_think_stripper_passthrough_and_trailing_lt():
    s = ThinkStripper()
    assert s.feed("plain text") == "plain text"
    s2 = ThinkStripper()
    assert s2.feed("before<") == "before"
    assert s2.flush() == "<"


def test_offline_system_prompts():
    assert offline_system("chat", "qwen2.5-coder:7b") == OFFLINE_CHAT_SYSTEM
    assert offline_system("edit", "qwen2.5-coder:7b") == OFFLINE_EDIT_SYSTEM
    hint = offline_system("chat", UNCENSORED)
    assert OFFLINE_UNRESTRICTED_HINT in hint
    assert is_uncensored("hf.co/RootMonsteR/Qwen3-14B-Abliterated-GGUF:Q4_K_M")
    assert not is_uncensored("qwen3:14b")


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _RecordingClient:
    last_payload = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, json=None):
        _RecordingClient.last_payload = json
        return _FakeResponse(
            {
                "message": {"content": "ok"},
                "done_reason": "stop",
            }
        )


def test_ollama_chat_once_payload_no_think_for_7b(monkeypatch):
    _settings(
        monkeypatch,
        architect_model="ollama:qwen2.5-coder:7b",
        ollama_base_url="http://localhost:11434",
        ollama_num_ctx="16384",
    )
    monkeypatch.setattr(oc.httpx, "AsyncClient", _RecordingClient)
    asyncio.run(oc.ollama_chat_once([{"role": "user", "content": "hi"}]))
    p = _RecordingClient.last_payload
    assert "think" not in p
    assert p["options"]["num_ctx"] == 16384
    assert "num_predict" in p["options"]


def test_ollama_chat_once_payload_think_false_for_qwen3(monkeypatch):
    _settings(
        monkeypatch,
        architect_model=f"ollama:{UNCENSORED}",
        ollama_base_url="http://localhost:11434",
        ollama_num_ctx="16384",
    )
    monkeypatch.setattr(oc.httpx, "AsyncClient", _RecordingClient)
    asyncio.run(oc.ollama_chat_once([{"role": "user", "content": "hi"}]))
    p = _RecordingClient.last_payload
    assert p.get("think") is False
