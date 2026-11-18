"""Bounded cleanup for regenerable Minecraft server artifacts."""
import logging
import os
import stat
import time
from pathlib import Path

from mcpanel.config import (
    CRASH_REPORT_KEEP,
    CRASH_REPORT_RETENTION_DAYS,
    DATA_DIR,
    PROFILES_DIR,
    SERVER_ROTATED_LOG_KEEP,
)

logger = logging.getLogger(__name__)


def _inside_data(path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(DATA_DIR.resolve())
    return resolved


def _regular_files(directory: Path, suffix: str | None = None) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        return []
    try:
        _inside_data(directory)
        paths = []
        for path in directory.iterdir():
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                if suffix is None or path.name.endswith(suffix):
                    _inside_data(path)
                    paths.append(path)
            except (OSError, ValueError):
                continue
        return paths
    except (OSError, ValueError):
        return []


def _mtime_or_minus_inf(path: Path) -> float:
    """Sort key that never raises; files deleted mid-scan sort last (and are
    skipped afterward anyway)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


def prune_server_artifacts(profile_id: str) -> None:
    server_dir = PROFILES_DIR / profile_id / "server"
    if server_dir.is_symlink():
        logger.warning("Skipping server artifact cleanup for symbolic-link server directory %s", profile_id)
        return
    try:
        server_dir = _inside_data(server_dir)
    except (OSError, ValueError):
        logger.warning("Skipping server artifact cleanup outside the data directory for %s", profile_id)
        return

    log_files = sorted(
        _regular_files(server_dir / "logs", ".log.gz"),
        key=_mtime_or_minus_inf,
        reverse=True,
    )
    for stale in log_files[SERVER_ROTATED_LOG_KEEP:]:
        try:
            _inside_data(stale).unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.warning("Could not remove old server log %s", stale)

    reports_dir = server_dir / "crash-reports"
    reports = sorted(_regular_files(reports_dir), key=_mtime_or_minus_inf, reverse=True)
    protected = {path.name for path in reports[:CRASH_REPORT_KEEP]}
    cutoff = time.time() - CRASH_REPORT_RETENTION_DAYS * 24 * 60 * 60
    for report in reports:
        try:
            if report.name not in protected and report.stat().st_mtime < cutoff:
                _inside_data(report).unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.warning("Could not remove old crash report %s", report)
