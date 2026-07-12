"""Integration: the scheduler's advisory lock actually contends across two
real connections (unlike tests/test_scheduler_lock.py, which mocks psycopg2).
"""

from __future__ import annotations

import pytest

from main import _try_acquire_scheduler_lock

pytestmark = pytest.mark.integration


def test_second_process_cannot_acquire_lock_while_first_holds_it(pg_url):
    conn1 = _try_acquire_scheduler_lock(pg_url)
    assert conn1 is not None

    conn2 = _try_acquire_scheduler_lock(pg_url)
    assert conn2 is None

    conn1.close()


def test_lock_is_available_again_after_release(pg_url):
    conn1 = _try_acquire_scheduler_lock(pg_url)
    assert conn1 is not None
    conn1.close()

    conn2 = _try_acquire_scheduler_lock(pg_url)
    assert conn2 is not None
    conn2.close()
