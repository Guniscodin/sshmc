"""Read-only host and Minecraft process monitoring for the dashboard."""
import asyncio
import logging
import os
import stat
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import psutil

from mcpanel import db
from mcpanel.config import PROFILES_DIR
from mcpanel.models import ProfileStatus
from mcpanel.core import backups
from mcpanel.core.monitoring_host import _collect_sample, _history_sample

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 3
PROFILE_SIZE_INTERVAL_SECONDS = 60
HISTORY_SECONDS = 10 * 60
HISTORY_CAPACITY = HISTORY_SECONDS // SAMPLE_INTERVAL_SECONDS

# Keep host warning thresholds together so dashboard colors are easy to adjust.
RAM_WARNING_AVAILABLE_BYTES = 3 * 1024**3
RAM_CRITICAL_AVAILABLE_BYTES = int(1.5 * 1024**3)
DISK_WARNING_PERCENT = 80
DISK_CRITICAL_PERCENT = 90
WARNING_THRESHOLDS = {
    "ram_available_warning_bytes": RAM_WARNING_AVAILABLE_BYTES,
    "ram_available_critical_bytes": RAM_CRITICAL_AVAILABLE_BYTES,
    "disk_warning_percent": DISK_WARNING_PERCENT,
    "disk_critical_percent": DISK_CRITICAL_PERCENT,
}

_history: deque[dict] = deque(maxlen=HISTORY_CAPACITY)
_latest: dict | None = None
_profile_sizes: dict[str, dict] = {}
_profile_sizes_timestamp: str | None = None
_sampler_task: asyncio.Task | None = None
_profile_size_task: asyncio.Task | None = None
def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_size(path: Path) -> int:
    try:
        root_stat = path.lstat()
    except OSError:
        return 0
    if not stat.S_ISDIR(root_stat.st_mode):
        return root_stat.st_size if stat.S_ISREG(root_stat.st_mode) else 0
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(entry_stat.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(entry_stat.st_mode):
                        total += entry_stat.st_size
        except OSError:
            continue
    return total


def _server_tree_sizes(server_dir: Path, world_names: tuple[str, ...]) -> tuple[int, dict[str, int]]:
    totals = {name: 0 for name in world_names}
    server_total = 0
    try:
        root_stat = server_dir.lstat()
    except OSError:
        return server_total, totals
    if not stat.S_ISDIR(root_stat.st_mode):
        return server_total, totals
    pending = [(server_dir, None)]
    while pending:
        current, top_level = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(entry_stat.st_mode):
                        bucket = entry.name if top_level is None else top_level
                        pending.append((Path(entry.path), bucket))
                    elif stat.S_ISREG(entry_stat.st_mode):
                        server_total += entry_stat.st_size
                        if top_level in totals:
                            totals[top_level] += entry_stat.st_size
        except OSError:
            continue
    return server_total, totals


def _scan_profile_sizes() -> tuple[dict[str, dict], str]:
    sizes = {}
    for profile in db.list_profiles():
        profile_dir = PROFILES_DIR / profile.id
        server_dir = profile_dir / "server"
        server_bytes, world_folders = _server_tree_sizes(
            server_dir, ("world", "world_nether", "world_the_end")
        )
        sizes[profile.id] = {
            "profile_id": profile.id,
            "server_bytes": server_bytes,
            "world_bytes": sum(world_folders.values()),
            "world_folders": world_folders,
            "backups_bytes": _path_size(backups.backup_directory(profile.id)),
        }
    return sizes, _timestamp()


async def _sampler_loop() -> None:
    global _latest
    while True:
        await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
        try:
            sample = await asyncio.to_thread(_collect_sample)
        except Exception:
            logger.exception("Could not collect monitoring sample")
            continue
        _latest = sample
        _history.append(_history_sample(sample))


async def _profile_size_loop() -> None:
    global _profile_sizes, _profile_sizes_timestamp
    while True:
        try:
            sizes, timestamp = await asyncio.to_thread(_scan_profile_sizes)
            _profile_sizes = sizes
            _profile_sizes_timestamp = timestamp
        except Exception:
            logger.exception("Could not scan profile folder sizes")
        await asyncio.sleep(PROFILE_SIZE_INTERVAL_SECONDS)


async def start_monitoring() -> None:
    global _latest, _sampler_task, _profile_size_task
    sampler_running = _sampler_task is not None and not _sampler_task.done()
    size_task_running = _profile_size_task is not None and not _profile_size_task.done()
    if sampler_running and size_task_running:
        logger.warning("Monitoring is already running; start_monitoring() ignored a duplicate start.")
        return
    try:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
    except (OSError, psutil.Error):
        logger.exception("Could not prime CPU monitoring")
    await asyncio.sleep(0.1)
    try:
        _latest = await asyncio.to_thread(_collect_sample)
        _history.append(_history_sample(_latest))
    except Exception:
        logger.exception("Could not collect initial monitoring sample")
    if not sampler_running:
        _sampler_task = asyncio.create_task(_sampler_loop(), name="host-monitor-sampler")
    if not size_task_running:
        _profile_size_task = asyncio.create_task(_profile_size_loop(), name="profile-size-sampler")


async def stop_monitoring() -> None:
    tasks = [task for task in (_sampler_task, _profile_size_task) if task]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def get_stats() -> dict:
    sample = _latest or {
        "timestamp": None,
        "host": None,
        "disks": [],
        "servers": [],
    }
    return {
        **sample,
        "profile_sizes": list(_profile_sizes.values()),
        "profile_sizes_timestamp": _profile_sizes_timestamp,
        "thresholds": WARNING_THRESHOLDS,
    }


def get_history() -> dict:
    return {"samples": list(_history), "history_seconds": HISTORY_SECONDS}


def available_ram_bytes() -> int:
    if _latest and _latest.get("host"):
        return int(_latest["host"]["memory"]["available_bytes"])
    return int(psutil.virtual_memory().available)


def start_ram_warning(ram_mb: int) -> str | None:
    available = available_ram_bytes()
    requested = ram_mb * 1024**2
    if requested <= available:
        return None
    return (
        f"Warning: this profile allocates {ram_mb} MB, but the host currently has "
        f"about {available // 1024**2} MB available. Startup will continue."
    )
