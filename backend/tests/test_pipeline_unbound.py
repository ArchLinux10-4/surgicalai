"""pyflakes F821/F823 on the live pipeline.

Plan builds used to crash because MAX_QA_RETRIES was read before its only
assignment. pyflakes reports that as "undefined name" / "referenced before
assignment". Unused imports are pre-existing and are not this gate.
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

PIPELINE = os.path.join(_BACKEND, "services", "pipeline.py")


def test_pipeline_has_no_unbound_names():
    from pyflakes.api import checkPath
    from pyflakes.reporter import Reporter
    import io

    out = io.StringIO()
    reporter = Reporter(out, out)
    checkPath(PIPELINE, reporter)
    bad = [
        line for line in out.getvalue().splitlines()
        if "undefined name" in line or "referenced before assignment" in line
    ]
    assert bad == [], "\n".join(bad)
