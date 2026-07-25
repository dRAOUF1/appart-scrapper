"""Integration tests for AdminRepository against a real Postgres."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.integration


class TestExecuteQueryReadOnly:
    def test_select_works(self, storage):
        storage.users.create_user(f"u_{time.time_ns()}")

        rows, row_count, error = storage.admin.execute_query("SELECT COUNT(*) AS n FROM users")

        assert error is None
        assert rows[0]["n"] == 1

    def test_delete_is_rejected_by_postgres_and_nothing_is_committed(self, storage):
        username = f"protected_{time.time_ns()}"
        storage.users.create_user(username)

        rows, row_count, error = storage.admin.execute_query("DELETE FROM users")

        assert error is not None
        assert "read-only" in error.lower()

        remaining = storage.users.get_user_by_username(username)
        assert remaining is not None

    def test_drop_table_is_rejected(self, storage):
        rows, row_count, error = storage.admin.execute_query("DROP TABLE users")

        assert error is not None
        # Table must still exist and be usable afterwards.
        storage.users.create_user(f"still_works_{time.time_ns()}")


class TestPurgeOldLogs:
    def test_make_interval_syntax_executes_without_error(self, storage):
        """Regression: INTERVAL '%s days' used to be invalid SQL."""
        storage.admin.log_admin_action("test_action", "details", "tester")

        deleted = storage.admin.purge_old_logs(days=30)

        assert deleted == 0  # just-inserted log isn't old enough

    def test_purges_logs_older_than_cutoff(self, storage):
        storage.admin.log_admin_action("old_action", "details", "tester")

        conn = storage._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE admin_logs SET created_at = NOW() - INTERVAL '60 days' "
                    "WHERE action = 'old_action'"
                )
            conn.commit()
        finally:
            storage._release_conn(conn)

        deleted = storage.admin.purge_old_logs(days=30)
        assert deleted == 1
