"""Tests for storage.py::Storage."""
from unittest.mock import patch

from storage import Storage


class TestRunMigrations:
    def test_bootstraps_instance_and_calls_run_ddl_migrations(self):
        with patch.object(Storage, "_run_ddl_migrations", autospec=True) as mock_migrate:
            Storage.run_migrations("postgresql://test:test@localhost/testdb")

        mock_migrate.assert_called_once()
        called_instance = mock_migrate.call_args.args[0]
        assert called_instance.database_url == "postgresql://test:test@localhost/testdb"

    def test_does_not_require_tables_to_already_exist(self):
        """Unlike Storage(url), run_migrations must not invoke _init_db()
        (which requires the users table to exist) — that's the whole point:
        it's how tables get created/updated in the first place."""
        with patch.object(Storage, "_init_db") as mock_init_db, \
             patch.object(Storage, "_run_ddl_migrations"):
            Storage.run_migrations("postgresql://test:test@localhost/testdb")

        mock_init_db.assert_not_called()
