"""Tests for scrape service."""
import pytest
from unittest.mock import MagicMock, patch
from services.scrape_service import ScrapeService


class TestScrapeService:
    def test_execute_invalid_search(self):
        """Returns 0 when search doesn't exist."""
        mock_app = MagicMock()
        mock_app.storage.get_search.return_value = None
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        service = ScrapeService(mock_app)
        result = service.execute(search_id=999, user_id=1)
        assert result == 0

    def test_execute_wrong_user(self):
        """Returns 0 when search belongs to different user."""
        mock_app = MagicMock()
        mock_app.storage.get_search.return_value = {"user_id": 2, "criteria": {"placeIds": ["123"]}}
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        service = ScrapeService(mock_app)
        result = service.execute(search_id=1, user_id=1)
        assert result == 0

    def test_execute_empty_criteria(self):
        """Returns 0 and logs error when criteria are empty."""
        mock_app = MagicMock()
        mock_app.storage.get_search.return_value = {"user_id": 1, "criteria": {}}
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        service = ScrapeService(mock_app)
        result = service.execute(search_id=1, user_id=1)
        assert result == 0
        mock_app.storage.create_scrape_log.assert_called()

    def test_execute_unknown_parser(self):
        """Returns 0 when parser source is unknown."""
        mock_app = MagicMock()
        mock_app.storage.get_search.return_value = {
            "user_id": 1, "criteria": {"placeIds": ["123"]}, "source": "unknown_source"
        }
        mock_app.storage.get_setting.return_value = "true"
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        service = ScrapeService(mock_app)
        result = service.execute(search_id=1, user_id=1)
        assert result == 0
        mock_app.storage.create_scrape_log.assert_called()

    def test_execute_no_listings_found(self):
        """Returns 0 when parser returns no listings."""
        mock_app = MagicMock()
        mock_app.storage.get_search.return_value = {
            "user_id": 1, "criteria": {"placeIds": ["123"]}, "source": "seloger"
        }
        mock_app.storage.get_setting.return_value = "true"
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        mock_parser = MagicMock()
        mock_parser.scrape.return_value = []

        service = ScrapeService(mock_app)
        with patch("parsers.get_parser", return_value=mock_parser):
            result = service.execute(search_id=1, user_id=1)
        assert result == 0
