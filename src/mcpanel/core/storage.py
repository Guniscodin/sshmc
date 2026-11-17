"""Background storage inventory for panel-managed persistent data."""

import asyncio
import logging
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from mcpanel import db
from mcpanel.config import (
    BACKUPS_ROOT, CRASH_REPORT_KEEP, CRASH_REPORT_RETENTION_DAYS, DATA_DIR,
    PANEL_LOG_BACKUP_COUNT, PANEL_LOG_MAX_BYTES, PROFILES_DIR,
    SERVER_LOG_BACKUP_COUNT, SERVER_LOG_MAX_BYTES, SERVER_ROTATED_LOG_KEEP,
    UPLOAD_SESSION_MAX_AGE_SECONDS,
)

logger = logging.getLogger(__name__)
SCAN_INTERVAL_SECONDS = 60
WORLD_FOLDERS = ("world", "world_nether", "world_the_end")
_snapshot: dict = {"scanned_at": None, "scanning": True, "total_bytes": 0, "profiles": [], "modpacks": [], "panel_logs": []}
_refresh_event: asyncio.Event | None = None
_scan_task: asyncio.Task | None = None


def _empty_profile(profile_id: str, name: str, status: str = "stopped", orphaned: bool = False) -> dict:
    return {
        "id": profile_id, "name": name, "status": status, "orphaned": orphaned,
        "total_bytes": 0, "server_bytes": 0, "world_bytes": 0,
        "worlds": {name: 0 for name in WORLD_FOLDERS}, "server_logs_bytes": 0,
        "relay_logs_bytes": 0, "crash_reports_bytes": 0, "backups_bytes": 0,
        "download_cache_bytes": 0, "modrinth_cache_bytes": 0, "modrinth_manifest_bytes": 0,
        "repo_bytes": 0, "other_bytes": 0,
        "other_items": {}, "backups": [],
    }


def _profile_bucket(entry: dict, parts: tuple[str, ...], size: int):
    if len(parts) == 3 and parts[2] == "mods.json":
        entry["modrinth_manifest_bytes"] += size
        return
    if len(parts) < 3:
        entry["other_bytes"] += size
        entry["other_items"]["profile files"] = entry["other_items"].get("profile files", 0) + size
        return
    folder = parts[2]
    key = "other_bytes"
    if folder == "server":
        entry["server_bytes"] += size
        child = parts[3] if len(parts) > 3 else ""
        if child in WORLD_FOLDERS:
            entry["worlds"][child] += size
            entry["world_bytes"] += size
            key = None
        elif child == "logs":
            key = "server_logs_bytes"
        elif child == "crash-reports":
            key = "crash_reports_bytes"
    elif folder == "logs":
        key = "relay_logs_bytes"
    elif folder == "backups":
        key = "backups_bytes"
        if len(parts) == 4 and parts[-1].endswith(".tar.gz"):
            entry["backups"].append({"filename": parts[-1], "size_bytes": size})
    elif folder == "downloaded_mods":
        key = "download_cache_bytes"
    elif folder == "modrinth_mods":
        key = "modrinth_cache_bytes"
    elif folder == "repo":
        key = "repo_bytes"
    if key:
        entry[key] += size
    if key == "other_bytes":
        entry["other_items"][folder] = entry["other_items"].get(folder, 0) + size


def _scan_root(root: Path, inventory: dict, known_profiles: dict[str, dict]) -> int:
    total = 0
    if not root.is_dir() or root.is_symlink():
        return total
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for item in entries:
                    try:
                        info = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(item.path))
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    size = info.st_size
                    path = Path(item.path)
                    try:
                        parts = path.relative_to(DATA_DIR).parts if root == DATA_DIR else ()
                    except ValueError:
                        parts = ()
                    if root == DATA_DIR:
                        total += size
                        if parts and parts[0] == "panel.db":
                            inventory["database_bytes"] += size
                        elif parts and parts[0] == "logs":
                            inventory["panel_logs_bytes"] += size
                            inventory["panel_logs"].append({"filename": path.name, "size_bytes": size})
                        elif parts and parts[0] == "uploads":
                            inventory["uploads_bytes"] += size
                        elif parts and parts[0] == "modpacks":
                            inventory["modpacks_bytes"] += size
                            if len(parts) > 1 and not parts[1].startswith("."):
                                pack = inventory["modpack_map"].setdefault(parts[1], {"name": parts[1], "size_bytes": 0, "jar_count": 0})
                                pack["size_bytes"] += size
                                if path.suffix.lower() == ".jar":
                                    pack["jar_count"] += 1
                        elif parts and parts[0] == "profiles" and len(parts) > 1:
                            profile = known_profiles.get(parts[1])
                            if not profile and re.fullmatch(r"[0-9a-f]{32}", parts[1]):
                                profile = inventory["orphan_profiles"].setdefault(
                                    parts[1],
                                    _empty_profile(parts[1], f"Unlinked profile files ({parts[1][:8]})", "orphaned", True),
                                )
                            if profile:
                                profile["total_bytes"] += size
                                inventory["profiles_bytes"] += size
                                _profile_bucket(profile, parts, size)
                            else:
                                inventory["other_items"][parts[0]] = inventory["other_items"].get(parts[0], 0) + size
                        elif parts:
                            inventory["other_items"][parts[0]] = inventory["other_items"].get(parts[0], 0) + size
                    else:
                        inventory["external_backups_bytes"] += size
                        try:
                            external_parts = path.relative_to(BACKUPS_ROOT).parts
                        except ValueError:
                            external_parts = ()
                        if len(external_parts) > 1 and re.fullmatch(r"[0-9a-f]{32}", external_parts[0]):
                            profile_id = external_parts[0]
                            profile = known_profiles.get(profile_id) or inventory["orphan_profiles"].get(profile_id)
                            if not profile:
                                profile = inventory["orphan_profiles"].setdefault(
                                    profile_id,
                                    _empty_profile(profile_id, f"Unlinked profile files ({profile_id[:8]})", "orphaned", True),
                                )
                            profile["total_bytes"] += size
                            profile["backups_bytes"] += size
                            if len(external_parts) == 2 and path.name.endswith(".tar.gz"):
                                profile["backups"].append({"filename": path.name, "size_bytes": size})
        except OSError:
            continue
    return total


