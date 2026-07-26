"""Search log manager — captures loguru output per search.

Each scrape creates a log file at logs/search_<id>.log.
All loguru messages are duplicated to this file during the scrape.
Files older than 5 days are automatically cleaned up.
"""

from __future__ import annotations

import glob
import os
import time

from loguru import logger

from scrape_logs.storage import cleanup_old_logs as cleanup_scrape_logs

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(_REPO_ROOT, "logs")
RETENTION_DAYS = 5


class SearchLogManager:
    """Captures all loguru output to a per-search log file."""

    def __init__(self, search_id: int):
        self.search_id = search_id
        self.log_file = os.path.join(LOGS_DIR, f"search_{search_id}.log")
        self._handler_id = None

    def start(self) -> None:
        """Start capturing loguru output to the search log file."""
        os.makedirs(LOGS_DIR, exist_ok=True)
        self._handler_id = logger.add(
            self.log_file,
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {module}:{function} | {message}",
            rotation="10 MB",
            retention=f"{RETENTION_DAYS} days",
            mode="a",
        )
        logger.info(f"[LogManager] Capture démarrée pour search_id={self.search_id} → {self.log_file}")

    def stop(self) -> str:
        """Stop capturing and return the full log content."""
        if self._handler_id:
            logger.remove(self._handler_id)
            self._handler_id = None
        logger.info(f"[LogManager] Capture arrêtée pour search_id={self.search_id}")
        return self.read_all()

    def read_all(self) -> str:
        """Read the entire log file."""
        if not os.path.exists(self.log_file):
            return ""
        with open(self.log_file, encoding="utf-8", errors="replace") as f:
            return f.read()

    def read_tail(self, offset: int = 0) -> tuple[str, int]:
        """Read new log lines since offset. Returns (new_text, new_offset)."""
        if not os.path.exists(self.log_file):
            return "", 0
        with open(self.log_file, encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            new_text = f.read()
            return new_text, f.tell()

    def file_exists(self) -> bool:
        return os.path.exists(self.log_file)

    @staticmethod
    def cleanup_old_logs() -> int:
        """Delete log files older than RETENTION_DAYS. Returns count."""
        if not os.path.exists(LOGS_DIR):
            return 0
        cutoff = time.time() - (RETENTION_DAYS * 86400)
        deleted = 0
        for fpath in glob.glob(os.path.join(LOGS_DIR, "search_*.log")):
            if os.path.getmtime(fpath) < cutoff:
                try:
                    os.remove(fpath)
                    deleted += 1
                except OSError:
                    pass
        if deleted:
            logger.info(f"[LogManager] {deleted} anciens fichiers de log supprimés")
        cleanup_scrape_logs(RETENTION_DAYS)
        return deleted
