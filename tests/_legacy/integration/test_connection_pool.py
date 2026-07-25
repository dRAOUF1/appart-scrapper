"""Integration tests for BaseRepository's connection pool against a real Postgres."""

from __future__ import annotations

import psycopg2.extensions
import pytest

from repositories.search_repo import SearchRepository
from repositories.user_repo import UserRepository

pytestmark = pytest.mark.integration


class TestPoolSharing:
    def test_pool_is_shared_across_repos_for_same_url(self, pg_url):
        repo_a = UserRepository(pg_url)
        repo_b = SearchRepository(pg_url)

        assert repo_a._get_pool() is repo_b._get_pool()

    def test_borrow_and_release_cycle_works(self, pg_url):
        repo = UserRepository(pg_url)

        conn = repo._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone() == (1,)
        finally:
            repo._release_conn(conn)

        # Borrowing again must return a usable connection (from the pool).
        conn2 = repo._get_conn()
        try:
            with conn2.cursor() as cur:
                cur.execute("SELECT 2")
                assert cur.fetchone() == (2,)
        finally:
            repo._release_conn(conn2)


class TestAbortedTransactionCleanup:
    def test_aborted_transaction_is_rolled_back_before_returning_to_pool(self, pg_url):
        repo = UserRepository(pg_url)
        conn = repo._get_conn()

        with conn.cursor() as cur:
            with pytest.raises(Exception):
                cur.execute("SELECT * FROM this_table_does_not_exist")

        assert conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_INERROR

        repo._release_conn(conn)

        # Re-borrow (may or may not be the same physical connection — pool
        # internals aren't guaranteed — but it must be usable either way).
        conn2 = repo._get_conn()
        try:
            assert conn2.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_INERROR
            with conn2.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone() == (1,)
        finally:
            repo._release_conn(conn2)
