"""Read-only host and Minecraft process monitoring for the dashboard."""
import asyncio
from collections import deque
from datetime import datetime, timezone
import logging

import psutil

from mcpanel.core import storage
from mcpanel.core.monitoring_host import _collect_sample, _history_sample

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 3
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
_sampler_task: asyncio.Task | None = None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


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


async def start_monitoring() -> None:
    global _latest, _sampler_task
    if _sampler_task and not _sampler_task.done():
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
    _sampler_task = asyncio.create_task(_sampler_loop(), name="host-monitor-sampler")


async def stop_monitoring() -> None:
    task = _sampler_task
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def get_stats() -> dict:
    sample = _latest or {
        "timestamp": None,
        "host": None,
        "disks": [],
        "servers": [],
    }
    profile_sizes, sizes_timestamp = storage.profile_sizes_snapshot()
    # Merge per-world storage stats (chunk counts, sizes, last modified) into
    # each running server entry so the monitoring tab can render per-world
    # breakdowns without an extra endpoint round-trip.
    sizes_by_id = {item["profile_id"]: item for item in profile_sizes}
    enriched_servers = []
    for server in sample.get("servers", []):
        enriched = dict(server)
        size_info = sizes_by_id.get(server["profile_id"], {})
        enriched["worlds"] = size_info.get("world_folders", {})
        enriched_servers.append(enriched)
    return {
        **sample,
        "servers": enriched_servers,
        "profile_sizes": profile_sizes,
        "profile_sizes_timestamp": sizes_timestamp,
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