def _scan_storage() -> dict:
    profiles = {}
    for profile in db.list_profiles():
        entry = _empty_profile(profile.id, profile.name)
        state = db.get_runtime_state(profile.id)
        entry["status"] = state.status.value if state else "stopped"
        profiles[profile.id] = entry
    inventory = {
        "data_dir": str(DATA_DIR), "total_bytes": 0, "panel_logs_bytes": 0,
        "uploads_bytes": 0, "modpacks_bytes": 0, "profiles_bytes": 0, "database_bytes": 0,
        "external_backups_bytes": 0, "profiles": profiles,
        "orphan_profiles": {}, "modpack_map": {}, "panel_logs": [], "other_items": {},
    }
    inventory["total_bytes"] = _scan_root(DATA_DIR, inventory, profiles)
    try:
        BACKUPS_ROOT.resolve().relative_to(DATA_DIR.resolve())
        external = False
    except ValueError:
        external = True
    if external:
        _scan_root(BACKUPS_ROOT, inventory, profiles)
    for entry in (*profiles.values(), *inventory["orphan_profiles"].values()):
        entry["backups"].sort(key=lambda item: item["filename"], reverse=True)
    all_profiles = [*profiles.values(), *inventory.pop("orphan_profiles").values()]
    modpacks = sorted(inventory.pop("modpack_map").values(), key=lambda item: item["name"].casefold())
    inventory["other_bytes"] = max(
        0,
        inventory["total_bytes"] - inventory["panel_logs_bytes"] - inventory["uploads_bytes"]
        - inventory["modpacks_bytes"] - inventory["profiles_bytes"] - inventory["database_bytes"],
    )
    return {
        **inventory,
        "profiles": sorted(all_profiles, key=lambda item: item["name"].casefold()),
        "modpacks": modpacks,
        "combined_total_bytes": inventory["total_bytes"] + inventory["external_backups_bytes"],
        "other_items": [
            {"name": name, "size_bytes": size}
            for name, size in sorted(inventory["other_items"].items(), key=lambda item: item[1], reverse=True)
        ],
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "scanning": False,
        "external_backup_root": str(BACKUPS_ROOT) if external else None,
        "policies": {
            "panel_log_max_bytes": PANEL_LOG_MAX_BYTES,
            "panel_log_rotations": PANEL_LOG_BACKUP_COUNT,
            "server_log_max_bytes": SERVER_LOG_MAX_BYTES,
            "server_log_rotations": SERVER_LOG_BACKUP_COUNT,
            "server_rotated_logs_kept": SERVER_ROTATED_LOG_KEEP,
            "crash_reports_keep_newest": CRASH_REPORT_KEEP,
            "crash_reports_min_age_days": CRASH_REPORT_RETENTION_DAYS,
            "upload_expiry_seconds": UPLOAD_SESSION_MAX_AGE_SECONDS,
            "backups_unbounded": True,
            "worlds_unbounded": True,
            "download_cache_unbounded": True,
            "modpacks_unbounded": True,
            "modrinth_cache_entries": 256,
            "modrinth_cache_ttl_seconds": 90,
            "install_jobs_retained": 256,
            "install_jobs_ttl_seconds": 3600,
        },
    }


async def _scan_loop():
    global _snapshot
    while True:
        try:
            _snapshot = await asyncio.to_thread(_scan_storage)
        except Exception:
            logger.exception("Could not scan panel storage")
        try:
            await asyncio.wait_for(_refresh_event.wait(), timeout=SCAN_INTERVAL_SECONDS)
            _refresh_event.clear()
        except asyncio.TimeoutError:
            pass


async def start_storage_monitoring():
    global _refresh_event, _scan_task
    if _scan_task and not _scan_task.done():
        return
    _refresh_event = asyncio.Event()
    _scan_task = asyncio.create_task(_scan_loop(), name="storage-inventory-sampler")


async def stop_storage_monitoring():
    global _scan_task
    if _scan_task:
        _scan_task.cancel()
        await asyncio.gather(_scan_task, return_exceptions=True)
        _scan_task = None


def get_storage_snapshot() -> dict:
    return _snapshot


def request_storage_scan():
    if _refresh_event:
        _refresh_event.set()
