"""Tests for scrape_logs/exporter.py (zip export/import of scrape logs)."""
import json
import os
import zipfile
from datetime import datetime

import pytest

import scrape_logs.exporter as log_exporter
import scrape_logs.storage as log_storage


def _patch_dirs(monkeypatch, tmp_path):
    scrape_logs_dir = str(tmp_path / "scrape_logs")
    exports_dir = str(tmp_path / "scrape_logs" / "exports")
    counter_file = str(tmp_path / "scrape_logs" / "counter.json")
    import_log_file = str(tmp_path / "scrape_logs" / "imports.log")

    monkeypatch.setattr(log_storage, "SCRAPE_LOGS_DIR", scrape_logs_dir)
    monkeypatch.setattr(log_storage, "EXPORTS_DIR", exports_dir)
    monkeypatch.setattr(log_storage, "COUNTER_FILE", counter_file)

    monkeypatch.setattr(log_exporter, "SCRAPE_LOGS_DIR", scrape_logs_dir)
    monkeypatch.setattr(log_exporter, "EXPORTS_DIR", exports_dir)
    monkeypatch.setattr(log_exporter, "IMPORT_LOG_FILE", import_log_file)


class TestExportSearchLogs:
    def test_zip_contains_metadata_summary_and_raw_logs(self, tmp_path, monkeypatch):
        _patch_dirs(monkeypatch, tmp_path)
        log_storage.append_entry(1, {
            "id": 1, "status": "success", "listings_found": 5, "new_listings": 2,
            "duration_sec": 1.5,
        })
        log_storage.write_raw_log(1, 1, "raw log content")

        zip_path = log_exporter.export_search_logs(1)

        assert os.path.exists(zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            assert "metadata.json" in names
            assert "summary.json" in names
            assert "raw_logs/1.log" in names

            metadata = json.loads(zf.read("metadata.json"))
            assert metadata[0]["id"] == 1

            summary = json.loads(zf.read("summary.json"))
            assert summary["search_id"] == 1
            assert summary["total"] == 1
            assert summary["success_count"] == 1

    def test_empty_search_produces_zero_summary(self, tmp_path, monkeypatch):
        _patch_dirs(monkeypatch, tmp_path)
        zip_path = log_exporter.export_search_logs(999)

        with zipfile.ZipFile(zip_path) as zf:
            summary = json.loads(zf.read("summary.json"))
            assert summary["total"] == 0
            assert summary["avg_listings"] == 0


class TestImportSearchLogs:
    def test_imports_entries_and_raw_logs_for_matching_search(self, tmp_path, monkeypatch):
        _patch_dirs(monkeypatch, tmp_path)
        log_storage.append_entry(1, {"id": 1, "search_id": 1, "status": "success"})
        log_storage.write_raw_log(1, 1, "original raw")
        zip_path = log_exporter.export_search_logs(1)
        log_storage.delete_search_logs(1)

        result = log_exporter.import_search_logs(1, zip_path)

        assert result["imported"] == 1
        assert result["skipped"] == 0
        entries = log_storage.read_entries(1)
        assert entries[0]["id"] == 1
        assert log_storage.read_raw_log(1, 1) == "original raw"

    def test_skips_already_present_entries(self, tmp_path, monkeypatch):
        _patch_dirs(monkeypatch, tmp_path)
        log_storage.append_entry(1, {"id": 1, "search_id": 1, "status": "success"})
        zip_path = log_exporter.export_search_logs(1)

        result = log_exporter.import_search_logs(1, zip_path)

        assert result["imported"] == 0
        assert result["skipped"] == 1

    def test_raises_value_error_on_missing_metadata(self, tmp_path, monkeypatch):
        _patch_dirs(monkeypatch, tmp_path)
        bad_zip = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("something_else.json", "{}")

        with pytest.raises(ValueError, match="metadata.json manquant"):
            log_exporter.import_search_logs(1, str(bad_zip))

    def test_requires_override_when_search_id_mismatches(self, tmp_path, monkeypatch):
        _patch_dirs(monkeypatch, tmp_path)
        log_storage.append_entry(2, {"id": 1, "search_id": 2, "status": "success"})
        zip_path = log_exporter.export_search_logs(2)

        with pytest.raises(ValueError, match="override_required"):
            log_exporter.import_search_logs(1, zip_path, allow_override=False)

        result = log_exporter.import_search_logs(1, zip_path, allow_override=True)
        assert result["imported"] == 1

    def test_writes_audit_log_entry(self, tmp_path, monkeypatch):
        _patch_dirs(monkeypatch, tmp_path)
        log_storage.append_entry(1, {"id": 1, "search_id": 1, "status": "success"})
        zip_path = log_exporter.export_search_logs(1)
        log_storage.delete_search_logs(1)

        log_exporter.import_search_logs(1, zip_path, performed_by="tester")

        with open(log_exporter.IMPORT_LOG_FILE, "r") as f:
            content = f.read()
        assert "search_id=1" in content
        assert "by=tester" in content
