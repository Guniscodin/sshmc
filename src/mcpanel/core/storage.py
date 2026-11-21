"""Background storage inventory for panel-managed persistent data."""

import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import stat
import struct

from mcpanel import db
from mcpanel.config import (
    BACKUPS_ROOT,
    CRASH_REPORT_KEEP,
    CRASH_REPORT_RETENTION_DAYS,
    DATA_DIR,
    PANEL_LOG_BACKUP_COUNT,
    PANEL_LOG_MAX_BYTES,
    PROFILES_DIR,
    SERVER_LOG_BACKUP_COUNT,
    SERVER_LOG_MAX_BYTES,
    SERVER_ROTATED_LOG_KEEP,
    UPLOAD_SESSION_MAX_AGE_SECONDS,
)

logger = logging.getLogger(__name__)
SCAN_INTERVAL_SECONDS = 600
WORLD_FOLDERS = ("world", "world_nether", "world_the_end")

REGION_HEADER_BYTES = 4096


def _count_chunks_in_region_file(region_path: Path) -> int:
    """Count generated chunks in a single .mca region file.

    The Anvil region format stores a 4096-byte chunk offset table at the start
    of the file: 1024 big-endian 32-bit offsets, one per (x, z) chunk in the
    region. An offset of 0 means the chunk has never been generated. Counting
    non-zero entries gives the exact number of generated chunks without parsing
    NBT, and reading just the header is cheap (8 KB per file).
    """
    try:
        with open(region_path, "rb") as f:
            header = f.read(REGION_HEADER_BYTES)
    except OSError:
        return 0
    if len(header) < REGION_HEADER_BYTES:
        return 0
    try:
        offsets = struct.unpack(">1024I", header)
    except struct.error:
        return 0
    return sum(1 for offset in offsets if offset != 0)


def _world_folder_stats(server_dir: Path) -> dict[str, dict]:
    """Compute per-world stats for every known world folder under a server dir.

    Returns a mapping of world_name -> {bytes, chunks, region_files,
    last_modified}. Worlds that don't exist on disk report zeros and a null
    timestamp.
    """
    stats: dict[str, dict] = {}
    for world_name in WORLD_FOLDERS:
        world_dir = server_dir / world_name
        entry: dict = {
            "bytes": 0,
            "chunks": 0,
            "region_files": 0,
            "last_modified": None,
        }
        if not world_dir.is_dir() or world_dir.is_symlink():
            stats[world_name] = entry
            continue

        total_bytes = 0
        last_mtime: float | None = None
        try:
            for path in world_dir.rglob("*"):
                try:
                    info = path.stat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                total_bytes += info.st_size
                if last_mtime is None or info.st_mtime > last_mtime:
                    last_mtime = info.st_mtime
        except OSError:
            pass

        region_files = 0
        chunks = 0
        region_dir = world_dir / "region"
        if region_dir.is_dir() and not region_dir.is_symlink():
            try:
                region_files_iter = region_dir.glob("*.mca")
            except OSError:
                region_files_iter = []
            for region_file in region_files_iter:
                try:
                    if not region_file.is_file() or region_file.is_symlink():
                        continue
                    region_files += 1
                    chunks += _count_chunks_in_region_file(region_file)
                except OSError:
                    continue

        entry["bytes"] = total_bytes
        entry["chunks"] = chunks
        entry["region_files"] = region_files
        if last_mtime is not None:
            entry["last_modified"] = datetime.fromtimestamp(last_mtime, tz=timezone.utc).isoformat()
        stats[world_name] = entry
    return stats

# Monitoring payload derived from the last storage scan: one shared walk,
# two consumers.
_sizes_snapshot: list[dict] = []
_sizes_timestamp: str | None = None
_snapshot: dict = {"scanned_at": None, "scanning": True, "total_bytes": 0, "profiles": [], "modpacks": [], "panel_logs": []}
_refresh_event: asyncio.Event | None = None
_scan_task: asyncio.Task | None = None


def _empty_profile(profile_id: str, name: str, status: str = "stopped", orphaned: bool = False) -> dict:
    return {
        "id": profile_id, "name": name, "status": status, "orphaned": orphaned,
        "total_bytes": 0, "server_bytes": 0, "world_bytes": 0,
        "worlds": {
            name: {"bytes": 0, "chunks": 0, "region_files": 0, "last_modified": None}
            for name in WORLD_FOLDERS
        },
        "server_logs_bytes": 0,
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
            # World breakdown (bytes/chunks/region_files/last_modified) is
            # computed in the post-pass via _world_folder_stats to avoid
            # double-walking world directories. Keep key None so world files
            # aren't double-counted as other_bytes.
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
    for profile, state in db.list_profiles_with_states():
        entry = _empty_profile(profile.id, profile.name)
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

    # Enrich per-profile world breakdowns (bytes/chunks/region_files/
    # last_modified) from the storage scan. This walks each world directory
    # once; monitoring consumes the result via profile_sizes_snapshot().
    for entry in profiles.values():
        server_dir = PROFILES_DIR / entry["id"] / "server"
        world_stats = _world_folder_stats(server_dir)
        entry["worlds"] = world_stats
        entry["world_bytes"] = sum(world["bytes"] for world in world_stats.values())

    all_profiles = [*profiles.values(), *inventory.pop("orphan_profiles").values()]
    modpacks = sorted(inventory.pop("modpack_map").values(), key=lambda item: item["name"].casefold())

    # Publish the monitoring-facing per-profile sizes; derived from this same
    # walk so monitoring never needs its own scan.
    scanned_at = datetime.now(timezone.utc).isoformat()
    global _sizes_snapshot, _sizes_timestamp
    _sizes_snapshot = [
        {
            "profile_id": entry["id"],
            "server_bytes": entry["server_bytes"],
            "world_bytes": entry["world_bytes"],
            "world_folders": dict(entry["worlds"]),
            "backups_bytes": entry["backups_bytes"],
        }
        for entry in profiles.values()
    ]
    _sizes_timestamp = scanned_at

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
        "scanned_at": scanned_at,
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


def profile_sizes_snapshot() -> tuple[list[dict], str | None]:
    """Per-profile sizes derived from the last storage scan (shared with monitoring)."""
    return ([dict(item) for item in _sizes_snapshot], _sizes_timestamp)


def request_storage_scan():
    if _refresh_event:
        _refresh_event.set()
