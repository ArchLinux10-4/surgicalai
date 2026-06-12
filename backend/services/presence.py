"""
In-memory user presence tracker.

Tracks last API activity per user.  Zero DB overhead — just a dict
updated on every authenticated request from the auth middleware.
Lost on process restart, which is fine (rebuilds as users hit the API).
"""
import time
import threading

_lock = threading.Lock()
_presence: dict[str, dict] = {}
# {user_id: {"username": str, "last_seen": float, "last_path": str}}


def touch(user_id: str, username: str, path: str) -> None:
    """Record an API hit for this user. Called from auth middleware."""
    with _lock:
        _presence[user_id] = {
            "username": username,
            "last_seen": time.time(),
            "last_path": path,
        }


def get_all() -> list[dict]:
    """
    Return presence info for all seen users with computed status.

    Status thresholds:
      active  — last API call < 2 min ago
      idle    — last API call 2–15 min ago
      offline — > 15 min ago
    """
    now = time.time()
    result = []
    with _lock:
        for uid, info in _presence.items():
            age = now - info["last_seen"]
            if age < 120:
                status = "active"
            elif age < 900:
                status = "idle"
            else:
                status = "offline"
            result.append({
                "user_id": uid,
                "username": info["username"],
                "status": status,
                "last_seen_seconds_ago": round(age),
                "last_path": info["last_path"],
            })
    # Sort: active first, then idle, then offline
    order = {"active": 0, "idle": 1, "offline": 2}
    result.sort(key=lambda r: (order.get(r["status"], 9), r["username"]))
    return result
