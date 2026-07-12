"""Tests for repository base class."""
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
