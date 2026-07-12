"""File-based storage for scrape logs."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Iterable

from loguru import logger

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(_REPO_ROOT, "logs")
SCRAPE_LOGS_DIR = os.path.join(LOGS_DIR, "scrape_logs")
EXPORTS_DIR = os.path.join(SCRAPE_LOGS_DIR, "exports")
COUNTER_FILE = os.path.join(SCRAPE_LOGS_DIR, "counter.json")
RETENTION_DAYS = 5

_COUNTER_LOCK = threading.Lock()
_LOCKS: dict[int, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _get_lock(search_id: int) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(search_id)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[search_id] = lock
        return lock


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def get_search_dir(search_id: int) -> str:
    return os.path.join(SCRAPE_LOGS_DIR, f"search_{search_id}")


def get_metadata_path(search_id: int) -> str:
    return os.path.join(get_search_dir(search_id), "metadata.jsonl")


def get_raw_dir(search_id: int) -> str:
    return os.path.join(get_search_dir(search_id), "raw")


def get_raw_path(search_id: int, log_id: int) -> str:
    return os.path.join(get_raw_dir(search_id), f"{log_id}.log")


def _serialize_entry(entry: dict) -> dict:
    data = dict(entry)
    for key in ("started_at", "completed_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _parse_entry(entry: dict) -> dict:
    data = dict(entry)
    for key in ("started_at", "completed_at"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                data[key] = datetime.fromisoformat(value)
            except ValueError:
                pass
    return data


def _read_counter() -> int:
    if not os.path.exists(COUNTER_FILE):
        return 0
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return int(payload.get("last_id", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _write_counter(value: int) -> None:
    _ensure_dir(SCRAPE_LOGS_DIR)
    tmp_path = f"{COUNTER_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"last_id": value}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, COUNTER_FILE)


def allocate_log_id() -> int:
    with _COUNTER_LOCK:
        current = _read_counter()
        next_id = current + 1
        _write_counter(next_id)
        return next_id


def append_entry(search_id: int, entry: dict) -> None:
    lock = _get_lock(search_id)
    with lock:
        _ensure_dir(get_search_dir(search_id))
        _ensure_dir(get_raw_dir(search_id))
        path = get_metadata_path(search_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_serialize_entry(entry)) + "\n")


def read_entries(search_id: int) -> list[dict]:
    lock = _get_lock(search_id)
    with lock:
        path = get_metadata_path(search_id)
        if not os.path.exists(path):
            return []
        entries = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(f"[LogStorage] Ligne corrompue ignorée dans {path}")
                    continue
                entries.append(_parse_entry(data))
        return entries


def write_entries(search_id: int, entries: Iterable[dict]) -> None:
    lock = _get_lock(search_id)
    with lock:
        _ensure_dir(get_search_dir(search_id))
        _ensure_dir(get_raw_dir(search_id))
        path = get_metadata_path(search_id)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(_serialize_entry(entry)) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)


def find_entry(search_id: int, log_id: int) -> dict | None:
    for entry in read_entries(search_id):
        if entry.get("id") == log_id:
            return entry
    return None


def find_entry_any(log_id: int) -> tuple[int, dict] | None:
    if not os.path.exists(SCRAPE_LOGS_DIR):
        return None
    for name in os.listdir(SCRAPE_LOGS_DIR):
        if not name.startswith("search_"):
            continue
        try:
            search_id = int(name.split("_")[-1])
        except ValueError:
            continue
        entry = find_entry(search_id, log_id)
        if entry:
            return search_id, entry
    return None


def write_raw_log(search_id: int, log_id: int, raw_logs: str) -> bool:
    lock = _get_lock(search_id)
    with lock:
        _ensure_dir(get_raw_dir(search_id))
        path = get_raw_path(search_id, log_id)
        try:
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(raw_logs)
            return True
        except OSError as e:
            logger.warning(f"[LogStorage] Échec écriture raw log {path}: {e}")
            return False


def read_raw_log(search_id: int, log_id: int) -> str:
    path = get_raw_path(search_id, log_id)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def delete_search_logs(search_id: int) -> None:
    search_dir = get_search_dir(search_id)
    if not os.path.exists(search_dir):
        return
    lock = _get_lock(search_id)
    with lock:
        for root, dirs, files in os.walk(search_dir, topdown=False):
            for fname in files:
                try:
                    os.remove(os.path.join(root, fname))
                except OSError as e:
                    logger.warning(f"[LogStorage] Échec suppression {os.path.join(root, fname)}: {e}")
            for dname in dirs:
                try:
                    os.rmdir(os.path.join(root, dname))
                except OSError as e:
                    logger.warning(f"[LogStorage] Échec suppression dossier {os.path.join(root, dname)}: {e}")
        try:
            os.rmdir(search_dir)
        except OSError as e:
            logger.warning(f"[LogStorage] Échec suppression dossier {search_dir}: {e}")


def cleanup_old_logs(retention_days: int = RETENTION_DAYS) -> int:
    if not os.path.exists(SCRAPE_LOGS_DIR):
        return 0
    cutoff = datetime.utcnow().timestamp() - (retention_days * 86400)
    deleted = 0
    for name in os.listdir(SCRAPE_LOGS_DIR):
        if not name.startswith("search_"):
            continue
        try:
            search_id = int(name.split("_")[-1])
        except ValueError:
            continue
        lock = _get_lock(search_id)
        with lock:
            entries = read_entries(search_id)
            kept = []
            removed_ids = []
            for entry in entries:
                dt = entry.get("completed_at") or entry.get("started_at")
                ts = dt.timestamp() if isinstance(dt, datetime) else None
                if ts is None or ts >= cutoff:
                    kept.append(entry)
                else:
                    removed_ids.append(entry.get("id"))
            for log_id in removed_ids:
                if log_id is None:
                    continue
                try:
                    os.remove(get_raw_path(search_id, log_id))
                    deleted += 1
                except OSError as e:
                    logger.warning(f"[LogStorage] Échec suppression raw log {search_id}/{log_id}: {e}")
            if kept != entries:
                write_entries(search_id, kept)
    if deleted:
        logger.info(f"[LogStorage] {deleted} anciens fichiers de log supprimés")
    return deleted
