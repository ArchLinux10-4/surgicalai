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
