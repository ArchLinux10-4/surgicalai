"""Stop one stuck test from holding the suite.

Plan-stream tests can block inside a stream that never ends (sleep or
epoll). The default limit turns that into a failure. Mark a test ``slow``
when it is expected to take longer.
"""
import signal

import pytest

LIMIT_S = 45
SLOW_S = 180


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    limit = SLOW_S if item.get_closest_marker("slow") else LIMIT_S

    def _on_alarm(signum, frame):
        raise TimeoutError(f"{item.nodeid} stopped after {limit}s")

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(limit)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
