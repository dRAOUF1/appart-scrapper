"""Export/import helpers for file-based scrape logs."""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime

from loguru import logger

from scrape_logs.storage import (
    EXPORTS_DIR,
    SCRAPE_LOGS_DIR,
    allocate_log_id,
    append_entry,
    find_entry_any,
    get_raw_path,
    read_entries,
    write_raw_log,
)

IMPORT_LOG_FILE = os.path.join(SCRAPE_LOGS_DIR, "imports.log")


def _serialize_entry(entry: dict) -> dict:
    data = dict(entry)
    for key in ("started_at", "completed_at"):
        value = data.get(key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _parse_dt(value):
    if isinstance(value, datetime) or value is None:
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def export_search_logs(search_id: int) -> str:
    entries = read_entries(search_id)
    _ensure_dir(EXPORTS_DIR)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    zip_name = f"search_{search_id}_{timestamp}.zip"
    zip_path = os.path.join(EXPORTS_DIR, zip_name)

    total = len(entries)
    success_count = sum(1 for log in entries if log.get("status") == "success")
    error_count = sum(1 for log in entries if log.get("status") == "error")
    empty_count = sum(1 for log in entries if log.get("status") == "empty")
    avg_listings = sum(log.get("listings_found") or 0 for log in entries) / total if total else 0
    avg_new = sum(log.get("new_listings") or 0 for log in entries) / total if total else 0
    avg_duration = sum(log.get("duration_sec") or 0 for log in entries) / total if total else 0

    summary = {
        "search_id": search_id,
        "exported_at": datetime.utcnow().isoformat(),
        "total": total,
        "success_count": success_count,
        "error_count": error_count,
        "empty_count": empty_count,
        "avg_listings": avg_listings,
        "avg_new": avg_new,
        "avg_duration": avg_duration,
        "format_version": 1,
    }

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        metadata_payload = json.dumps([_serialize_entry(e) for e in entries], indent=2)
        zf.writestr("metadata.json", metadata_payload)
        zf.writestr("summary.json", json.dumps(summary, indent=2))
        for entry in entries:
            log_id = entry.get("id")
            if log_id is None:
                continue
            raw_path = get_raw_path(search_id, log_id)
            if os.path.exists(raw_path):
                zf.write(raw_path, arcname=f"raw_logs/{log_id}.log")

    return zip_path


def import_search_logs(search_id: int, zip_path: str, allow_override: bool = False, performed_by: str = "") -> dict:
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        if "metadata.json" not in names:
            raise ValueError("Archive invalide: metadata.json manquant")
        try:
            metadata = json.loads(zf.read("metadata.json").decode("utf-8"))
        except Exception as e:
            raise ValueError(f"metadata.json invalide: {e}")

        if not isinstance(metadata, list):
            raise ValueError("metadata.json invalide: format attendu liste")

        source_search_id = None
        if metadata:
            source_search_id = metadata[0].get("search_id")
        if source_search_id is not None and source_search_id != search_id:
            if not allow_override:
                raise ValueError("override_required")

        imported = 0
        skipped = 0
        remapped = 0

        existing = {entry.get("id") for entry in read_entries(search_id)}

        for entry in metadata:
            if not isinstance(entry, dict):
                continue
            original_id = entry.get("id")
            if original_id is None:
                continue
            if original_id in existing:
                skipped += 1
                continue

            found = find_entry_any(original_id)
            new_id = original_id
            if found and found[0] != search_id:
                new_id = allocate_log_id()
                remapped += 1

            entry = dict(entry)
            entry["id"] = new_id
            entry["search_id"] = search_id
            entry["started_at"] = _parse_dt(entry.get("started_at"))
            entry["completed_at"] = _parse_dt(entry.get("completed_at"))
            entry["raw_logs_file"] = f"raw/{new_id}.log"

            raw_name = f"raw_logs/{original_id}.log"
            raw_text = ""
            if raw_name in names:
                try:
                    raw_text = zf.read(raw_name).decode("utf-8", errors="replace")
                except Exception:
                    raw_text = ""

            append_entry(search_id, entry)
            write_raw_log(search_id, new_id, raw_text)
            existing.add(new_id)
            imported += 1

    _append_import_audit(
        search_id=search_id,
        imported=imported,
        skipped=skipped,
        remapped=remapped,
        allow_override=allow_override,
        performed_by=performed_by,
    )
    return {
        "imported": imported,
        "skipped": skipped,
        "remapped": remapped,
    }


def _append_import_audit(search_id: int, imported: int, skipped: int, remapped: int, allow_override: bool, performed_by: str) -> None:
    _ensure_dir(SCRAPE_LOGS_DIR)
    timestamp = datetime.utcnow().isoformat()
    line = (
        f"{timestamp} | search_id={search_id} | imported={imported} | skipped={skipped} | "
        f"remapped={remapped} | override={allow_override} | by={performed_by}\n"
    )
    try:
        with open(IMPORT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        logger.warning(f"[LogImport] Failed to write audit log: {e}")
