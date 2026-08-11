"""Shared fake Request for router unit tests that call handlers directly."""
from types import SimpleNamespace


def fake_request(user_id: str = "test-user", is_admin: bool = False):
    """Minimal stand-in for starlette Request — only request.state is used."""
    return SimpleNamespace(
        state=SimpleNamespace(user_id=user_id, username=user_id, is_admin=is_admin)
    )
