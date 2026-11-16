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
from mcpanel.config import DATA_DIR, PROFILES_DIR
from mcpanel.models import ProfileStatus

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

_PSEUDO_FILESYSTEMS = {
    "autofs", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts",
    "devtmpfs", "efivarfs", "fusectl", "hugetlbfs", "mqueue", "nsfs", "proc",
    "pstore", "ramfs", "securityfs", "squashfs", "sysfs",
    "tmpfs", "tracefs", "overlay",
}
_history: deque[dict] = deque(maxlen=HISTORY_CAPACITY)
_latest: dict | None = None
_profile_sizes: dict[str, dict] = {}
_profile_sizes_timestamp: str | None = None
_sampler_task: asyncio.Task | None = None
_profile_size_task: asyncio.Task | None = None
_process_cache: dict[tuple[int, float], psutil.Process] = {}
_sample_process_keys: set[tuple[int, float]] = set()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


_EXCLUDED_MOUNT_ROOTS = ("/boot", "/proc", "/sys", "/run", "/snap", "/dev")


def _is_real_data_partition(partition) -> bool:
    device = partition.device
    mountpoint = os.path.abspath(partition.mountpoint)
    if not device.startswith("/dev/") or partition.fstype.strip().lower() in _PSEUDO_FILESYSTEMS:
        return False
    if any(_mount_contains(root, mountpoint) for root in _EXCLUDED_MOUNT_ROOTS):
        return False
    try:
        resolved_device = Path(device).resolve(strict=True)
        resolved_device.relative_to(Path("/dev"))
        if resolved_device.name.startswith("loop") or not stat.S_ISBLK(os.stat(device).st_mode):
            return False
    except (OSError, ValueError):
        return False
    return True


def _mount_contains(mountpoint: str, path: str) -> bool:
    try:
        return os.path.commonpath((os.path.abspath(mountpoint), os.path.abspath(path))) == os.path.abspath(mountpoint)
    except (ValueError, OSError):
        return False


def _disk_samples() -> list[dict]:
    data_path = str(DATA_DIR.resolve())
    try:
        partitions = psutil.disk_partitions(all=True)
    except (OSError, psutil.Error):
        partitions = []
    data_partition = max(
        (item for item in partitions
         if _is_real_data_partition(item) and _mount_contains(item.mountpoint, data_path)),
        key=lambda item: len(os.path.abspath(item.mountpoint)),
        default=None,
    )
    selected = []
    seen_mounts = set()
    if data_partition:
        selected.append((data_partition, True))
        seen_mounts.add(os.path.abspath(data_partition.mountpoint))
    for partition in partitions:
        mountpoint = os.path.abspath(partition.mountpoint)
        if mountpoint in seen_mounts or not _is_real_data_partition(partition):
            continue
        selected.append((partition, False))
        seen_mounts.add(mountpoint)

    disks = []
    for partition, is_data in selected:
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (OSError, psutil.Error):
            continue
        disks.append({
            "mountpoint": partition.mountpoint,
            "device": partition.device,
            "filesystem": partition.fstype,
            "is_data_filesystem": is_data,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "percent": usage.percent,
        })

    disks.sort(key=lambda item: (not item["is_data_filesystem"], item["mountpoint"]))
    return disks


def _server_process_metrics(pid: int | None, create_time: float | None, cpu_count: int) -> dict:
    result = {
        "cpu_percent_one_core": 0.0,
        "cpu_percent_machine": 0.0,
        "rss_bytes": 0,
        "process_count": 0,
        "available": False,
    }
    if not pid or create_time is None:
        return result
    try:
        leader = psutil.Process(pid)
        if leader.create_time() != create_time:
            return result
        children = leader.children(recursive=True)
        processes = [leader, *children]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return result

    cpu_percent = 0.0
    rss_bytes = 0
    live_pids = set()
    for proc in processes:
        try:
            process_key = (proc.pid, proc.create_time())
            if proc.pid in live_pids:
                continue
            live_pids.add(proc.pid)
            _sample_process_keys.add(process_key)
            tracked_proc = _process_cache.setdefault(process_key, proc)
            cpu_percent += tracked_proc.cpu_percent(interval=None)
            rss_bytes += tracked_proc.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    result.update({
        "cpu_percent_one_core": cpu_percent,
        "cpu_percent_machine": cpu_percent / max(1, cpu_count),
        "rss_bytes": rss_bytes,
        "process_count": len(live_pids),
        "available": bool(live_pids),
    })
    return result


