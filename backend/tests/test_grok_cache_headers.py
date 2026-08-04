"""
Tests for Grok gap #1: the ``x-grok-conv-id`` prompt-cache-routing header must
actually reach the real ``openai.OpenAI`` client instance returned by
``get_grok_client`` — not just be constructed and silently dropped before the
outgoing request (the exact class of bug fixed upstream in
NousResearch/hermes-agent#22705 / PR #22708).

xAI's own docs (docs.x.ai/developers/grok-4-5, "Important details";
docs.x.ai/developers/advanced-api-usage/prompt-caching/best-practices) say to
always set a stable ``x-grok-conv-id`` header so a conversation's requests
route to the same cache-warm server.

NO LIVE API CALLS. ``_get_grok_key`` is mocked (same pattern as
tests/test_grok_provider.py / tests/test_grok_tool_call_handling.py) so no
real API key or network access is needed. The real, installed ``openai.OpenAI``
client class is used (not a fake stand-in) so this test actually exercises the
SDK's ``default_headers`` constructor kwarg end-to-end.
"""
import pathlib
import sys

import pytest

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

from services import grok_provider as gp  # noqa: E402


class _Rec:
    """Collects (event, kwargs) so tests can assert _dlog fired on the path."""

    def __init__(self):
        self.events = []

    def __call__(self, event, **kw):
        self.events.append((event, kw))

    @property
    def names(self):
        return [e for e, _ in self.events]


def _mock_key(monkeypatch, key="xai-test-key"):
    monkeypatch.setattr(gp, "get_user_api_key", lambda u, k: "ENCRYPTED")
    monkeypatch.setattr(gp, "decrypt_api_key", lambda v: key)


# ─────────────────────────────────────────────────────────────────────────
# The header must reach the REAL openai.OpenAI client instance, not just a
# helper function's return value (the hermes-agent-style bug this guards
# against: a header built in one place but dropped before the real request).
# ─────────────────────────────────────────────────────────────────────────

def test_get_grok_client_real_openai_instance_carries_cache_header(monkeypatch):
    _mock_key(monkeypatch)
    client = gp.get_grok_client(user_id="user-42", session_id="session-abc")

    # This is the REAL openai.OpenAI class (grok_provider imports it locally
    # and does not fake it out here) — default_headers is the SDK's own
    # documented mechanism for headers applied to every request made through
    # this client instance.
    import openai
    assert isinstance(client, openai.OpenAI)
    assert client.default_headers.get("x-grok-conv-id") == "surgicalai-session-abc"


def test_get_grok_client_header_value_changes_with_session_id(monkeypatch):
    _mock_key(monkeypatch)
    client_a = gp.get_grok_client(user_id="user-1", session_id="session-A")
    client_b = gp.get_grok_client(user_id="user-1", session_id="session-B")

    header_a = client_a.default_headers.get("x-grok-conv-id")
    header_b = client_b.default_headers.get("x-grok-conv-id")

    assert header_a != header_b
    assert header_a == "surgicalai-session-A"
    assert header_b == "surgicalai-session-B"


def test_get_grok_client_header_value_stable_for_same_session_id_across_calls(monkeypatch):
    _mock_key(monkeypatch)
    client_1 = gp.get_grok_client(user_id="user-1", session_id="session-stable")
    client_2 = gp.get_grok_client(user_id="user-1", session_id="session-stable")

    header_1 = client_1.default_headers.get("x-grok-conv-id")
    header_2 = client_2.default_headers.get("x-grok-conv-id")

    assert header_1 == header_2 == "surgicalai-session-stable"


def test_get_grok_client_falls_back_to_user_id_only_key_when_no_session_id(monkeypatch):
    """Callers that cannot supply a session_id (default "") still get a
    per-user cache key rather than no header at all."""
    _mock_key(monkeypatch)
    client = gp.get_grok_client(user_id="user-only")
    assert client.default_headers.get("x-grok-conv-id") == "surgicalai-user-only"


def test_get_grok_client_anon_key_when_neither_session_nor_user_id():
    """``get_grok_client`` itself requires a resolvable user_id
    (``_get_grok_key`` is per-user-only, pre-existing/unrelated behaviour —
    see test_grok_provider.py::test_get_grok_key_raises_when_no_user_id), so
    the "both empty" case for the header itself is exercised directly against
    ``prompt_cache_headers``, the helper ``get_grok_client`` calls internally."""
    headers = gp.prompt_cache_headers(session_id="", user_id="")
    assert headers == {"x-grok-conv-id": "surgicalai-anon"}


# ─────────────────────────────────────────────────────────────────────────
# _dlog coverage: a new event fires on both the success and failure path,
# per project convention (every branch logs, including error branches).
# ─────────────────────────────────────────────────────────────────────────

def test_get_grok_client_logs_cache_headers_created_event(monkeypatch):
    _mock_key(monkeypatch)
    rec = _Rec()
    client = gp.get_grok_client(user_id="user-1", session_id="sess-1", dlog=rec)
    assert "grok_client_created_with_cache_headers" in rec.names
    # sanity: the client that was logged about actually carries the header.
    assert client.default_headers.get("x-grok-conv-id") == "surgicalai-sess-1"


def test_get_grok_client_cache_headers_build_failure_falls_back_gracefully(monkeypatch):
    """If header construction blows up, get_grok_client must never raise —
    it falls back to no extra headers and logs
    ``grok_cache_headers_build_failed``."""
    _mock_key(monkeypatch)
    rec = _Rec()

    def boom(*a, **kw):
        raise RuntimeError("cache header build exploded")

    monkeypatch.setattr(gp, "prompt_cache_headers", boom)

    client = gp.get_grok_client(user_id="user-1", session_id="sess-1", dlog=rec)

    import openai
    assert isinstance(client, openai.OpenAI)
    assert "grok_cache_headers_build_failed" in rec.names
    # Client itself is still created successfully — SDK's default headers
    # dict just won't carry a custom x-grok-conv-id in this degraded case
    # (may still contain the SDK's own baseline headers).
    assert client.default_headers.get("x-grok-conv-id") is None


def test_get_grok_client_still_logs_grok_client_created_event(monkeypatch):
    """Pre-existing event name must still fire (byte-identical behaviour for
    the rest of the create-client path)."""
    _mock_key(monkeypatch)
    rec = _Rec()
    gp.get_grok_client(user_id="user-1", session_id="sess-1", dlog=rec)
    assert "grok_client_created" in rec.names


# ─────────────────────────────────────────────────────────────────────────
# openai SDK support confirmation — the pinned/installed version really
# accepts default_headers as a constructor kwarg (verified against the real
# installed package, not assumed from docs).
# ─────────────────────────────────────────────────────────────────────────

def test_installed_openai_sdk_supports_default_headers_kwarg():
    import inspect
    import openai
    sig = inspect.signature(openai.OpenAI.__init__)
    assert "default_headers" in sig.parameters
