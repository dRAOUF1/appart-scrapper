"""Tests for the scheduler singleton advisory lock in main.py."""
from unittest.mock import MagicMock, patch

from main import _try_acquire_scheduler_lock


class TestSchedulerLock:
    def test_returns_connection_when_lock_acquired(self):
        fake_conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (True,)
        fake_conn.cursor.return_value.__enter__.return_value = cur

        with patch("psycopg2.connect", return_value=fake_conn):
            result = _try_acquire_scheduler_lock("postgresql://x")

        assert result is fake_conn
        fake_conn.close.assert_not_called()

    def test_returns_none_and_closes_when_lock_held_elsewhere(self):
        fake_conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (False,)
        fake_conn.cursor.return_value.__enter__.return_value = cur

        with patch("psycopg2.connect", return_value=fake_conn):
            result = _try_acquire_scheduler_lock("postgresql://x")

        assert result is None
        fake_conn.close.assert_called_once()

    def test_returns_none_on_connection_error(self):
        with patch("psycopg2.connect", side_effect=Exception("db down")):
            result = _try_acquire_scheduler_lock("postgresql://x")

        assert result is None
