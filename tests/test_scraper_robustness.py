"""Tests for scraper/seloger.py network robustness helpers (no real network calls)."""
from unittest.mock import MagicMock, patch

from scraper.seloger import _post_with_429_retry, _try_with_proxies


class TestPostWith429Retry:
    def test_returns_immediately_on_success(self):
        session = MagicMock()
        ok_resp = MagicMock(status_code=200)
        session.post.return_value = ok_resp

        resp = _post_with_429_retry(session, "http://example.com", {})

        assert resp is ok_resp
        session.post.assert_called_once()

    def test_retries_on_429_then_succeeds(self):
        session = MagicMock()
        throttled = MagicMock(status_code=429, headers={"Retry-After": "0"})
        ok_resp = MagicMock(status_code=200)
        session.post.side_effect = [throttled, ok_resp]

        with patch("scraper.seloger.time.sleep"):
            resp = _post_with_429_retry(session, "http://example.com", {}, max_retries=3)

        assert resp is ok_resp
        assert session.post.call_count == 2

    def test_gives_up_after_max_retries(self):
        session = MagicMock()
        throttled = MagicMock(status_code=429, headers={"Retry-After": "0"})
        session.post.return_value = throttled

        with patch("scraper.seloger.time.sleep"):
            resp = _post_with_429_retry(session, "http://example.com", {}, max_retries=2)

        assert resp is throttled
        assert session.post.call_count == 3  # initial + 2 retries

    def test_invalid_retry_after_header_falls_back_to_default(self):
        session = MagicMock()
        throttled = MagicMock(status_code=429, headers={"Retry-After": "not-a-number"})
        ok_resp = MagicMock(status_code=200)
        session.post.side_effect = [throttled, ok_resp]

        with patch("scraper.seloger.time.sleep") as mock_sleep:
            resp = _post_with_429_retry(session, "http://example.com", {}, max_retries=3)

        assert resp is ok_resp
        mock_sleep.assert_called_once_with(5)


class TestTryWithProxiesDeadline:
    def test_gives_up_after_deadline_without_hanging(self):
        """Even if every proxy test would block indefinitely, the deadline
        must bound the total wait so the single-worker scrape queue can't
        be stalled for hours."""
        def never_returns(proxy):
            import threading
            threading.Event().wait()  # blocks forever, simulating a dead proxy

        with patch("scraper.seloger._get_free_proxies", return_value=["1.2.3.4:8080"] * 5):
            with patch("scraper.seloger.ThreadPoolExecutor") as MockExecutor:
                mock_executor = MockExecutor.return_value.__enter__.return_value
                pending_future = MagicMock()
                mock_executor.submit.return_value = pending_future
                with patch("scraper.seloger.as_completed", side_effect=TimeoutError):
                    resp = _try_with_proxies("http://example.com", max_proxies=5, deadline_seconds=0.01)

        assert resp is None
        pending_future.cancel.assert_called()
