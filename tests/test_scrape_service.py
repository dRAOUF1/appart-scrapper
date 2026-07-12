"""Tests for scrape service."""
from unittest.mock import MagicMock, patch
from services.scrape_service import ScrapeService


class TestScrapeService:
    def test_execute_invalid_search(self):
        """Returns 0 when search doesn't exist."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = None
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        result = service.execute(search_id=999, user_id=1)
        assert result == 0

    def test_execute_wrong_user(self):
        """Returns 0 when search belongs to different user."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {"user_id": 2, "criteria": {"placeIds": ["123"]}}
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        result = service.execute(search_id=1, user_id=1)
        assert result == 0

    def test_execute_empty_criteria(self):
        """Returns 0 and logs error when criteria are empty."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {"user_id": 1, "criteria": {}}
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        result = service.execute(search_id=1, user_id=1)
        assert result == 0
        mock_app.storage.scrape_logs.create_scrape_log.assert_called()

    def test_execute_unknown_parser(self):
        """Returns 0 when parser source is unknown."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {
            "user_id": 1, "criteria": {"placeIds": ["123"]}, "source": "unknown_source"
        }
        mock_app.storage.settings.get_setting.return_value = "true"
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        result = service.execute(search_id=1, user_id=1)
        assert result == 0
        mock_app.storage.scrape_logs.create_scrape_log.assert_called()

    def test_execute_no_listings_found(self):
        """Returns 0 when parser returns no listings."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {
            "user_id": 1, "criteria": {"placeIds": ["123"]}, "source": "seloger"
        }
        mock_app.storage.settings.get_setting.return_value = "true"
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        mock_parser = MagicMock()
        mock_parser.scrape.return_value = []

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        with patch("parsers.get_parser", return_value=mock_parser):
            result = service.execute(search_id=1, user_id=1)
        assert result == 0

    def test_execute_scrape_failure_is_logged_distinctly_from_empty_result(self):
        """A real scraping error (e.g. anti-bot block) must not be logged as 'no listings found'."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {
            "user_id": 1, "criteria": {"placeIds": ["123"]}, "source": "seloger"
        }
        mock_app.storage.settings.get_setting.return_value = "true"
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        mock_parser = MagicMock()
        mock_parser.scrape.side_effect = ValueError("Ton IP est bloquée par DataDome")

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        with patch("parsers.get_parser", return_value=mock_parser):
            result = service.execute(search_id=1, user_id=1)

        assert result == 0
        _, kwargs = mock_app.storage.scrape_logs.create_scrape_log.call_args
        assert "DataDome" in kwargs["error_message"]
        assert kwargs["error_message"] != "Aucune annonce trouvée"


class TestNotificationDurability:
    def _base_app(self, listing):
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {
            "user_id": 1, "criteria": {"placeIds": ["123"]}, "source": "seloger",
            "ntfy_topic": "test-topic", "blacklist_mode": "exclude", "blacklisted_agencies": [],
        }
        mock_app.storage.settings.get_setting.return_value = "true"
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()
        mock_app.storage.listings.save_and_link.return_value = ([listing], [])
        mock_app.storage.listings.get_unnotified_listings_for_search.return_value = [listing]
        return mock_app

    def test_failed_notification_is_not_marked_notified(self):
        """A failed ntfy send must not be marked notified, so it's retried next scrape."""
        listing = MagicMock()
        listing.agency = "Some Agency"
        listing.listing_id = "sl_1"
        mock_app = self._base_app(listing)
        mock_app.notifier.notify_new_listing.return_value = False

        mock_parser = MagicMock()
        mock_parser.scrape.return_value = [listing]

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        with patch("parsers.get_parser", return_value=mock_parser):
            service.execute(search_id=1, user_id=1)

        mock_app.storage.listings.mark_listings_notified.assert_called_once_with(1, [])

    def test_successful_notification_is_marked_notified(self):
        """A successful ntfy send marks the listing as notified so it's not resent."""
        listing = MagicMock()
        listing.agency = "Some Agency"
        listing.listing_id = "sl_1"
        mock_app = self._base_app(listing)
        mock_app.notifier.notify_new_listing.return_value = True

        mock_parser = MagicMock()
        mock_parser.scrape.return_value = [listing]

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        with patch("parsers.get_parser", return_value=mock_parser):
            service.execute(search_id=1, user_id=1)

        mock_app.storage.listings.mark_listings_notified.assert_called_once_with(1, ["sl_1"])
