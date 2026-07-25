"""Tests for scraper/seloger.py network robustness helpers (no real network calls)."""
from unittest.mock import MagicMock, patch

from scraper.seloger import _try_with_proxies


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
