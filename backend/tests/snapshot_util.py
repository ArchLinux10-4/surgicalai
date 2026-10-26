"""Compare a prompt to a checked-in snapshot.

Set UPDATE_SNAPSHOTS=1 to rewrite the files after an intentional prompt edit.
"""
import difflib
import os
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "prompt_snapshots"


def assert_snapshot(name: str, text: str) -> None:
    path = _DIR / f"{name}.txt"
    body = text if text.endswith("\n") else text + "\n"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return
    assert path.is_file(), f"missing {path.name}; run with UPDATE_SNAPSHOTS=1"
    saved = path.read_text()
    if saved == body:
        return
    diff = "".join(difflib.unified_diff(
        saved.splitlines(True),
        body.splitlines(True),
        fromfile=f"{name}.txt",
        tofile=f"{name}.txt (now)",
    ))
    raise AssertionError(diff[:6000])
