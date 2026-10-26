"""A diff shown before QA finishes must still Apply.

The early card only sends symbol name, original_code, and new_code.
Apply used to 422 on the missing symbol fields and confidence, and the
button reported that as "[object Object]".
"""
from services.surgical_editor import apply_change
from models.schemas import SurgicalApplyRequest


def test_thin_diff_validates_and_replaces_the_anchor():
    change = {
        "id": "c1",
        "symbol": {"name": "config.py", "full_path": "config.py"},
        "original_code": 'OPENAI_MODEL = "gpt-4.1"\n',
        "new_code": 'OPENAI_MODEL = "gpt-5.6-luna"\n',
        "diff": '-OPENAI_MODEL = "gpt-4.1"\n+OPENAI_MODEL = "gpt-5.6-luna"\n',
        "description": "point the default at luna",
        "applied": False,
        "qa_result": {"verdict": "safe", "qa_score": 9},
    }
    req = SurgicalApplyRequest(
        file_path="config.py",
        changes=[change],
        file_content='HEADER\nOPENAI_MODEL = "gpt-4.1"\nTAIL\n',
    )
    assert req.changes[0].confidence == 8
    assert req.changes[0].symbol.symbol_type.value == "variable"
    assert req.changes[0].symbol.start_line == 0
    out = apply_change('HEADER\nOPENAI_MODEL = "gpt-4.1"\nTAIL\n', req.changes[0])
    assert out == 'HEADER\nOPENAI_MODEL = "gpt-5.6-luna"\nTAIL\n'
    assert req.changes[0].applied is True
