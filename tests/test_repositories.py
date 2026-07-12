"""Tests for repository base class."""
from unittest.mock import MagicMock, patch

import psycopg2.extensions

from repositories.base import BaseRepository


class TestBaseRepository:
    def test_parse_json_column_dict(self):
        repo = BaseRepository.__new__(BaseRepository)
        row = {"data": {"key": "value"}}
        result = repo._parse_json_column(row, "data")
        assert result["data"] == {"key": "value"}

    def test_parse_json_column_string(self):
        repo = BaseRepository.__new__(BaseRepository)
        row = {"data": '{"key": "value"}'}
        result = repo._parse_json_column(row, "data")
        assert result["data"] == {"key": "value"}

    def test_parse_json_column_invalid_string(self):
        repo = BaseRepository.__new__(BaseRepository)
        row = {"data": "not-json"}
        result = repo._parse_json_column(row, "data")
        assert result["data"] == {}

    def test_parse_json_column_missing(self):
        repo = BaseRepository.__new__(BaseRepository)
        row = {"other": "value"}
        repo._parse_json_column(row, "data")
        assert "data" not in row

    def test_init_stores_database_url(self):
        repo = BaseRepository("postgresql://test:test@localhost/testdb")
        assert repo.database_url == "postgresql://test:test@localhost/testdb"

    def test_release_conn_rolls_back_aborted_transaction_before_pooling(self):
        """A connection left in an aborted transaction state must be rolled
        back before returning it to the pool, or the next borrower would
        inherit a poisoned transaction."""
        repo = BaseRepository.__new__(BaseRepository)
        conn = MagicMock()
        conn.get_transaction_status.return_value = psycopg2.extensions.TRANSACTION_STATUS_INERROR
        conn.closed = False
        fake_pool = MagicMock()
        repo._get_pool = lambda: fake_pool

        repo._release_conn(conn)

        conn.rollback.assert_called_once()
        fake_pool.putconn.assert_called_once_with(conn)
        conn.close.assert_not_called()

    def test_release_conn_does_not_rollback_healthy_transaction(self):
        repo = BaseRepository.__new__(BaseRepository)
        conn = MagicMock()
        conn.get_transaction_status.return_value = psycopg2.extensions.TRANSACTION_STATUS_IDLE
        conn.closed = False
        fake_pool = MagicMock()
        repo._get_pool = lambda: fake_pool

        repo._release_conn(conn)

        conn.rollback.assert_not_called()
        fake_pool.putconn.assert_called_once_with(conn)

    def test_release_conn_keeps_shared_request_connection_open(self):
        from flask import Flask

        app = Flask(__name__)
        repo = BaseRepository.__new__(BaseRepository)
        conn = MagicMock()
        fake_pool = MagicMock()
        repo._get_pool = lambda: fake_pool

        with app.test_request_context("/"):
            from flask import g
            g._db_conn = conn
            repo._release_conn(conn)

        fake_pool.putconn.assert_not_called()
        conn.close.assert_not_called()

    def test_pool_is_shared_across_repository_instances_for_same_url(self):
        from repositories.user_repo import UserRepository
        from repositories.search_repo import SearchRepository

        url = "postgresql://test:test@localhost/shared_pool_test_db"
        repo_a = UserRepository(url)
        repo_b = SearchRepository(url)

        try:
            with patch("psycopg2.pool.ThreadedConnectionPool") as MockPool:
                MockPool.return_value = MagicMock()
                pool_a = repo_a._get_pool()
                pool_b = repo_b._get_pool()

            assert pool_a is pool_b
            MockPool.assert_called_once()
        finally:
            BaseRepository._pools.pop(url, None)
