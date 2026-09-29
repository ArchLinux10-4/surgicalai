"""Claude Sonnet 5.5 is selectable and is the QA model; corrections stay on Sonnet 5.

Source-level checks (no live API calls) plus behaviour checks on the small
pipeline helpers that decide thinking / effort / tool_choice per model.
"""
import pathlib
import re
import sys

import pytest

_BACKEND = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

_PIPELINE = (_BACKEND / "services" / "pipeline.py").read_text()
_TASK_RUNNER = (_BACKEND / "services" / "task_runner.py").read_text()
_SECOND_QA = (_BACKEND / "services" / "grok_second_qa.py").read_text()

S55 = "claude-sonnet-5-5"


def _func_src(src: str, name: str) -> str:
    m = re.search(rf"^(?:async )?def {re.escape(name)}\(", src, flags=re.MULTILINE)
    assert m, f"{name} not found"
    nxt = re.search(r"^(?:async def|def|class) ", src[m.end():], flags=re.MULTILINE)
    return src[m.start(): m.end() + (nxt.start() if nxt else len(src))]


def _pipeline_module():
    try:
        from services import pipeline as p
    except Exception as exc:  # pragma: no cover - env without full deps
        pytest.skip(f"pipeline import unavailable: {exc}")
    return p


def test_helpers_treat_sonnet_55_as_adaptive_128k():
    p = _pipeline_module()
    assert p._max_output_tokens(S55) == 128000
    assert p._max_output_tokens(S55 + "-20261001") == 128000
    assert p._uses_adaptive_thinking(S55) is True
    assert p._get_thinking_kwargs(S55, 4000) == {
        "thinking": {"type": "adaptive", "display": "summarized"}}
    assert p._get_effort_kwargs(S55) == {"output_config": {"effort": "medium"}}
    assert S55 not in p._NO_XHIGH_EFFORT_MODELS


def test_sonnet_55_never_gets_disabled_or_budget_thinking():
    p = _pipeline_module()
    params = p._bounded_thinking_params(S55, 16000)
    assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "budget_tokens" not in params["thinking"]
    assert params["max_tokens"] <= 128000
    assert '"type": "disabled"' not in _PIPELINE


def test_sonnet_55_forced_tool_choice_is_downgraded():
    p = _pipeline_module()
    assert p._rejects_forced_tool_choice(S55) is True
    assert p._rejects_forced_tool_choice("claude-sonnet-5") is False
    assert S55 in p._NO_FORCED_TOOL_CHOICE_MODELS


def test_qa_call_sites_use_sonnet_55():
    assert f'_qa_model = "{S55}"' in _func_src(_PIPELINE, "run_qa_agent")
    assert f'_model = "{S55}"' in _func_src(_PIPELINE, "_run_qa_for_new_file")
    assert f'_qa_model_legacy = "{S55}"' in _func_src(_PIPELINE, "run_qa_for_changes")
    assert f'model="{S55}"' in _func_src(_TASK_RUNNER, "_run_integration_qa")
    assert f'_SECOND_QA_MODEL = "{S55}"' in _SECOND_QA


def test_qa_call_sites_no_longer_reference_sonnet_5():
    for name in ("run_qa_agent", "_run_qa_for_new_file", "run_qa_for_changes"):
        assert '"claude-sonnet-5"' not in _func_src(_PIPELINE, name), name
    assert '"claude-sonnet-5"' not in _func_src(_TASK_RUNNER, "_run_integration_qa")
    assert '"claude-sonnet-5"' not in _SECOND_QA


def test_corrections_and_defaults_stay_on_sonnet_5():
    assert "# R25: corrections always Claude" in _PIPELINE
    assert '_corr_correction_model = "claude-sonnet-5"' in _PIPELINE
    assert '_correction_model = "claude-sonnet-5"' in _PIPELINE
    assert 'get_setting("surgeon_model", "claude-sonnet-5")' in _PIPELINE
    settings = (_BACKEND / "routers" / "settings.py").read_text()
    assert 's.get("architect_model", "claude-sonnet-5")' in settings
    assert 's.get("surgeon_model", "claude-sonnet-5")' in settings


class _FakeMsg:
    def __init__(self, stop_reason="end_turn", text="ok", category=None):
        self.stop_reason = stop_reason
        self.content = [type("B", (), {"type": "text", "text": text})()] if text else []
        self.stop_details = type("D", (), {"category": category})() if category else None
        self.usage = None


def _run_with_fake_stream(p, monkeypatch, responses, **call_kwargs):
    import asyncio

    calls = []

    async def fake_stream(_client, **kwargs):
        calls.append(kwargs)
        return responses[len(calls) - 1]

    monkeypatch.setattr(p, "_stream_and_collect", fake_stream)
    msg = asyncio.run(p._safe_claude_call_refusal_fallback(
        object(), model=S55, desired_text_tokens=1000,
        system="s", messages=[{"role": "user", "content": "u"}], **call_kwargs))
    return msg, calls


def test_refusal_falls_back_to_sonnet_5_without_starvation_retry(monkeypatch):
    p = _pipeline_module()
    refused = _FakeMsg("refusal", text="", category="frontier_llm")
    fine = _FakeMsg("end_turn", text="{}")
    msg, calls = _run_with_fake_stream(p, monkeypatch, [refused, fine])
    assert msg is fine
    assert [c["model"] for c in calls] == [S55, "claude-sonnet-5"]


def test_non_refusal_makes_a_single_call(monkeypatch):
    p = _pipeline_module()
    fine = _FakeMsg("end_turn", text="{}")
    msg, calls = _run_with_fake_stream(p, monkeypatch, [fine])
    assert msg is fine
    assert [c["model"] for c in calls] == [S55]


def test_refusal_on_both_models_is_returned_as_refusal(monkeypatch):
    p = _pipeline_module()
    r1 = _FakeMsg("refusal", text="", category="cyber")
    r2 = _FakeMsg("refusal", text="", category="cyber")
    msg, calls = _run_with_fake_stream(p, monkeypatch, [r1, r2])
    assert p._is_refusal_response(msg)
    assert len(calls) == 2


def test_no_fallback_when_fallback_model_matches(monkeypatch):
    p = _pipeline_module()
    import asyncio
    refused = _FakeMsg("refusal", text="")
    calls = []

    async def fake_stream(_client, **kwargs):
        calls.append(kwargs["model"])
        return refused

    monkeypatch.setattr(p, "_stream_and_collect", fake_stream)
    msg = asyncio.run(p._safe_claude_call_refusal_fallback(
        object(), model="claude-sonnet-5", desired_text_tokens=1000,
        system="s", messages=[{"role": "user", "content": "u"}]))
    assert p._is_refusal_response(msg)
    assert calls == ["claude-sonnet-5"]


def test_qa_call_sites_use_refusal_fallback_and_check_refusals():
    for name in ("run_qa_agent", "_run_qa_for_new_file", "run_qa_for_changes"):
        body = _func_src(_PIPELINE, name)
        assert "_safe_claude_call_refusal_fallback(" in body, name
        assert "_is_refusal_response(" in body, name
    assert "_QARefusalError" in _func_src(_PIPELINE, "run_qa_agent")
    assert "_safe_claude_call_refusal_fallback(" in _func_src(_TASK_RUNNER, "_run_integration_qa")
    assert "_safe_claude_call_refusal_fallback(" in _SECOND_QA
    assert "messages.create(" not in _func_src(_TASK_RUNNER, "_run_integration_qa")
    assert "messages.create(" not in _SECOND_QA
