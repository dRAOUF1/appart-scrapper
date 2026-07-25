"""Tests for blacklist mode feature."""
from unittest.mock import MagicMock
from services.scrape_service import ScrapeService


class TestBlacklistMode:
    """Tests for blacklist_mode feature in scrape service."""

    def test_exclude_mode_filters_notifications(self):
        """Mode 'exclude' should skip both results and notifications."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {
            "id": 1,
            "user_id": 1,
            "criteria": {"locations": [{"city": "Paris", "postalCode": "75018", "inseeCode": "75118"}]},
            "source": "seloger",
            "ntfy_topic": "test-topic",
            "blacklisted_agencies": ["Bad Agency"],
            "blacklist_mode": "exclude",
        }
        mock_app.storage.settings.get_setting.return_value = "true"

        mock_notifier = MagicMock()
        mock_app.notifier = mock_notifier

        mock_listing = MagicMock()
        mock_listing.agency = "Bad Agency"

        def mock_save_and_link(listings, search_id):
            return listings, []

        mock_app.storage.listings.save_and_link = mock_save_and_link
        mock_app.storage.listings.get_unnotified_listings_for_search.return_value = [mock_listing]
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        mock_parser = MagicMock()
        mock_parser.cannot_search_reason.return_value = None
        mock_parser.scrape.return_value = [mock_listing]

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        from unittest.mock import patch
        with patch("parsers.get_parser", return_value=mock_parser):
            result = service.execute(search_id=1, user_id=1)

        mock_notifier.notify_new_listing.assert_not_called()

    def test_no_notify_mode_allows_results_but_skips_notifications(self):
        """Mode 'no_notify' should save results but skip notifications."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {
            "id": 1,
            "user_id": 1,
            "criteria": {"locations": [{"city": "Paris", "postalCode": "75018", "inseeCode": "75118"}]},
            "source": "seloger",
            "ntfy_topic": "test-topic",
            "blacklisted_agencies": ["Bad Agency"],
            "blacklist_mode": "no_notify",
        }
        mock_app.storage.settings.get_setting.return_value = "true"

        mock_notifier = MagicMock()
        mock_app.notifier = mock_notifier

        mock_listing = MagicMock()
        mock_listing.agency = "Bad Agency"

        def mock_save_and_link(listings, search_id):
            return listings, []

        mock_app.storage.listings.save_and_link = mock_save_and_link
        mock_app.storage.listings.get_unnotified_listings_for_search.return_value = [mock_listing]
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        mock_parser = MagicMock()
        mock_parser.cannot_search_reason.return_value = None
        mock_parser.scrape.return_value = [mock_listing]

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        from unittest.mock import patch
        with patch("parsers.get_parser", return_value=mock_parser):
            result = service.execute(search_id=1, user_id=1)

        mock_notifier.notify_new_listing.assert_not_called()

    def test_no_notify_mode_allows_non_blacklisted_notifications(self):
        """Mode 'no_notify' should send notifications for non-blacklisted agencies."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {
            "id": 1,
            "user_id": 1,
            "criteria": {"locations": [{"city": "Paris", "postalCode": "75018", "inseeCode": "75118"}]},
            "source": "seloger",
            "ntfy_topic": "test-topic",
            "blacklisted_agencies": ["Bad Agency"],
            "blacklist_mode": "no_notify",
        }
        mock_app.storage.settings.get_setting.return_value = "true"

        mock_notifier = MagicMock()
        mock_app.notifier = mock_notifier

        mock_listing_blacklisted = MagicMock()
        mock_listing_blacklisted.agency = "Bad Agency"
        mock_listing_good = MagicMock()
        mock_listing_good.agency = "Good Agency"

        def mock_save_and_link(listings, search_id):
            return listings, []

        mock_app.storage.listings.save_and_link = mock_save_and_link
        mock_app.storage.listings.get_unnotified_listings_for_search.return_value = [
            mock_listing_blacklisted, mock_listing_good
        ]
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        mock_parser = MagicMock()
        mock_parser.cannot_search_reason.return_value = None
        mock_parser.scrape.return_value = [mock_listing_blacklisted, mock_listing_good]

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        from unittest.mock import patch
        with patch("parsers.get_parser", return_value=mock_parser):
            result = service.execute(search_id=1, user_id=1)

        mock_notifier.notify_new_listing.assert_called_once()
        call_args = mock_notifier.notify_new_listing.call_args
        assert call_args[0][1].agency == "Good Agency"

    def test_default_mode_is_exclude(self):
        """Default mode should be 'exclude' when not specified."""
        mock_app = MagicMock()
        mock_app.storage.searches.get_search.return_value = {
            "id": 1,
            "user_id": 1,
            "criteria": {"locations": [{"city": "Paris", "postalCode": "75018", "inseeCode": "75118"}]},
            "source": "seloger",
            "ntfy_topic": "test-topic",
            "blacklisted_agencies": ["Bad Agency"],
        }
        mock_app.storage.settings.get_setting.return_value = "true"

        mock_notifier = MagicMock()
        mock_app.notifier = mock_notifier

        mock_listing = MagicMock()
        mock_listing.agency = "Bad Agency"

        def mock_save_and_link(listings, search_id):
            return listings, []

        mock_app.storage.listings.save_and_link = mock_save_and_link
        mock_app.storage.listings.get_unnotified_listings_for_search.return_value = [mock_listing]
        mock_app.app_context.return_value.__enter__ = MagicMock()
        mock_app.app_context.return_value.__exit__ = MagicMock()

        mock_parser = MagicMock()
        mock_parser.cannot_search_reason.return_value = None
        mock_parser.scrape.return_value = [mock_listing]

        service = ScrapeService(mock_app.storage, mock_app.notifier)
        from unittest.mock import patch
        with patch("parsers.get_parser", return_value=mock_parser):
            result = service.execute(search_id=1, user_id=1)

        mock_notifier.notify_new_listing.assert_not_called()