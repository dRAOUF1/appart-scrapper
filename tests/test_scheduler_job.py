"""Tests for the due-for-scrape filtering logic inside
main.py::_start_background_tasks's scheduled_scrape_job closure.

The closure can't be imported directly (it's nested), so we capture it via
the mocked BackgroundScheduler.add_job() call and invoke it directly with a
controlled app/storage mock.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from main import _start_background_tasks


def _get_scheduled_job(app):
    with patch("main._try_acquire_scheduler_lock", return_value=MagicMock()), \
         patch("apscheduler.schedulers.background.BackgroundScheduler") as MockScheduler:
        mock_scheduler = MockScheduler.return_value
        _start_background_tasks(app)
    return mock_scheduler.add_job.call_args.args[0]


def _search(**overrides):
    base = {
        "id": 1,
        "is_active": True,
        "criteria": {"placeIds": ["123"]},
        "scrape_interval": 5,
        "last_scraped": None,
    }
    base.update(overrides)
    return base


def _run_job_with_search(search):
    app = MagicMock()
    app.storage.users.get_all_users.return_value = [{"id": 1}]
    app.storage.searches.get_user_searches.return_value = [search]

    with patch("core.scrape_control.submit_scrape") as mock_submit:
        job_fn = _get_scheduled_job(app)
        job_fn()
    return mock_submit


class TestSchedulerLockNotAcquired:
    def test_scheduler_not_started_when_lock_unavailable(self):
        app = MagicMock()
        with patch("main._try_acquire_scheduler_lock", return_value=None), \
             patch("apscheduler.schedulers.background.BackgroundScheduler") as MockScheduler:
            _start_background_tasks(app)
        MockScheduler.return_value.add_job.assert_not_called()
        MockScheduler.return_value.start.assert_not_called()


class TestDueForScrapeFiltering:
    def test_inactive_search_is_not_submitted(self):
        mock_submit = _run_job_with_search(_search(is_active=False))
        mock_submit.assert_not_called()

    def test_empty_criteria_is_not_submitted(self):
        mock_submit = _run_job_with_search(_search(criteria={}))
        mock_submit.assert_not_called()

    def test_non_dict_criteria_is_not_submitted(self):
        mock_submit = _run_job_with_search(_search(criteria=["not", "a", "dict"]))
        mock_submit.assert_not_called()

    def test_criteria_without_place_ids_is_not_submitted(self):
        mock_submit = _run_job_with_search(_search(criteria={"priceMax": 1000}))
        mock_submit.assert_not_called()

    def test_recently_scraped_within_interval_is_not_submitted(self):
        recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        mock_submit = _run_job_with_search(_search(scrape_interval=5, last_scraped=recent))
        mock_submit.assert_not_called()

    def test_scraped_past_interval_is_submitted(self):
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
        mock_submit = _run_job_with_search(_search(scrape_interval=5, last_scraped=old))
        mock_submit.assert_called_once()
        args = mock_submit.call_args.args
        assert args[1] == 1  # search_id
        assert args[2] == 1  # user_id

    def test_never_scraped_is_submitted(self):
        mock_submit = _run_job_with_search(_search(last_scraped=None))
        mock_submit.assert_called_once()

    def test_last_scraped_as_iso_string_is_parsed(self):
        old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)).isoformat()
        mock_submit = _run_job_with_search(_search(scrape_interval=5, last_scraped=old))
        mock_submit.assert_called_once()
