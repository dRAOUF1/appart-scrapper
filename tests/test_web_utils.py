"""Tests for web_utils.to_int."""
from web_utils import to_int


class TestToInt:
    def test_valid_string(self):
        assert to_int("42", 0) == 42

    def test_valid_int(self):
        assert to_int(7, 0) == 7

    def test_invalid_string_returns_default(self):
        assert to_int("abc", 5) == 5

    def test_none_returns_default(self):
        assert to_int(None, 5) == 5

    def test_empty_string_returns_default(self):
        assert to_int("", 5) == 5
