"""Tests for scrape_logs/manager.py (per-search loguru capture)."""
import os
import time
from unittest.mock import patch

from loguru import logger

import scrape_logs.manager as log_manager
from scrape_logs.manager import SearchLogManager


class TestStartStop:
    def test_captures_messages_logged_between_start_and_stop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_manager, "LOGS_DIR", str(tmp_path))
        mgr = SearchLogManager(search_id=1)

        mgr.start()
        logger.info("hello from test")
        content = mgr.stop()

        assert "hello from test" in content
        assert mgr.file_exists()

    def test_log_file_path_uses_search_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_manager, "LOGS_DIR", str(tmp_path))
        mgr = SearchLogManager(search_id=42)
        assert mgr.log_file == os.path.join(str(tmp_path), "search_42.log")

    def test_stop_removes_handler_so_later_logs_are_not_captured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_manager, "LOGS_DIR", str(tmp_path))
        mgr = SearchLogManager(search_id=2)

        mgr.start()
        logger.info("captured")
        mgr.stop()
        logger.info("not captured")

        content = mgr.read_all()
        assert "captured" in content
        assert "not captured" not in content


class TestReadAll:
    def test_returns_empty_string_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_manager, "LOGS_DIR", str(tmp_path))
        mgr = SearchLogManager(search_id=3)
        assert mgr.read_all() == ""


class TestReadTail:
    def test_returns_only_new_content_since_offset(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_manager, "LOGS_DIR", str(tmp_path))
        mgr = SearchLogManager(search_id=4)
        os.makedirs(tmp_path, exist_ok=True)
        with open(mgr.log_file, "w") as f:
            f.write("line1\n")

        text1, offset1 = mgr.read_tail(0)
        assert text1 == "line1\n"

        with open(mgr.log_file, "a") as f:
            f.write("line2\n")

        text2, offset2 = mgr.read_tail(offset1)
        assert text2 == "line2\n"

    def test_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_manager, "LOGS_DIR", str(tmp_path))
        mgr = SearchLogManager(search_id=5)
        text, offset = mgr.read_tail(0)
        assert text == ""
        assert offset == 0


class TestFileExists:
    def test_false_before_start(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_manager, "LOGS_DIR", str(tmp_path))
        mgr = SearchLogManager(search_id=6)
        assert mgr.file_exists() is False


class TestCleanupOldLogs:
    def test_deletes_files_older_than_retention_and_keeps_recent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_manager, "LOGS_DIR", str(tmp_path))
        old_file = tmp_path / "search_100.log"
        recent_file = tmp_path / "search_200.log"
        old_file.write_text("old")
        recent_file.write_text("recent")

        old_time = time.time() - (log_manager.RETENTION_DAYS + 1) * 86400
        os.utime(old_file, (old_time, old_time))

        with patch("scrape_logs.manager.cleanup_scrape_logs") as mock_cleanup:
            deleted = SearchLogManager.cleanup_old_logs()

        assert deleted == 1
        assert not old_file.exists()
        assert recent_file.exists()
        mock_cleanup.assert_called_once_with(log_manager.RETENTION_DAYS)

    def test_returns_zero_when_logs_dir_missing(self, tmp_path, monkeypatch):
        missing_dir = str(tmp_path / "does_not_exist")
        monkeypatch.setattr(log_manager, "LOGS_DIR", missing_dir)

        with patch("scrape_logs.manager.cleanup_scrape_logs"):
            deleted = SearchLogManager.cleanup_old_logs()

        assert deleted == 0
