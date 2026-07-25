"""Tests for scrape_logs/storage.py (file-based scrape log storage)."""
import os
from datetime import datetime, timedelta

import scrape_logs.storage as log_storage


def _use_tmp_scrape_logs_dir(monkeypatch, tmp_path):
    scrape_logs_dir = tmp_path / "scrape_logs"
    monkeypatch.setattr(log_storage, "SCRAPE_LOGS_DIR", str(scrape_logs_dir))
    monkeypatch.setattr(log_storage, "COUNTER_FILE", str(scrape_logs_dir / "counter.json"))
    return scrape_logs_dir


class TestAppendAndReadEntries:
    def test_round_trip(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        log_storage.append_entry(1, {"id": 1, "status": "success"})
        log_storage.append_entry(1, {"id": 2, "status": "error"})

        entries = log_storage.read_entries(1)
        assert [e["id"] for e in entries] == [1, 2]

    def test_read_entries_empty_when_no_file(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        assert log_storage.read_entries(999) == []

    def test_serializes_and_parses_datetime_fields(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        now = datetime(2026, 1, 1, 12, 0, 0)
        log_storage.append_entry(1, {"id": 1, "started_at": now})

        entries = log_storage.read_entries(1)
        assert entries[0]["started_at"] == now

    def test_corrupted_line_is_skipped_and_logged(self, tmp_path, monkeypatch, capsys):
        scrape_logs_dir = _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        log_storage.append_entry(1, {"id": 1})
        path = log_storage.get_metadata_path(1)
        with open(path, "a", encoding="utf-8") as f:
            f.write("not-valid-json\n")
        log_storage.append_entry(1, {"id": 2})

        entries = log_storage.read_entries(1)

        assert [e["id"] for e in entries] == [1, 2]


class TestWriteEntries:
    def test_overwrites_existing_entries(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        log_storage.append_entry(1, {"id": 1})
        log_storage.append_entry(1, {"id": 2})

        log_storage.write_entries(1, [{"id": 2}])

        entries = log_storage.read_entries(1)
        assert [e["id"] for e in entries] == [2]


class TestFindEntry:
    def test_find_entry_returns_matching_entry(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        log_storage.append_entry(1, {"id": 5, "status": "success"})

        entry = log_storage.find_entry(1, 5)
        assert entry["status"] == "success"

    def test_find_entry_returns_none_when_missing(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        assert log_storage.find_entry(1, 999) is None

    def test_find_entry_any_scans_all_search_dirs(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        log_storage.append_entry(1, {"id": 1})
        log_storage.append_entry(2, {"id": 2})

        result = log_storage.find_entry_any(2)
        assert result == (2, {"id": 2})

    def test_find_entry_any_returns_none_when_dir_missing(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        assert log_storage.find_entry_any(1) is None


class TestRawLogs:
    def test_write_and_read_raw_log_round_trip(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        ok = log_storage.write_raw_log(1, 5, "some raw log content")
        assert ok is True

        content = log_storage.read_raw_log(1, 5)
        assert content == "some raw log content"

    def test_read_raw_log_returns_empty_when_missing(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        assert log_storage.read_raw_log(1, 999) == ""


class TestDeleteSearchLogs:
    def test_removes_the_whole_search_directory(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        log_storage.append_entry(1, {"id": 1})
        log_storage.write_raw_log(1, 1, "raw content")
        search_dir = log_storage.get_search_dir(1)
        assert os.path.exists(search_dir)

        log_storage.delete_search_logs(1)

        assert not os.path.exists(search_dir)

    def test_no_op_when_search_dir_missing(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        log_storage.delete_search_logs(999)  # must not raise


class TestCleanupOldLogs:
    def test_keeps_recent_and_removes_old_entries(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        recent = datetime.utcnow()
        old = datetime.utcnow() - timedelta(days=10)

        log_storage.append_entry(1, {"id": 1, "completed_at": recent})
        log_storage.append_entry(1, {"id": 2, "completed_at": old})
        log_storage.write_raw_log(1, 2, "old raw log")

        deleted = log_storage.cleanup_old_logs(retention_days=5)

        assert deleted == 1
        remaining_ids = [e["id"] for e in log_storage.read_entries(1)]
        assert remaining_ids == [1]
        assert log_storage.read_raw_log(1, 2) == ""

    def test_returns_zero_when_dir_missing(self, tmp_path, monkeypatch):
        _use_tmp_scrape_logs_dir(monkeypatch, tmp_path)
        assert log_storage.cleanup_old_logs() == 0
