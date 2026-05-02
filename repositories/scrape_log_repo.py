"""Scrape log repository — file-based CRUD for scrape execution logs."""

from __future__ import annotations

from datetime import datetime

from repositories.base import BaseRepository
from log_exporter import export_search_logs, import_search_logs
from log_storage import (
    allocate_log_id,
    append_entry,
    find_entry,
    find_entry_any,
    read_entries,
    read_raw_log,
    write_raw_log,
)


class ScrapeLogRepository(BaseRepository):
    """Scrape log CRUD operations (file storage)."""

    def create_scrape_log(self, search_id: int, status: str, listings_found: int = 0, new_listings: int = 0, error_message: str = "", details: dict | None = None, started_at=None) -> int:
        now = started_at or datetime.utcnow()
        completed_at = datetime.utcnow()
        duration = (completed_at - now).total_seconds() if started_at else 0
        log_id = allocate_log_id()
        entry = {
            "id": log_id,
            "search_id": search_id,
            "started_at": now,
            "completed_at": completed_at,
            "status": status,
            "listings_found": listings_found,
            "new_listings": new_listings,
            "error_message": error_message,
            "details": details or {},
            "duration_sec": duration,
            "raw_logs_file": f"raw/{log_id}.log",
        }
        append_entry(search_id, entry)
        return log_id

    def get_scrape_logs(self, search_id: int, limit: int = 50, offset: int = 0, status_filter: str = "") -> list[dict]:
        logs = read_entries(search_id)
        if status_filter:
            logs = [log for log in logs if log.get("status") == status_filter]
        logs.sort(key=lambda log: log.get("started_at") or datetime.min, reverse=True)
        return logs[offset: offset + limit]

    def count_scrape_logs(self, search_id: int, status_filter: str = "") -> int:
        logs = read_entries(search_id)
        if status_filter:
            logs = [log for log in logs if log.get("status") == status_filter]
        return len(logs)

    def get_scrape_stats(self, search_id: int) -> dict:
        logs = read_entries(search_id)
        total = len(logs)
        success_count = sum(1 for log in logs if log.get("status") == "success")
        error_count = sum(1 for log in logs if log.get("status") == "error")
        avg_listings = sum(log.get("listings_found") or 0 for log in logs) / total if total else 0
        avg_new = sum(log.get("new_listings") or 0 for log in logs) / total if total else 0
        avg_duration = sum(log.get("duration_sec") or 0 for log in logs) / total if total else 0
        last_scrape = None
        if logs:
            last_scrape = max(logs, key=lambda log: log.get("started_at") or datetime.min)
            last_scrape = {
                "status": last_scrape.get("status"),
                "started_at": last_scrape.get("started_at"),
                "error_message": last_scrape.get("error_message"),
            }
        return {
            "total": total,
            "success_count": success_count,
            "error_count": error_count,
            "avg_listings": avg_listings,
            "avg_new": avg_new,
            "avg_duration": avg_duration,
            "last_scrape": last_scrape,
        }

    def update_scrape_log_raw(self, log_id: int, raw_logs: str) -> bool:
        found = find_entry_any(log_id)
        if not found:
            return False
        search_id, _ = found
        return write_raw_log(search_id, log_id, raw_logs)

    def get_scrape_log_raw(self, log_id: int, user_id: int | None = None) -> dict | None:
        found = find_entry_any(log_id)
        if not found:
            return None
        search_id, entry = found
        if user_id is not None:
            conn = self._get_conn_for_request()
            try:
                with self._dict_cursor(conn) as cur:
                    cur.execute(
                        "SELECT id FROM searches WHERE id = %s AND user_id = %s",
                        (search_id, user_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
            finally:
                self._release_conn(conn)
        entry = dict(entry)
        entry["raw_logs"] = read_raw_log(search_id, log_id)
        return entry

    def get_latest_scrape_log_id(self, search_id: int) -> int | None:
        logs = read_entries(search_id)
        if not logs:
            return None
        latest = max(logs, key=lambda log: log.get("started_at") or datetime.min)
        return latest.get("id")

    def export_scrape_logs(self, search_id: int) -> str:
        return export_search_logs(search_id)

    def import_scrape_logs(self, search_id: int, zip_path: str, allow_override: bool = False, performed_by: str = "") -> dict:
        return import_search_logs(search_id, zip_path, allow_override, performed_by)
