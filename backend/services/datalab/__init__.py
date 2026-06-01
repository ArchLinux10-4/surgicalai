"""
DataLab — isolated spreadsheet/CSV power-house lane.

This package is fully self-contained and additive. The code-surgery pipeline
(pipeline.py, task_planner.py, ast_parser.py, QA gate) is NOT touched by any
module in this package. The entire lane is gated behind the DATALAB_ENABLED
feature flag; when off, the application behaves byte-identically to before.
"""

# Feature-flag accessor is re-exported for convenience.
from .config import datalab_enabled  # noqa: F401

__all__ = ["datalab_enabled"]
