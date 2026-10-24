"""Offline preflight and preview fixture check.

    python scripts/preflight_eval.py
"""
import inspect
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import tests.test_preflight as preflight_tests  # noqa: E402
import tests.test_preview_check as preview_tests  # noqa: E402


def _run(module) -> tuple:
    passed = 0
    failed = []
    for name in sorted(dir(module)):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if not callable(fn) or inspect.iscoroutinefunction(fn):
            continue
        try:
            fn()
        except TypeError as exc:
            if "missing" in str(exc):
                continue
            failed.append((name, exc))
        except Exception as exc:
            failed.append((name, exc))
        else:
            passed += 1
    return passed, failed


def main() -> None:
    total_passed = 0
    total_failed = []
    for module in (preflight_tests, preview_tests):
        passed, failed = _run(module)
        total_passed += passed
        total_failed.extend((module.__name__, name, exc) for name, exc in failed)
        print(f"{module.__name__}: {passed} passed, {len(failed)} failed")
    for module_name, name, exc in total_failed:
        print(f"FAIL {module_name}.{name}: {exc}")
    if total_failed:
        raise SystemExit(1)
    print(f"preflight eval ok ({total_passed} fixtures)")


if __name__ == "__main__":
    main()