def _host_sample() -> dict:
    virtual_memory = psutil.virtual_memory()
    try:
        load_average = list(os.getloadavg())
    except (AttributeError, OSError):
        load_average = None
    try:
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
    except (OSError, psutil.Error):
        cpu_per_core = []
    try:
        cpu_percent = psutil.cpu_percent(interval=None)
    except (OSError, psutil.Error):
        cpu_percent = 0.0
    return {
        "cpu_percent": cpu_percent,
        "cpu_per_core_percent": cpu_per_core,
        "logical_cpu_count": psutil.cpu_count(logical=True) or 1,
        "load_average": load_average,
        "memory": {
            "used_bytes": virtual_memory.used,
            "available_bytes": virtual_memory.available,
            "total_bytes": virtual_memory.total,
            "percent": virtual_memory.percent,
        },
    }


def _collect_sample() -> dict:
    global _sample_process_keys
    _sample_process_keys = set()
    timestamp = _timestamp()
    host = _host_sample()
    disks = _disk_samples()
    cpu_count = host["logical_cpu_count"]
    servers = []
    for profile in db.list_profiles():
        state = db.get_runtime_state(profile.id)
        if not state or state.status != ProfileStatus.RUNNING:
            continue
        metrics = _server_process_metrics(state.pid, state.pid_create_time, cpu_count)
        servers.append({
            "profile_id": profile.id,
            "name": profile.name,
            "ram_allocation_mb": profile.ram_mb,
            "ram_allocation_bytes": profile.ram_mb * 1024**2,
            **metrics,
        })
    # Keep psutil's CPU counters across samples, while dropping objects for dead processes.
    for process_key in tuple(_process_cache):
        if process_key not in _sample_process_keys:
            _process_cache.pop(process_key, None)
    return {
        "timestamp": timestamp,
        "host": host,
        "disks": disks,
        "servers": servers,
    }


def _history_sample(sample: dict) -> dict:
    return {
        "timestamp": sample["timestamp"],
        "host": {
            "cpu_percent": sample["host"]["cpu_percent"],
            "memory_used_bytes": sample["host"]["memory"]["used_bytes"],
            "memory_available_bytes": sample["host"]["memory"]["available_bytes"],
            "memory_total_bytes": sample["host"]["memory"]["total_bytes"],
        },
        "servers": [
            {
                "profile_id": server["profile_id"],
                "name": server["name"],
                "cpu_percent_one_core": server["cpu_percent_one_core"],
                "cpu_percent_machine": server["cpu_percent_machine"],
                "rss_bytes": server["rss_bytes"],
            }
            for server in sample["servers"]
        ],
    }


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


def _scan_profile_sizes() -> tuple[dict[str, dict], str]:
    sizes = {}
    for profile in db.list_profiles():
        profile_dir = PROFILES_DIR / profile.id
        server_dir = profile_dir / "server"
        world_folders = {
            name: _path_size(server_dir / name)
            for name in ("world", "world_nether", "world_the_end")
        }
        sizes[profile.id] = {
            "profile_id": profile.id,
            "server_bytes": _path_size(server_dir),
            "world_bytes": sum(world_folders.values()),
            "world_folders": world_folders,
            "backups_bytes": _path_size(profile_dir / "backups"),
        }
    return sizes, _timestamp()


async def _sampler_loop() -> None:
    global _latest
    while True:
        await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
        try:
            sample = _collect_sample()
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
    if _sampler_task and not _sampler_task.done():
        return
    try:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
    except (OSError, psutil.Error):
        logger.exception("Could not prime CPU monitoring")
    await asyncio.sleep(0.1)
    try:
        _latest = _collect_sample()
        _history.append(_history_sample(_latest))
    except Exception:
        logger.exception("Could not collect initial monitoring sample")
    _sampler_task = asyncio.create_task(_sampler_loop(), name="host-monitor-sampler")
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
