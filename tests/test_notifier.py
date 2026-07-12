"""Tests for notifier.py (ntfy.sh push notifications)."""
from unittest.mock import MagicMock, patch

import requests

from models.listing import Listing
from notifier import Notifier


class TestSend:
    def test_returns_true_on_200(self):
        notifier = Notifier()
        resp = MagicMock(status_code=200)
        with patch("notifier.requests.post", return_value=resp) as mock_post:
            ok = notifier.send("topic", "Title", "Message")
        assert ok is True
        mock_post.assert_called_once()

    def test_returns_false_on_non_200(self):
        notifier = Notifier()
        resp = MagicMock(status_code=500, text="server error")
        with patch("notifier.requests.post", return_value=resp):
            ok = notifier.send("topic", "Title", "Message")
        assert ok is False

    def test_returns_false_on_network_error(self):
        notifier = Notifier()
        with patch("notifier.requests.post", side_effect=requests.RequestException("boom")):
            ok = notifier.send("topic", "Title", "Message")
        assert ok is False

    def test_includes_click_and_actions_headers_when_url_given(self):
        notifier = Notifier()
        resp = MagicMock(status_code=200)
        with patch("notifier.requests.post", return_value=resp) as mock_post:
            notifier.send("topic", "Title", "Message", url="https://example.com/x")
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Click"] == "https://example.com/x"
        assert "https://example.com/x" in headers["Actions"]

    def test_omits_click_header_without_url(self):
        notifier = Notifier()
        resp = MagicMock(status_code=200)
        with patch("notifier.requests.post", return_value=resp) as mock_post:
            notifier.send("topic", "Title", "Message")
        headers = mock_post.call_args.kwargs["headers"]
        assert "Click" not in headers

    def test_endpoint_strips_trailing_slash_from_server(self):
        notifier = Notifier(server="https://ntfy.example.com/")
        resp = MagicMock(status_code=200)
        with patch("notifier.requests.post", return_value=resp) as mock_post:
            notifier.send("topic", "Title", "Message")
        assert mock_post.call_args.args[0] == "https://ntfy.example.com/topic"


class TestSanitizeHeader:
    def test_strips_non_ascii_characters(self):
        notifier = Notifier()
        assert notifier._sanitize_header("Appartement à Paris") == "Appartement ? Paris"

    def test_leaves_ascii_untouched(self):
        notifier = Notifier()
        assert notifier._sanitize_header("Plain ASCII") == "Plain ASCII"


class TestNotifyNewListing:
    def test_message_includes_all_present_fields(self):
        notifier = Notifier()
        listing = Listing(
            listing_id="sl_1", url="https://example.com/sl_1",
            agency="Agence X", price="1000€", surface="50", rooms="3", location="Paris",
        )
        resp = MagicMock(status_code=200)
        with patch("notifier.requests.post", return_value=resp) as mock_post:
            notifier.notify_new_listing("topic", listing)

        message = mock_post.call_args.kwargs["data"].decode("utf-8")
        assert "Agence X" in message
        assert "1000€" in message
        assert "50" in message
        assert "3" in message
        assert "Paris" in message

    def test_fallback_message_when_no_fields_present(self):
        notifier = Notifier()
        listing = Listing(listing_id="sl_1", url="https://example.com/sl_1")
        resp = MagicMock(status_code=200)
        with patch("notifier.requests.post", return_value=resp) as mock_post:
            notifier.notify_new_listing("topic", listing)

        message = mock_post.call_args.kwargs["data"].decode("utf-8")
        assert message == "Nouvelle annonce disponible"

    def test_uses_listing_url_and_high_priority(self):
        notifier = Notifier()
        listing = Listing(listing_id="sl_1", url="https://example.com/sl_1")
        resp = MagicMock(status_code=200)
        with patch("notifier.requests.post", return_value=resp) as mock_post:
            notifier.notify_new_listing("topic", listing)

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Click"] == "https://example.com/sl_1"
        assert headers["Priority"] == "high"


class TestNotifySummary:
    def test_no_op_when_no_new_listings(self):
        notifier = Notifier()
        with patch("notifier.requests.post") as mock_post:
            ok = notifier.notify_summary("topic", new_count=0, total_scanned=10)
        assert ok is True
        mock_post.assert_not_called()

    def test_sends_when_new_listings_present(self):
        notifier = Notifier()
        resp = MagicMock(status_code=200)
        with patch("notifier.requests.post", return_value=resp) as mock_post:
            ok = notifier.notify_summary("topic", new_count=3, total_scanned=10)
        assert ok is True
        mock_post.assert_called_once()


class TestSendTest:
    def test_sends_low_priority_test_message(self):
        notifier = Notifier()
        resp = MagicMock(status_code=200)
        with patch("notifier.requests.post", return_value=resp) as mock_post:
            ok = notifier.send_test("topic")
        assert ok is True
        assert mock_post.call_args.kwargs["headers"]["Priority"] == "low"
