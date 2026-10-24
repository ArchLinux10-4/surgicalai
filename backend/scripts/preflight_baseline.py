"""Count how often post-loop QA already blocks an edit.

The preflight gate is worth its cost only if blocking errors regularly reach
the QA stage. This reads the local debug log and, when the SQLite file is
readable, the debug_events table. It does not call the network.

    python scripts/preflight_baseline.py
"""
import json
import sqlite3
from collections import Counter
from pathlib import Path

JSONL = Path("/tmp/surgical_debug.jsonl")
DB = Path.home() / ".surgicalai" / "surgicalai.db"

# Event names the pipeline already writes when a check fires after the agent
# has finished. preview_runtime_error is the browser event LivePreview sends.
WATCH_PREFIXES = (
    "structural_qa",
    "lint_",
    "qa_retry",
    "compile",
    "syntax_validator",
    "client_preview_runtime_error",
    "preflight_",
)


def _interesting(event: str) -> bool:
    return any(event.startswith(p) or p in event for p in WATCH_PREFIXES)


def _count_jsonl(path: Path) -> Counter:
    counts = Counter()
    if not path.exists():
        return counts
    with path.open() as handle:
        for line in handle:
            try:
                event = json.loads(line).get("event") or ""
            except Exception:
                continue
            if _interesting(event):
                counts[event] += 1
    return counts


def _count_sqlite(path: Path) -> Counter:
    counts = Counter()
    if not path.exists():
        return counts
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"sqlite unreadable: {exc}")
        return counts
    try:
        rows = conn.execute(
            "SELECT event, COUNT(*) FROM debug_events GROUP BY event"
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"sqlite query failed: {exc}")
        return counts
    finally:
        conn.close()
    for event, count in rows:
        if event and _interesting(event):
            counts[event] += count
    return counts


def main() -> None:
    jsonl_counts = _count_jsonl(JSONL)
    db_counts = _count_sqlite(DB)
    print(f"jsonl {JSONL} interesting={sum(jsonl_counts.values())}")
    for event, count in jsonl_counts.most_common():
        print(f"  {count:6}  {event}")
    print(f"sqlite {DB} interesting={sum(db_counts.values())}")
    for event, count in db_counts.most_common():
        print(f"  {count:6}  {event}")
    blocking = jsonl_counts["structural_qa_blocking"] + db_counts["structural_qa_blocking"]
    retries = sum(v for k, v in (jsonl_counts + db_counts).items() if k.startswith("qa_retry"))
    crashes = (
        jsonl_counts["client_preview_runtime_error"]
        + db_counts["client_preview_runtime_error"]
    )
    print(
        f"summary structural_qa_blocking={blocking} "
        f"qa_retry_events={retries} preview_runtime_error={crashes}"
    )


if __name__ == "__main__":
    main()
